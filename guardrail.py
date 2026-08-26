"""
guardrail.py

The Deterministic Guardrail for Recover-Net.

Validates the Groq classifier's intent recommendation against hard business rules
before any recovery action is executed. No ML, no probability — pure Python logic
that cannot hallucinate.

Usage:
    from guardrail import evaluate_action, GuardrailResult

    result = evaluate_action(transaction_data, groq_intent)
    # result.final_intent  -> the safe, validated action
    # result.overridden    -> True if the guardrail corrected the AI
    # result.rule_applied  -> name of the rule that fired, or None

Rule priority (highest → lowest):
    1. RULE_FRAUD_ESCALATE      — fraud_suspected always routes to human review.
    2. RULE_INVALID_CVV_ESCALATE — repeated CVV failures always route to human review.
    3. RULE_HIGH_RISK_ESCALATE  — large amount + poor history → human review.
    4. RULE_TIMEOUT_EMI_CORRECT — gateway timeout cannot offer EMI.

Notes:
    - Rules 1 and 2 only fire (override=True) when the LLM did NOT already
      propose escalate_to_human. If the LLM got it right, the rule still
      enforces the outcome but overridden=False to avoid inflating audit stats.
    - Rule 3 similarly sets overridden=False when the LLM already escalated.
    - A high-amount + low-history + gateway_timeout payload hits Rule 3 before
      Rule 4: escalating is the safer outcome and is acceptable.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from classifier import RecoveryIntent

# ---------------------------------------------------------------------------
# Rule identifiers — used in audit logs so every override is traceable
# ---------------------------------------------------------------------------
RULE_FRAUD_ESCALATE = "RULE_FRAUD_ESCALATE"
RULE_INVALID_CVV_ESCALATE = "RULE_INVALID_CVV_ESCALATE"
RULE_HIGH_RISK_ESCALATE = "RULE_HIGH_RISK_ESCALATE"
RULE_TIMEOUT_EMI_CORRECT = "RULE_TIMEOUT_EMI_CORRECT"

# Threshold constants — centralised so they're easy to tune
HIGH_RISK_AMOUNT_THRESHOLD = 10_000
HIGH_RISK_SUCCESS_RATE_CEILING = 0.2


@dataclass(frozen=True)
class GuardrailResult:
    """
    Immutable result returned by evaluate_action().

    Attributes:
        final_intent:    The validated, safe action to execute.
        overridden:      True when the guardrail changed the AI's proposed intent.
                         False when the AI was already correct (rule enforced, not corrected).
        rule_applied:    Identifier of the rule that fired, or None if no rule matched.
        original_intent: The intent proposed by the AI before guardrail evaluation.
    """

    final_intent: RecoveryIntent
    overridden: bool
    rule_applied: Optional[str]
    original_intent: RecoveryIntent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "final_intent": self.final_intent,
            "overridden": self.overridden,
            "rule_applied": self.rule_applied,
            "original_intent": self.original_intent,
        }


def evaluate_action(
    transaction_data: Dict[str, Any],
    intent: RecoveryIntent,
) -> GuardrailResult:
    """
    Apply deterministic safety rules to the AI-proposed intent.

    Rules are evaluated in priority order — the first match wins and the
    remaining rules are skipped to keep the logic unambiguous.

    Parameters:
        transaction_data: Raw or sanitized transaction dict.  Expected keys:
                          - error_code (str)
                          - amount (int | float | Decimal)
                          - past_success_rate (float | None)
        intent:           RecoveryIntent proposed by the Groq classifier.

    Returns:
        GuardrailResult with the final safe intent and override metadata.
    """
    error_code: str = str(transaction_data.get("error_code", "")).strip().lower()
    amount: float = float(transaction_data.get("amount", 0) or 0)
    past_success_rate: Optional[float] = transaction_data.get("past_success_rate")

    # ------------------------------------------------------------------
    # Rule 1 — Fraud suspected: never retry or offer EMI on a stolen card
    # overridden=True only when the LLM missed it; avoids inflating stats
    # when the LLM correctly escalated.
    # ------------------------------------------------------------------
    if error_code == "fraud_suspected":
        already_correct = intent == "escalate_to_human"
        return GuardrailResult(
            final_intent="escalate_to_human",
            overridden=not already_correct,
            rule_applied=RULE_FRAUD_ESCALATE,
            original_intent=intent,
        )

    # ------------------------------------------------------------------
    # Rule 2 — Invalid CVV: repeated card security failures go to human review
    # ------------------------------------------------------------------
    if error_code == "invalid_cvv":
        already_correct = intent == "escalate_to_human"
        return GuardrailResult(
            final_intent="escalate_to_human",
            overridden=not already_correct,
            rule_applied=RULE_INVALID_CVV_ESCALATE,
            original_intent=intent,
        )

    # ------------------------------------------------------------------
    # Rule 3 — High-value transaction with very poor success history
    # overridden=False when the LLM already chose escalate_to_human.
    # ------------------------------------------------------------------
    if (
        amount > HIGH_RISK_AMOUNT_THRESHOLD
        and past_success_rate is not None
        and past_success_rate < HIGH_RISK_SUCCESS_RATE_CEILING
    ):
        already_correct = intent == "escalate_to_human"
        return GuardrailResult(
            final_intent="escalate_to_human",
            overridden=not already_correct,
            rule_applied=RULE_HIGH_RISK_ESCALATE,
            original_intent=intent,
        )

    # ------------------------------------------------------------------
    # Rule 4 — Gateway timeout cannot result in an EMI offer
    # ------------------------------------------------------------------
    if error_code == "gateway_timeout" and intent == "offer_emi":
        return GuardrailResult(
            final_intent="retry_now",
            overridden=True,
            rule_applied=RULE_TIMEOUT_EMI_CORRECT,
            original_intent=intent,
        )

    # ------------------------------------------------------------------
    # No rule fired — AI decision is safe, pass it through unchanged
    # ------------------------------------------------------------------
    return GuardrailResult(
        final_intent=intent,
        overridden=False,
        rule_applied=None,
        original_intent=intent,
    )
