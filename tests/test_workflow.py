"""
tests/test_workflow.py

Unit and integration tests for the Orchestrator (workflow.py).

All Groq API calls are mocked — tests exercise the full pipeline logic
(ingestion → classification → guardrail → audit write) against an
in-memory SQLite database via the shared db_session fixture.
"""

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from models import AuditLog, Transaction
from workflow import RecoveryResult, _INTENT_TO_STATUS, run_recovery_pipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_PAYLOAD = {
    "transaction_id": "aabbccdd-0000-0000-0000-111111111111",
    "user_email": "test.user@example.com",
    "phone": "+919876543210",
    "amount": 5000,
    "error_code": "gateway_timeout",
    "past_success_rate": 0.85,
}


def _mock_groq_client(intent: str, confidence: float = 0.90) -> MagicMock:
    """Build a mock Groq client that returns the given intent and confidence."""
    mock_message = MagicMock()
    mock_message.content = json.dumps({"intent": intent, "confidence": confidence})
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=mock_message)]
    )
    return mock_client


def _fresh_payload(**overrides) -> dict:
    """Return a copy of BASE_PAYLOAD with a unique transaction_id each call."""
    p = {**BASE_PAYLOAD, "transaction_id": str(uuid.uuid4())}
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# RecoveryResult shape
# ---------------------------------------------------------------------------

class TestRecoveryResult:
    def test_to_dict_has_all_keys(self, db_session: Session):
        result = run_recovery_pipeline(
            _fresh_payload(),
            db_session,
            groq_client=_mock_groq_client("retry_now"),
        )
        d = result.to_dict()
        assert set(d.keys()) == {
            "transaction_id",
            "audit_log_id",
            "llm_intent",
            "llm_confidence",
            "final_intent",
            "overridden",
            "rule_applied",
            "final_status",
        }

    def test_result_is_immutable(self, db_session: Session):
        result = run_recovery_pipeline(
            _fresh_payload(),
            db_session,
            groq_client=_mock_groq_client("retry_now"),
        )
        with pytest.raises((AttributeError, TypeError)):
            result.final_intent = "offer_emi"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Happy-path: no guardrail override
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_retry_now_pass_through(self, db_session: Session):
        result = run_recovery_pipeline(
            _fresh_payload(error_code="gateway_timeout", amount=500),
            db_session,
            groq_client=_mock_groq_client("retry_now", confidence=0.95),
        )
        assert result.final_intent == "retry_now"
        assert result.overridden is False
        assert result.rule_applied is None
        assert result.final_status == "RETRIED"
        assert result.llm_intent == "retry_now"
        assert result.llm_confidence == 0.95

    def test_offer_emi_pass_through(self, db_session: Session):
        result = run_recovery_pipeline(
            _fresh_payload(
                error_code="insufficient_funds",
                amount=8000,
                past_success_rate=0.65,
            ),
            db_session,
            groq_client=_mock_groq_client("offer_emi", confidence=0.88),
        )
        assert result.final_intent == "offer_emi"
        assert result.overridden is False
        assert result.final_status == "EMI_OFFERED"

    def test_escalate_pass_through(self, db_session: Session):
        """
        fraud_suspected where LLM already chose escalate.
        Rule fires (rule_applied is set) but overridden=False.
        """
        result = run_recovery_pipeline(
            _fresh_payload(
                error_code="fraud_suspected",
                amount=1000,
                past_success_rate=0.9,
            ),
            db_session,
            groq_client=_mock_groq_client("escalate_to_human", confidence=0.99),
        )
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is False
        assert result.final_status == "ESCALATED"


# ---------------------------------------------------------------------------
# Guardrail overrides
# ---------------------------------------------------------------------------

class TestGuardrailOverrides:
    def test_fraud_overrides_retry_now(self, db_session: Session):
        """AI proposed retry on a fraud case — guardrail must escalate."""
        result = run_recovery_pipeline(
            _fresh_payload(error_code="fraud_suspected", amount=500, past_success_rate=0.9),
            db_session,
            groq_client=_mock_groq_client("retry_now"),
        )
        assert result.final_intent == "escalate_to_human"
        assert result.llm_intent == "retry_now"
        assert result.overridden is True
        assert result.rule_applied == "RULE_FRAUD_ESCALATE"
        assert result.final_status == "ESCALATED"

    def test_fraud_overrides_offer_emi(self, db_session: Session):
        result = run_recovery_pipeline(
            _fresh_payload(error_code="fraud_suspected"),
            db_session,
            groq_client=_mock_groq_client("offer_emi"),
        )
        assert result.final_intent == "escalate_to_human"
        assert result.rule_applied == "RULE_FRAUD_ESCALATE"

    def test_high_risk_overrides_offer_emi(self, db_session: Session):
        """High amount + poor history — guardrail must escalate regardless of AI intent."""
        result = run_recovery_pipeline(
            _fresh_payload(
                error_code="insufficient_funds",
                amount=50_000,
                past_success_rate=0.05,
            ),
            db_session,
            groq_client=_mock_groq_client("offer_emi"),
        )
        assert result.final_intent == "escalate_to_human"
        assert result.rule_applied == "RULE_HIGH_RISK_ESCALATE"
        assert result.overridden is True

    def test_timeout_emi_corrected_to_retry(self, db_session: Session):
        """AI hallucinated EMI on a gateway timeout — guardrail must correct to retry."""
        result = run_recovery_pipeline(
            _fresh_payload(error_code="gateway_timeout", amount=500, past_success_rate=0.8),
            db_session,
            groq_client=_mock_groq_client("offer_emi"),
        )
        assert result.final_intent == "retry_now"
        assert result.llm_intent == "offer_emi"
        assert result.rule_applied == "RULE_TIMEOUT_EMI_CORRECT"
        assert result.overridden is True
        assert result.final_status == "RETRIED"


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------

class TestDatabaseWrites:
    def test_transaction_row_is_persisted(self, db_session: Session):
        payload = _fresh_payload()
        result = run_recovery_pipeline(
            payload,
            db_session,
            groq_client=_mock_groq_client("retry_now"),
        )
        tx = db_session.get(Transaction, result.transaction_id)
        assert tx is not None
        assert str(tx.source_transaction_id) == payload["transaction_id"]
        # PII must be masked
        assert tx.user_email != payload["user_email"]
        assert tx.phone != payload["phone"]

    def test_audit_log_row_is_persisted(self, db_session: Session):
        result = run_recovery_pipeline(
            _fresh_payload(),
            db_session,
            groq_client=_mock_groq_client("retry_now"),
        )
        log = db_session.get(AuditLog, result.audit_log_id)
        assert log is not None
        assert log.transaction_id == result.transaction_id
        assert log.final_status == result.final_status

    def test_audit_log_llm_proposed_action_is_valid_json(self, db_session: Session):
        result = run_recovery_pipeline(
            _fresh_payload(),
            db_session,
            groq_client=_mock_groq_client("retry_now", confidence=0.77),
        )
        log = db_session.get(AuditLog, result.audit_log_id)
        proposed = json.loads(log.llm_proposed_action)
        assert proposed["intent"] == "retry_now"
        assert proposed["confidence"] == 0.77

    def test_audit_log_guardrail_decision_is_valid_json(self, db_session: Session):
        result = run_recovery_pipeline(
            _fresh_payload(error_code="fraud_suspected"),
            db_session,
            groq_client=_mock_groq_client("retry_now"),
        )
        log = db_session.get(AuditLog, result.audit_log_id)
        decision = json.loads(log.guardrail_decision)
        assert decision["overridden"] is True
        assert decision["rule_applied"] == "RULE_FRAUD_ESCALATE"
        assert decision["final_intent"] == "escalate_to_human"
        assert decision["original_intent"] == "retry_now"

    def test_override_flag_stored_in_guardrail_decision(self, db_session: Session):
        """When guardrail passes through, overridden=False must be stored."""
        result = run_recovery_pipeline(
            _fresh_payload(error_code="gateway_timeout"),
            db_session,
            groq_client=_mock_groq_client("retry_now"),
        )
        log = db_session.get(AuditLog, result.audit_log_id)
        decision = json.loads(log.guardrail_decision)
        assert decision["overridden"] is False
        assert decision["rule_applied"] is None

    def test_transaction_and_audit_log_are_linked(self, db_session: Session):
        result = run_recovery_pipeline(
            _fresh_payload(),
            db_session,
            groq_client=_mock_groq_client("retry_now"),
        )
        tx = db_session.get(Transaction, result.transaction_id)
        assert any(str(log.log_id) == str(result.audit_log_id) for log in tx.audit_logs)


# ---------------------------------------------------------------------------
# Status label mapping
# ---------------------------------------------------------------------------

class TestIntentToStatus:
    def test_all_intents_have_status_labels(self):
        for intent in ("retry_now", "offer_emi", "escalate_to_human"):
            assert intent in _INTENT_TO_STATUS

    def test_status_labels_are_correct(self):
        assert _INTENT_TO_STATUS["retry_now"] == "RETRIED"
        assert _INTENT_TO_STATUS["offer_emi"] == "EMI_OFFERED"
        assert _INTENT_TO_STATUS["escalate_to_human"] == "ESCALATED"


# ---------------------------------------------------------------------------
# FastAPI endpoint integration
# ---------------------------------------------------------------------------

class TestRecoverEndpoint:
    @pytest.fixture
    def client(self, db_session: Session):
        from main import app
        from database import get_db

        app.dependency_overrides[get_db] = lambda: db_session
        yield TestClient(app, raise_server_exceptions=False)
        app.dependency_overrides.clear()

    def test_recover_endpoint_returns_201_on_success(self, client: TestClient):
        payload = _fresh_payload(error_code="gateway_timeout", amount=500)
        with patch("workflow.classify_payment_failure") as mock_classify:
            from classifier import RecoveryDecision
            mock_classify.return_value = RecoveryDecision(intent="retry_now", confidence=0.91)
            response = client.post("/webhook/payment-failure/recover", json=payload)

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "success"
        assert body["final_intent"] == "retry_now"
        assert body["final_status"] == "RETRIED"
        assert body["guardrail_overridden"] is False
        assert "transaction_id" in body
        assert "audit_log_id" in body

    def test_recover_endpoint_returns_guardrail_override_fields(self, client: TestClient):
        payload = _fresh_payload(error_code="fraud_suspected", amount=500)
        with patch("workflow.classify_payment_failure") as mock_classify:
            from classifier import RecoveryDecision
            mock_classify.return_value = RecoveryDecision(intent="retry_now", confidence=0.80)
            response = client.post("/webhook/payment-failure/recover", json=payload)

        assert response.status_code == 201
        body = response.json()
        assert body["final_intent"] == "escalate_to_human"
        assert body["guardrail_overridden"] is True
        assert body["rule_applied"] == "RULE_FRAUD_ESCALATE"
        assert body["llm_proposed_intent"] == "retry_now"

    def test_recover_endpoint_422_on_missing_fields(self, client: TestClient):
        response = client.post("/webhook/payment-failure/recover", json={})
        assert response.status_code == 422

    def test_existing_ingest_endpoint_still_works(self, client: TestClient):
        """The original /webhook/payment-failure endpoint must not be broken."""
        payload = _fresh_payload()
        response = client.post("/webhook/payment-failure", json=payload)
        assert response.status_code == 201
        assert response.json()["status"] == "success"


if __name__ == "__main__":
    sys.exit(pytest.main(["-s", __file__]))
