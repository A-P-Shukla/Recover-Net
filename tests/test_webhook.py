"""
tests/test_webhook.py

Integration and logging tests for the FastAPI POST /webhook/payment-failure endpoint
and BlindLog ASGI middleware logging.
"""

import json
import logging
import sys
from typing import Any, Dict, Generator
from pathlib import Path

# Ensure project root is on sys.path when running script directly
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from recover_net.db.session import get_db
from recover_net.core.app import app


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """FastAPI TestClient wired to the transactional test database session."""
    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_payment_failure_webhook_endpoint_logs_masked_pii(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Fires a mock webhook payload from failed_webhooks.json at the endpoint.
    Verifies that:
    1. The HTTP request succeeds (201 Created).
    2. The terminal/middleware logs contain the hashed email and phone.
    3. The raw email and raw phone are NEVER emitted in logs.
    """
    # Load mock webhook payload
    mock_file = ROOT_DIR / "failed_webhooks.json"
    with open(mock_file, "r", encoding="utf-8") as f:
        mock_payloads = json.load(f)

    sample_payload: Dict[str, Any] = dict(mock_payloads[0])
    raw_email = sample_payload["user_email"]
    raw_phone = sample_payload["phone"]

    # Capture logs from blindlog.middleware at INFO level
    caplog.set_level(logging.INFO)

    response = client.post("/webhook/payment-failure", json=sample_payload)

    # 1. Verify HTTP Response
    assert response.status_code == 201
    resp_data = response.json()
    assert resp_data["status"] == "success"
    assert resp_data["source_transaction_id"] == sample_payload["transaction_id"]
    assert resp_data["masked_user_email"] != raw_email
    assert resp_data["masked_phone"] != raw_phone
    # Masked value should look like a BlindLog token (not the original)
    assert resp_data["masked_user_email"] != raw_email

    # 2. Inspect captured terminal logs
    captured_logs = caplog.text
    print("\n--- CAPTURED TERMINAL LOGS ---")
    print(captured_logs)

    # 3. Assertions on logged content
    assert "BlindLog [Request Body]" in captured_logs
    assert resp_data["masked_user_email"] in captured_logs
    assert resp_data["masked_phone"] in captured_logs

    # 4. Strict Security Verification: Raw PII is NOT in terminal logs
    assert raw_email not in captured_logs, f"Security Violation: Raw email {raw_email} was found in terminal logs!"
    assert raw_phone not in captured_logs, f"Security Violation: Raw phone {raw_phone} was found in terminal logs!"


def test_payment_failure_webhook_rejects_duplicate_transaction(client: TestClient) -> None:
    """Verifies that replaying an existing webhook transaction_id returns 409 Conflict."""
    payload: Dict[str, Any] = {
        "transaction_id": "duplicate-test-id-1234",
        "user_email": "dup.user@example.com",
        "phone": "+919876543210",
        "amount": 1500,
        "error_code": "insufficient_funds",
        "past_success_rate": 0.85,
    }

    res1 = client.post("/webhook/payment-failure", json=payload)
    assert res1.status_code == 201

    res2 = client.post("/webhook/payment-failure", json=payload)
    assert res2.status_code == 409
    assert "already been processed" in res2.json()["detail"]


def test_payment_failure_webhook_rejects_missing_fields(client: TestClient) -> None:
    """Verifies that requests missing required fields fail gracefully."""
    invalid_payload: Dict[str, Any] = {
        "amount": 500,
        "error_code": "invalid_cvv",
    }
    response = client.post("/webhook/payment-failure", json=invalid_payload)
    assert response.status_code == 422


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main(["-s", __file__]))
