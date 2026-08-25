"""
classifier.py

The Groq-Powered Brain for Recover-Net.
Uses the native Groq Python SDK with JSON schema response_format for
structured, sub-200ms classification — no LangChain, no tool-calling overhead.
"""

import json
import os
from typing import Any, Dict, Literal, Optional

from dotenv import load_dotenv
from groq import Groq
from pydantic import BaseModel, Field

from security import mask_payload

load_dotenv()

# Groq model identifier
DEFAULT_GROQ_MODEL = os.getenv("GROQ_MODEL_ID", "openai/gpt-oss-20b")

# JSON schema used with Groq's response_format to guarantee structured output
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
    """Structured decision returned by the Groq classification engine."""

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

    def to_dict(self) -> Dict[str, Any]:
        return {"intent": self.intent, "confidence": self.confidence}


def get_groq_client() -> Groq:
    """Initializes and returns a native Groq client."""
    api_key = os.getenv("GROQ_API_KEY")
    return Groq(api_key=api_key)


def classify_payment_failure(
    raw_or_masked_payload: Dict[str, Any],
    client: Optional[Any] = None,
    model: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> RecoveryDecision:
    """
    Sends the BlindLog-sanitized payload to the Groq model, using JSON schema
    response_format to enforce structured output — no tool calling, no regex parsing.

    Parameters:
        raw_or_masked_payload: Webhook payload dictionary.
        client: Optional Groq client instance (for testing/mocking).
        model: Optional model identifier override.
        secret_key: Optional BlindLog secret key override for payload sanitization.

    Returns:
        RecoveryDecision containing the predicted intent and confidence.
    """
    # 1. BlindLog-sanitize the payload before it leaves the system
    sanitized_payload = mask_payload(raw_or_masked_payload, secret_key=secret_key)

    # 2. Resolve client and model
    groq_client = client or get_groq_client()
    target_model = model or os.getenv("GROQ_MODEL_ID") or DEFAULT_GROQ_MODEL

    # 3. System prompt with decision guidelines
    system_prompt = (
        "You are an expert payment failure triage and recovery routing brain. "
        "Analyze the provided sanitized payment failure webhook event and determine the optimal recovery action. "
        "Return a JSON object with exactly two fields: 'intent' and 'confidence'.\n"
        "Decision Guidelines:\n"
        "- 'retry_now': Transient network/gateway timeouts, temporary processing glitches, "
        "or high past success rate (>0.75).\n"
        "- 'offer_emi': Insufficient funds on high-value transactions with moderate-to-good customer history.\n"
        "- 'escalate_to_human': Suspected fraud, invalid CVV repeated failures, "
        "or very low customer success rate."
    )

    user_content = json.dumps(sanitized_payload, indent=2)

    # 4. Call Groq API with JSON schema response_format — guaranteed structured output
    response = groq_client.chat.completions.create(
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
        raise RuntimeError("Groq returned an empty response content.")

    try:
        arguments = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse Groq response as JSON: {raw_content}"
        ) from exc

    return RecoveryDecision(**arguments)
