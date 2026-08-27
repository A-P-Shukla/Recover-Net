"""
engine/pipeline.py

The Orchestrator — central nervous system of Recover-Net.

Wires Phase 1 (Groq classifier) to Phase 2 (deterministic guardrail) and
writes an immutable audit log entry for every decision made.

Pipeline:
    raw_payload
        → BlindLog sanitization          (PII destroyed, GDPR/DPDP compliant)
        → classify_payment_failure()     (Groq LPU, <200ms)
        → evaluate_action()              (deterministic guardrail, pure Python)
        → AuditLog INSERT                (immutable ledger write)
        → RecoveryResult

Usage:
    from recover_net.engine.pipeline import run_recovery_pipeline, RecoveryResult

    result = run_recovery_pipeline(raw_payload, db_session)
    # result.final_intent      -> safe action to execute
    # result.overridden        -> True if guardrail corrected the AI
    # result.rule_applied      -> which rule fired, or None
    # result.llm_intent        -> what the AI originally proposed
    # result.transaction_id    -> internal UUID of the ingested transaction
    # result.audit_log_id      -> UUID of the written audit log row
"""

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from recover_net.llm.classifier import RecoveryDecision, RecoveryIntent, classify_payment_failure
from recover_net.guardrails.engine import GuardrailResult, evaluate_action
from recover_net.db.models import AuditLog, MerchantPolicy, Transaction

logger = logging.getLogger("recover_net.engine")

# Maps RecoveryIntent values to the final_status labels stored in audit_logs
INTENT_TO_STATUS: Dict[str, str] = {
    "retry_now": "RETRIED",
    "offer_emi": "EMI_OFFERED",
    "escalate_to_human": "ESCALATED",
}

_INTENT_TO_STATUS = INTENT_TO_STATUS


@dataclass(frozen=True)
class RecoveryResult:
    """
    Immutable summary of a completed recovery pipeline run.

    Attributes:
        transaction_id:  Internal UUID of the ingested Transaction row.
        audit_log_id:    UUID of the written AuditLog row.
        llm_intent:      Raw intent proposed by the Groq classifier.
        llm_confidence:  Confidence score from the classifier (0.0–1.0).
        final_intent:    Guardrail-validated action to execute.
        overridden:      True when the guardrail corrected the AI.
        rule_applied:    Identifier of the guardrail rule that fired, or None.
        final_status:    Human-readable status label written to audit_logs.
    """

    transaction_id: uuid.UUID
    audit_log_id: uuid.UUID
    llm_intent: RecoveryIntent
    llm_confidence: float
    final_intent: RecoveryIntent
    overridden: bool
    rule_applied: Optional[str]
    final_status: str
    action: str = "APPROVED"
    modified_parameters: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "transaction_id": str(self.transaction_id),
            "audit_log_id": str(self.audit_log_id),
            "llm_intent": self.llm_intent,
            "llm_confidence": self.llm_confidence,
            "final_intent": self.final_intent,
            "overridden": self.overridden,
            "rule_applied": self.rule_applied,
            "final_status": self.final_status,
        }
        if self.action != "APPROVED":
            result["action"] = self.action
            result["modified_parameters"] = self.modified_parameters
        return result


def run_recovery_pipeline(
    raw_payload: Dict[str, Any],
    db: Session,
    groq_client: Optional[Any] = None,
    groq_model: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> RecoveryResult:
    """
    Execute the full Recover-Net pipeline for a single payment failure event.

    Steps:
        1. Ingest raw payload → Transaction row (BlindLog masks PII).
        2. Sanitize payload again for the LLM call (no PII leaves the system).
        3. Call Groq classifier → RecoveryDecision.
        4. Apply deterministic guardrail → GuardrailResult.
        5. Write AuditLog row with full decision provenance.
        6. Caller commits atomically at the HTTP boundary.

    Parameters:
        raw_payload:  Raw inbound webhook dictionary.
        db:           SQLAlchemy Session (caller manages lifecycle).
        groq_client:  Optional Groq client override (for testing/mocking).
        groq_model:   Optional model ID override.
        secret_key:   Optional BlindLog secret key override.

    Returns:
        RecoveryResult with the full decision provenance.

    Raises:
        ValueError:    Payload is missing required fields.
        RuntimeError:  Groq API returned an unusable response.
        MaskingError:  PII could not be masked (security hard-stop).
    """
    # ------------------------------------------------------------------
    # Step 1 — Ingest: persist the transaction with masked PII
    # ------------------------------------------------------------------
    tx = Transaction.from_webhook(raw_payload, secret_key=secret_key)
    db.add(tx)
    db.flush()  # Assign tx.transaction_id without committing yet

    logger.info(
        "Transaction ingested: id=%s source_id=%s error_code=%s amount=%s",
        tx.transaction_id,
        tx.source_transaction_id,
        tx.error_code,
        tx.amount,
    )

    # ------------------------------------------------------------------
    # Step 2 — Classify: Groq LPU inference (<200ms)
    # classify_payment_failure() calls mask_payload() internally —
    # we pass raw_payload so it sanitizes exactly once.
    # ------------------------------------------------------------------
    groq_decision: RecoveryDecision = classify_payment_failure(
        raw_payload,
        client=groq_client,
        model=groq_model,
        secret_key=secret_key,
    )

    logger.info(
        "Groq decision: intent=%s confidence=%.2f",
        groq_decision.intent,
        groq_decision.confidence,
    )

    # ------------------------------------------------------------------
    # Step 3 — Guardrail: deterministic safety validation
    # ------------------------------------------------------------------
    guardrail_input: Dict[str, Any] = {
        "error_code": raw_payload.get("error_code", ""),
        "amount": raw_payload.get("amount", 0),
        "past_success_rate": raw_payload.get("past_success_rate"),
    }
    merchant_id = tx.merchant_id
    merchant_policy = db.get(MerchantPolicy, merchant_id)
    max_discount_allowed = float(
        merchant_policy.max_discount_allowed
        if merchant_policy is not None
        else 10.0
    )
    guardrail_result: GuardrailResult = evaluate_action(
        guardrail_input,
        groq_decision.intent,
        proposed_discount=groq_decision.discount,
        max_discount_allowed=max_discount_allowed,
    )

    if guardrail_result.overridden:
        logger.warning(
            "Guardrail OVERRIDE: rule=%s llm=%s -> final=%s",
            guardrail_result.rule_applied,
            guardrail_result.original_intent,
            guardrail_result.final_intent,
        )
    else:
        logger.info("Guardrail PASS: intent=%s", guardrail_result.final_intent)

    # ------------------------------------------------------------------
    # Step 4 — Audit: immutable ledger write
    # ------------------------------------------------------------------
    final_status = INTENT_TO_STATUS.get(
        guardrail_result.final_intent, guardrail_result.final_intent.upper()
    )

    audit_log = AuditLog(
        log_id=uuid.uuid4(),
        transaction_id=tx.transaction_id,
        llm_proposed_action=json.dumps(groq_decision.to_dict()),
        guardrail_decision=json.dumps(guardrail_result.to_dict()),
        action=guardrail_result.action,
        modified_parameters=guardrail_result.modified_parameters,
        final_status=final_status,
    )
    db.add(audit_log)

    # ------------------------------------------------------------------
    # Step 5 — Stage complete: caller commits atomically
    # db.commit() is intentionally NOT called here. Commit responsibility
    # belongs to the HTTP boundary (api/webhooks.py) so the session lifecycle
    # and rollback behaviour are controlled in one place.
    # ------------------------------------------------------------------
    db.refresh(tx)
    db.refresh(audit_log)

    logger.info(
        "Audit log written: log_id=%s final_status=%s overridden=%s",
        audit_log.log_id,
        final_status,
        guardrail_result.overridden,
    )

    return RecoveryResult(
        transaction_id=tx.transaction_id,
        audit_log_id=audit_log.log_id,
        llm_intent=groq_decision.intent,
        llm_confidence=groq_decision.confidence,
        final_intent=guardrail_result.final_intent,
        overridden=guardrail_result.overridden,
        rule_applied=guardrail_result.rule_applied,
        final_status=final_status,
        action=guardrail_result.action,
        modified_parameters=guardrail_result.modified_parameters,
    )
