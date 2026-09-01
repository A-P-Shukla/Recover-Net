"""
api/webhooks.py

FastAPI routes for payment failure webhook ingestion and recovery pipeline.
Includes HMAC-SHA256 signature verification and BlindLog PII middleware.
"""

import hashlib
import hmac
import logging
import os
from typing import Any, Dict

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from recover_net.db.session import get_db
from recover_net.db.models import Transaction
from recover_net.core.security import MaskingError
from recover_net.engine.pipeline import RecoveryResult, run_recovery_pipeline

logger = logging.getLogger("recover_net.api")


def _verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> None:
    """
    Verify HMAC-SHA256 webhook signature.

    The caller must send:
        X-Webhook-Signature: sha256=<hex_digest>

    computed as:
        HMAC-SHA256(key=WEBHOOK_SECRET, msg=raw_request_body)

    Raises HTTP 401 if the signature is missing or invalid.
    Skips verification if WEBHOOK_SECRET is not set (development only —
    a warning is logged).
    """
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()

    if not webhook_secret:
        logger.error("WEBHOOK_SECRET is not set — rejecting request (fail-closed).")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error: WEBHOOK_SECRET is missing.",
        )

    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Webhook-Signature header.",
        )

    if not signature_header.startswith("sha256="):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Webhook-Signature must use sha256= prefix.",
        )

    expected = hmac.new(
        webhook_secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    provided = signature_header[len("sha256="):]

    if not hmac.compare_digest(expected, provided):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature.",
        )


async def ingest_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_webhook_signature: str | None = Header(default=None),
) -> Dict[str, Any]:
    """
    Ingest a payment failure webhook event into the database with BlindLog PII hashing.
    """
    raw_body = await request.body()
    _verify_webhook_signature(raw_body, x_webhook_signature)

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Request body must be valid JSON.",
        )

    try:
        tx = Transaction.from_webhook(payload)
        db.add(tx)
        db.commit()
        db.refresh(tx)
        return {
            "status": "success",
            "message": "Payment failure webhook ingested successfully",
            "transaction_id": str(tx.transaction_id),
            "source_transaction_id": tx.source_transaction_id,
            "masked_user_email": tx.user_email,
            "masked_phone": tx.phone,
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(e),
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Webhook event with transaction_id '{payload.get('transaction_id')}' has already been processed.",
        )
    except MaskingError as e:
        db.rollback()
        logger.error("Masking hard-stop on ingest: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PII masking failed — request rejected for security.",
        )
    except Exception as e:
        db.rollback()
        logger.error("Failed to process webhook: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error processing webhook.",
        )


async def recover_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_webhook_signature: str | None = Header(default=None),
) -> Dict[str, Any]:
    """
    Full recovery pipeline: ingest → Bedrock classify → guardrail → audit log.

    Returns the guardrail-validated recovery action and full decision provenance.
    """
    raw_body = await request.body()
    _verify_webhook_signature(raw_body, x_webhook_signature)

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Request body must be valid JSON.",
        )

    try:
        result: RecoveryResult = run_recovery_pipeline(payload, db)
        db.commit()
        return {
            "status": "success",
            "transaction_id": str(result.transaction_id),
            "audit_log_id": str(result.audit_log_id),
            "llm_proposed_intent": result.llm_intent,
            "llm_confidence": result.llm_confidence,
            "final_intent": result.final_intent,
            "final_status": result.final_status,
            "guardrail_overridden": result.overridden,
            "rule_applied": result.rule_applied,
            "action": result.action,
            "modified_parameters": result.modified_parameters,
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(e),
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Transaction '{payload.get('transaction_id')}' has already been processed.",
        )
    except MaskingError as e:
        db.rollback()
        logger.error("Masking hard-stop: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PII masking failed — request rejected for security.",
        )
    except Exception as e:
        db.rollback()
        logger.error("Recovery pipeline failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error.",
        )
