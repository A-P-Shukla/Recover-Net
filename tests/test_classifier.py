"""
tests/test_classifier.py

Unit tests for the Groq-Powered Brain (classifier.py).
Uses native Groq SDK with JSON schema response_format — no tool calling.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest
from pydantic import ValidationError

from classifier import (
    DEFAULT_GROQ_MODEL,
    RECOVERY_ROUTING_TOOL,
    RECOVERY_SCHEMA,
    RecoveryDecision,
    classify_payment_failure,
)


def _make_mock_completion(arguments_dict: dict):
    """Build a mock Groq chat completion whose message.content is JSON."""
    mock_message = MagicMock()
    mock_message.content = json.dumps(arguments_dict)
    mock_message.tool_calls = None  # Groq JSON-schema path — no tool calls

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    return mock_response


# ---------------------------------------------------------------------------
# Schema sanity checks
# ---------------------------------------------------------------------------

def test_recovery_schema_shape():
    """RECOVERY_SCHEMA must define the required intent enum and confidence number."""
    props = RECOVERY_SCHEMA["properties"]
    assert props["intent"]["type"] == "string"
    assert props["intent"]["enum"] == ["retry_now", "offer_emi", "escalate_to_human"]
    assert props["confidence"]["type"] == "number"
    assert RECOVERY_SCHEMA["required"] == ["intent", "confidence"]


def test_recovery_routing_tool_backwards_compat():
    """RECOVERY_ROUTING_TOOL still exposes the same schema for backwards compatibility."""
    assert RECOVERY_ROUTING_TOOL["type"] == "function"
    fn = RECOVERY_ROUTING_TOOL["function"]
    assert fn["name"] == "route_recovery_action"
    assert fn["parameters"] is RECOVERY_SCHEMA


# ---------------------------------------------------------------------------
# Happy-path classification tests
# ---------------------------------------------------------------------------

def test_classify_payment_failure_sanitizes_pii_and_calls_groq():
    """
    Verifies that:
    1. The payload is BlindLog-sanitized before sending to the model.
    2. The correct model and response_format are passed to the Groq API.
    3. The JSON content is parsed into a RecoveryDecision object.
    """
    raw_payload = {
        "transaction_id": "7bf5d920-0619-4d3e-9d00-108faf65028c",
        "user_email": "vihaaniyer911@gmail.com",
        "phone": "+916772495977",
        "amount": 45735,
        "error_code": "gateway_timeout",
        "past_success_rate": 0.88,
    }

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_completion(
        {"intent": "retry_now", "confidence": 0.94}
    )

    decision = classify_payment_failure(raw_payload, client=mock_client)

    # Return value
    assert isinstance(decision, RecoveryDecision)
    assert decision.intent == "retry_now"
    assert decision.confidence == 0.94
    assert decision.to_dict() == {"intent": "retry_now", "confidence": 0.94}

    # API call parameters
    assert mock_client.chat.completions.create.called
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs

    assert call_kwargs["model"] == DEFAULT_GROQ_MODEL
    assert call_kwargs["temperature"] == 0.0
    assert call_kwargs["response_format"]["type"] == "json_schema"
    assert call_kwargs["response_format"]["json_schema"]["name"] == "recovery_action"
    assert call_kwargs["response_format"]["json_schema"]["schema"] is RECOVERY_SCHEMA

    # PII must be masked in the prompt sent to the LLM
    messages = call_kwargs["messages"]
    user_prompt = messages[1]["content"]
    assert raw_payload["user_email"] not in user_prompt, "Raw email leaked in LLM prompt!"
    assert raw_payload["phone"] not in user_prompt, "Raw phone leaked in LLM prompt!"
    assert "blnd_ref_" in user_prompt or "@masked.com" in user_prompt
    assert "blind:" in user_prompt


def test_classify_payment_failure_offer_emi():
    """Test classification route for offer_emi."""
    payload = {
        "transaction_id": "11111111-2222-3333-4444-555555555555",
        "user_email": "customer@example.com",
        "phone": "+919876543210",
        "amount": 35000,
        "error_code": "insufficient_funds",
        "past_success_rate": 0.65,
    }

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_completion(
        {"intent": "offer_emi", "confidence": 0.87}
    )

    decision = classify_payment_failure(payload, client=mock_client)
    assert decision.intent == "offer_emi"
    assert decision.confidence == 0.87


def test_classify_payment_failure_escalate_to_human():
    """Test classification route for escalate_to_human."""
    payload = {
        "transaction_id": "99999999-8888-7777-6666-555555555555",
        "user_email": "fraud_test@example.com",
        "phone": "+919876543210",
        "amount": 49000,
        "error_code": "fraud_suspected",
        "past_success_rate": 0.10,
    }

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_completion(
        {"intent": "escalate_to_human", "confidence": 0.99}
    )

    decision = classify_payment_failure(payload, client=mock_client)
    assert decision.intent == "escalate_to_human"
    assert decision.confidence == 0.99


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------

def test_classify_payment_failure_raises_on_empty_content():
    """Verifies RuntimeError when Groq returns empty message content."""
    mock_message = MagicMock()
    mock_message.content = ""

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=mock_message)]
    )

    with pytest.raises(RuntimeError, match="empty response content"):
        classify_payment_failure(
            {"user_email": "test@example.com", "phone": "+919999999999"},
            client=mock_client,
        )


def test_classify_payment_failure_rejects_invalid_intent():
    """Verifies ValidationError on hallucinated or unknown intent values."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_mock_completion(
        {"intent": "ignore_and_drop", "confidence": 0.5}
    )

    with pytest.raises(ValidationError):
        classify_payment_failure(
            {"user_email": "test@example.com", "phone": "+919999999999"},
            client=mock_client,
        )


def test_classify_payment_failure_rejects_invalid_json():
    """Verifies RuntimeError when Groq response content is not valid JSON."""
    mock_message = MagicMock()
    mock_message.content = "NOT_VALID_JSON"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=mock_message)]
    )

    with pytest.raises(RuntimeError, match="Failed to parse Groq response as JSON"):
        classify_payment_failure(
            {"user_email": "test@example.com", "phone": "+919999999999"},
            client=mock_client,
        )


if __name__ == "__main__":
    sys.exit(pytest.main(["-s", __file__]))
