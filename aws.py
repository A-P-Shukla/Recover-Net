import json
import os
from pathlib import Path
from typing import Any

from openai import OpenAI


def load_env_file() -> None:
    env_path = Path(__file__).resolve().with_name(".env")
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file()

api_key: str | None = os.getenv("OPENAI_API_KEY")
base_url: str | None = os.getenv("OPENAI_BASE_URL")

if not api_key:
    raise RuntimeError("Missing OPENAI_API_KEY in .env")

client = OpenAI(
    api_key=api_key,
    base_url=base_url,
)

# Explicitly type the payload so Pylance knows the value types.
payload: dict[str, Any] = {
    "transaction_id": "abc-123",
    "merchant_id": "default",
    "amount": 5000,
    "error_code": "gateway_timeout",
    "past_success_rate": 0.85,
    "user_email": "blnd_ref_abc...@masked.com",
    "phone": "blind:abcd1234",
}

# JSON Schema is inherently recursive and contains mixed value types,
# so dict[str, Any] is appropriate here.
schema: dict[str, Any] = {
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
        },
    },
    "required": ["intent", "confidence"],
}

response = client.chat.completions.create(
    model="mistral.ministral-3-8b-instruct",
    messages=[
        {
            "role": "system",
            "content": (
                "Return ONLY valid JSON with keys intent, confidence, discount. "
                "Use intent from [retry_now, offer_emi, escalate_to_human]. "
                "confidence must be between 0 and 1. "
                "discount is a number or null. No markdown."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload),
        },
    ],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "recovery_action",
            "schema": schema,
        },
    },
    temperature=0.0,
)

print(response.choices[0].message.content)
