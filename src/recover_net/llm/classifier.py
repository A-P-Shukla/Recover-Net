"""
llm/classifier.py

The AWS Bedrock classification layer for Recover-Net.
Uses the OpenAI-compatible Python SDK pointed at the Bedrock endpoint
(bedrock-mantle or bedrock-runtime) for structured JSON-schema responses.

Required environment variables:
    OPENAI_API_KEY   — AWS Bedrock bearer token / API key
    OPENAI_BASE_URL  — Bedrock OpenAI-compatible endpoint
                       e.g. https://bedrock-mantle.ap-southeast-2.api.aws/v1
    BEDROCK_MODEL    — Bedrock model ID (default: mistral.ministral-3-8b-instruct)
"""

import json
import os
from typing import Any, Dict, Literal, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field

from recover_net.core.security import mask_payload

load_dotenv()

# Bedrock model identifier — override via BEDROCK_MODEL env var.
DEFAULT_BEDROCK_MODEL = os.getenv(
    "BEDROCK_MODEL",
    "mistral.ministral-3-8b-instruct",
)

# JSON schema used with Bedrock's response_format to guarantee structured output
RECOVERY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["retry_now", "offer_emi", "escalate_to_human"],
        },
        "confidence": {
            "type": "number",
        },
        "discount": {
            "type": ["number", "null"],
            "minimum": 0,
            "maximum": 100,
            "description": "Optional EMI interest discount percentage.",
        },
    },
    "required": ["intent", "confidence"],
}

# Keep RECOVERY_ROUTING_TOOL for backwards-compatibility with existing tests
RECOVERY_ROUTING_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "route_recovery_action",
        "description": "Classify the failed transaction and determine the recovery route.",
        "parameters": RECOVERY_SCHEMA,
    },
}

RecoveryIntent = Literal["retry_now", "offer_emi", "escalate_to_human"]


class RecoveryDecision(BaseModel):
    """Structured decision returned by the Bedrock classification engine."""

    intent: RecoveryIntent = Field(
        ...,
        description="Categorized recovery intent ('retry_now', 'offer_emi', 'escalate_to_human')",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score between 0.0 and 1.0",
    )
    discount: Optional[float] = Field(
        default=None, ge=0.0, le=100.0,
        description="Optional EMI interest discount percentage.",
    )

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "intent": self.intent,
            "confidence": self.confidence,
        }
        if self.discount is not None:
            result["discount"] = self.discount
        return result


def get_bedrock_client() -> OpenAI:
    """
    Initializes and returns an OpenAI-compatible client pointed at AWS Bedrock.

    Reads OPENAI_API_KEY and OPENAI_BASE_URL from the environment — the same
    variables used by aws.py so the configuration is consistent across the
    project.  Fails closed if OPENAI_API_KEY is missing or empty.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. "
            "Set OPENAI_API_KEY to your AWS Bedrock bearer token before making LLM calls."
        )
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=30.0,
    )

def classify_payment_failure(
    raw_or_masked_payload: Dict[str, Any],
    client: Optional[Any] = None,
    model: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> RecoveryDecision:
    """
    Sends the BlindLog-sanitized payload to the AWS Bedrock model, using JSON
    schema response_format to enforce structured output — no tool calling, no
    regex parsing.

    Parameters:
        raw_or_masked_payload: Webhook payload dictionary.
        client: Optional OpenAI-compatible client instance (for testing/mocking).
        model: Optional model identifier override.
        secret_key: Optional BlindLog secret key override for payload sanitization.

    Returns:
        RecoveryDecision containing the predicted intent and confidence.
    """
    # 1. BlindLog-sanitize the payload before it leaves the system
    sanitized_payload = mask_payload(raw_or_masked_payload, secret_key=secret_key)

    # 2. Resolve client and model
    bedrock_client = client or get_bedrock_client()
    target_model = model or os.getenv("BEDROCK_MODEL") or DEFAULT_BEDROCK_MODEL

    # 3. System prompt with explicit, unambiguous decision rules
    system_prompt = (
        "You are a payment failure recovery routing engine. "
        "Your job is to maximise revenue recovery. "
        "Default to recovery actions (retry_now or offer_emi) unless a hard escalation condition is met.\n\n"
        "HARD ESCALATION — always choose escalate_to_human:\n"
        "  - error_code is 'fraud_suspected'\n"
        "  - error_code is 'invalid_cvv'\n"
        "  - past_success_rate < 0.20 AND amount > 10000\n\n"
        "RECOVERY ROUTING — for all other cases use these rules in order:\n"
        "  1. error_code is 'gateway_timeout' OR error_code is 'card_declined' → retry_now\n"
        "     (transient errors; the card itself is fine, retry immediately)\n"
        "  2. error_code is 'insufficient_funds' AND amount > 8000 → offer_emi\n"
        "     (customer wants to pay but lacks funds; split the payment)\n"
        "  3. error_code is 'insufficient_funds' AND amount <= 8000 → retry_now\n"
        "     (small amount; a retry after a short delay usually succeeds)\n"
        "  4. past_success_rate >= 0.75 → retry_now\n"
        "     (reliable customer; transient failure, retry)\n"
        "  5. past_success_rate >= 0.35 → offer_emi\n"
        "     (moderate history; give the customer a payment option)\n"
        "  6. past_success_rate < 0.35 → escalate_to_human\n"
        "     (persistent failures with poor history; needs human review)\n\n"
        "Return ONLY a JSON object: {\"intent\": \"...\", \"confidence\": 0.0-1.0, \"discount\": null_or_number}.\n"
        "Set discount (0-100) only when intent is offer_emi. Set to null otherwise.\n"
        "Never escalate unless one of the HARD ESCALATION conditions is met."
    )

    user_content = json.dumps(sanitized_payload, indent=2)

    # 4. Call Bedrock — no client-side rate limiting needed (default: 10,000 RPM)
    response = bedrock_client.chat.completions.create(
        model=target_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "recovery_action",
                "schema": RECOVERY_SCHEMA,
            },
        },
        temperature=0.0,
    )

    # 5. Parse the JSON content directly — no tool_calls needed
    raw_content = response.choices[0].message.content

    if not raw_content:
        raise RuntimeError("Bedrock returned an empty response content.")

    try:
        arguments = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse Bedrock response as JSON: {raw_content}"
        ) from exc

    return RecoveryDecision(**arguments)
