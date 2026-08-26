"""
main.py

FastAPI application for Recover-Net.
Includes:
- BlindLog ASGI Middleware attached to intercept and mask request/response logs.
- Lifespan startup: validates BLINDLOG_SECRET and GROQ_API_KEY before accepting traffic.
- HMAC-SHA256 webhook signature verification on all POST /webhook/* routes.
- POST /webhook/payment-failure          — ingest failure events (PII masked).
- POST /webhook/payment-failure/recover  — full pipeline: ingest → classify → guardrail → audit.
"""

import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from blindlog.integrations.fastapi import BlindLogFastAPIMiddleware
from database import get_db
from models import Transaction
from security import MaskingError, get_blind_logger, require_blindlog_secret
from workflow import RecoveryResult, run_recovery_pipeline

# Configure logging to output at INFO level so BlindLog middleware logs to terminal
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("recover_net")


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail closed: reject startup if required secrets are missing."""
    require_blindlog_secret()

    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Refusing to start without a valid Groq API key."
        )

    logger.info("Startup checks passed: BLINDLOG_SECRET and GROQ_API_KEY are set.")
    yield


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Recover-Net Payment Recovery API",
    description="Payment failure recovery pipeline with deterministic PII pseudonymization and guardrailed LLM decision engine.",
    version="0.1.0",
    lifespan=lifespan,
)

# Attach BlindLog middleware to intercept and mask request/response bodies and headers
app.add_middleware(
    BlindLogFastAPIMiddleware,
    blind_logger=get_blind_logger(),
)


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

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
        logger.warning(
            "WEBHOOK_SECRET is not set — skipping signature verification. "
            "Set WEBHOOK_SECRET before exposing this service beyond localhost."
        )
        return

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok", "service": "recover-net"}


@app.post("/webhook/payment-failure", status_code=status.HTTP_201_CREATED)
async def payment_failure_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_webhook_signature: str | None = Header(default=None),
):
    """
    Ingest a payment failure webhook event into the database with BlindLog PII hashing.
    """
    raw_body = await request.body()
    _verify_webhook_signature(raw_body, x_webhook_signature)

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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


@app.post("/webhook/payment-failure/recover", status_code=status.HTTP_201_CREATED)
async def payment_failure_recover(
    request: Request,
    db: Session = Depends(get_db),
    x_webhook_signature: str | None = Header(default=None),
):
    """
    Full recovery pipeline: ingest → Groq classify → guardrail → audit log.

    Returns the guardrail-validated recovery action and full decision provenance.
    """
    raw_body = await request.body()
    _verify_webhook_signature(raw_body, x_webhook_signature)

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
