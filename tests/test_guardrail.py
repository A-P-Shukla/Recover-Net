"""
tests/test_guardrail.py

Unit tests for the Deterministic Guardrail (guardrail.py).

Every hard rule is tested with:
  - the exact boundary case that should fire the rule
  - an adjacent case that must NOT fire (proves the rule is tight, not loose)
  - a pass-through case confirming the AI decision survives unchanged
"""

import sys
from decimal import Decimal
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest

from guardrail import (
    RULE_FRAUD_ESCALATE,
    RULE_INVALID_CVV_ESCALATE,
    RULE_HIGH_RISK_ESCALATE,
    RULE_TIMEOUT_EMI_CORRECT,
    GuardrailResult,
    evaluate_action,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tx(error_code="gateway_timeout", amount=500, past_success_rate=0.8):
    return {
        "error_code": error_code,
        "amount": amount,
        "past_success_rate": past_success_rate,
    }


# ---------------------------------------------------------------------------
# Rule 1 — RULE_FRAUD_ESCALATE
# ---------------------------------------------------------------------------

class TestFraudEscalate:
    def test_fraud_with_retry_now_is_overridden(self):
        result = evaluate_action(_tx(error_code="fraud_suspected"), intent="retry_now")
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is True
        assert result.rule_applied == RULE_FRAUD_ESCALATE
        assert result.original_intent == "retry_now"

    def test_fraud_with_offer_emi_is_overridden(self):
        result = evaluate_action(_tx(error_code="fraud_suspected"), intent="offer_emi")
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is True
        assert result.rule_applied == RULE_FRAUD_ESCALATE

    def test_fraud_already_escalate_not_overridden(self):
        """LLM correctly chose escalate_to_human — rule fires but overridden=False."""
        result = evaluate_action(_tx(error_code="fraud_suspected"), intent="escalate_to_human")
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is False
        assert result.rule_applied == RULE_FRAUD_ESCALATE

    def test_non_fraud_error_code_does_not_trigger_rule(self):
        result = evaluate_action(_tx(error_code="insufficient_funds"), intent="retry_now")
        assert result.rule_applied != RULE_FRAUD_ESCALATE


# ---------------------------------------------------------------------------
# Rule 2 — RULE_INVALID_CVV_ESCALATE
# ---------------------------------------------------------------------------

class TestInvalidCvvEscalate:
    def test_invalid_cvv_with_retry_is_overridden(self):
        result = evaluate_action(_tx(error_code="invalid_cvv"), intent="retry_now")
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is True
        assert result.rule_applied == RULE_INVALID_CVV_ESCALATE

    def test_invalid_cvv_with_emi_is_overridden(self):
        result = evaluate_action(_tx(error_code="invalid_cvv"), intent="offer_emi")
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is True
        assert result.rule_applied == RULE_INVALID_CVV_ESCALATE

    def test_invalid_cvv_already_escalate_not_overridden(self):
        """LLM correctly chose escalate — rule enforced but overridden=False."""
        result = evaluate_action(_tx(error_code="invalid_cvv"), intent="escalate_to_human")
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is False
        assert result.rule_applied == RULE_INVALID_CVV_ESCALATE

    def test_non_cvv_error_code_does_not_trigger_rule(self):
        result = evaluate_action(_tx(error_code="gateway_timeout"), intent="retry_now")
        assert result.rule_applied != RULE_INVALID_CVV_ESCALATE


# ---------------------------------------------------------------------------
# Rule 3 — RULE_HIGH_RISK_ESCALATE
# ---------------------------------------------------------------------------

class TestHighRiskEscalate:
    def test_high_amount_low_success_rate_is_overridden(self):
        result = evaluate_action(
            _tx(error_code="insufficient_funds", amount=15000, past_success_rate=0.1),
            intent="offer_emi",
        )
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is True
        assert result.rule_applied == RULE_HIGH_RISK_ESCALATE
        assert result.original_intent == "offer_emi"

    def test_high_risk_already_escalate_not_overridden(self):
        """LLM correctly chose escalate — rule enforced but overridden=False."""
        result = evaluate_action(
            _tx(error_code="insufficient_funds", amount=50_000, past_success_rate=0.05),
            intent="escalate_to_human",
        )
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is False
        assert result.rule_applied == RULE_HIGH_RISK_ESCALATE

    def test_boundary_amount_exactly_10000_does_not_trigger(self):
        """Amount must be strictly > 10000, not >=."""
        result = evaluate_action(
            _tx(error_code="insufficient_funds", amount=10_000, past_success_rate=0.1),
            intent="offer_emi",
        )
        assert result.rule_applied != RULE_HIGH_RISK_ESCALATE

    def test_boundary_success_rate_exactly_0_2_does_not_trigger(self):
        """Success rate must be strictly < 0.2, not <=."""
        result = evaluate_action(
            _tx(error_code="insufficient_funds", amount=15_000, past_success_rate=0.2),
            intent="offer_emi",
        )
        assert result.rule_applied != RULE_HIGH_RISK_ESCALATE

    def test_high_amount_good_success_rate_passes_through(self):
        result = evaluate_action(
            _tx(error_code="insufficient_funds", amount=50_000, past_success_rate=0.75),
            intent="offer_emi",
        )
        assert result.rule_applied != RULE_HIGH_RISK_ESCALATE
        assert result.overridden is False

    def test_missing_success_rate_does_not_trigger(self):
        result = evaluate_action(
            {"error_code": "insufficient_funds", "amount": 50_000, "past_success_rate": None},
            intent="offer_emi",
        )
        assert result.rule_applied != RULE_HIGH_RISK_ESCALATE

    def test_decimal_amount_is_handled(self):
        result = evaluate_action(
            _tx(error_code="insufficient_funds", amount=Decimal("20000.00"), past_success_rate=0.05),
            intent="retry_now",
        )
        assert result.final_intent == "escalate_to_human"
        assert result.rule_applied == RULE_HIGH_RISK_ESCALATE


# ---------------------------------------------------------------------------
# Rule 4 — RULE_TIMEOUT_EMI_CORRECT
# ---------------------------------------------------------------------------

class TestTimeoutEmiCorrect:
    def test_gateway_timeout_with_offer_emi_is_corrected(self):
        result = evaluate_action(_tx(error_code="gateway_timeout"), intent="offer_emi")
        assert result.final_intent == "retry_now"
        assert result.overridden is True
        assert result.rule_applied == RULE_TIMEOUT_EMI_CORRECT
        assert result.original_intent == "offer_emi"

    def test_gateway_timeout_with_retry_now_passes_through(self):
        result = evaluate_action(_tx(error_code="gateway_timeout"), intent="retry_now")
        assert result.final_intent == "retry_now"
        assert result.overridden is False

    def test_gateway_timeout_with_escalate_passes_through(self):
        result = evaluate_action(_tx(error_code="gateway_timeout"), intent="escalate_to_human")
        assert result.final_intent == "escalate_to_human"
        assert result.overridden is False

    def test_non_timeout_offer_emi_passes_through(self):
        result = evaluate_action(
            _tx(error_code="insufficient_funds", amount=500, past_success_rate=0.6),
            intent="offer_emi",
        )
        assert result.rule_applied != RULE_TIMEOUT_EMI_CORRECT
        assert result.final_intent == "offer_emi"


# ---------------------------------------------------------------------------
# Rule priority
# ---------------------------------------------------------------------------

class TestRulePriority:
    def test_fraud_takes_priority_over_high_risk(self):
        result = evaluate_action(
            {"error_code": "fraud_suspected", "amount": 50_000, "past_success_rate": 0.05},
            intent="retry_now",
        )
        assert result.final_intent == "escalate_to_human"
        assert result.rule_applied == RULE_FRAUD_ESCALATE

    def test_invalid_cvv_takes_priority_over_high_risk(self):
        result = evaluate_action(
            {"error_code": "invalid_cvv", "amount": 50_000, "past_success_rate": 0.05},
            intent="retry_now",
        )
        assert result.final_intent == "escalate_to_human"
        assert result.rule_applied == RULE_INVALID_CVV_ESCALATE


# ---------------------------------------------------------------------------
# Pass-through — no rules fire
# ---------------------------------------------------------------------------

class TestPassThrough:
    def test_clean_transaction_passes_intent_unchanged(self):
        result = evaluate_action(
            _tx(error_code="gateway_timeout", amount=500, past_success_rate=0.9),
            intent="retry_now",
        )
        assert result.final_intent == "retry_now"
        assert result.overridden is False
        assert result.rule_applied is None
        assert result.original_intent == "retry_now"

    def test_to_dict_shape(self):
        result = evaluate_action(_tx(), intent="retry_now")
        d = result.to_dict()
        assert set(d.keys()) == {"final_intent", "overridden", "rule_applied", "original_intent"}


# ---------------------------------------------------------------------------
# Edge cases — robustness
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_missing_keys_default_safely(self):
        result = evaluate_action({}, intent="retry_now")
        assert result.final_intent == "retry_now"
        assert result.overridden is False

    def test_error_code_is_case_insensitive(self):
        result = evaluate_action(_tx(error_code="FRAUD_SUSPECTED"), intent="retry_now")
        assert result.final_intent == "escalate_to_human"
        assert result.rule_applied == RULE_FRAUD_ESCALATE

    def test_result_is_immutable(self):
        result = evaluate_action(_tx(), intent="retry_now")
        with pytest.raises((AttributeError, TypeError)):
            result.final_intent = "offer_emi"  # type: ignore[misc]


if __name__ == "__main__":
    sys.exit(pytest.main(["-s", __file__]))
