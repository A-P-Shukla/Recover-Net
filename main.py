"""
main.py

FastAPI application for Recover-Net.
Includes:
- BlindLog ASGI Middleware attached to intercept and mask request/response logs.
- POST /webhook/payment-failure endpoint to ingest failure events with BlindLog hashing.
"""

import logging
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from blindlog.integrations.fastapi import BlindLogFastAPIMiddleware
from database import get_db
from models import Transaction
from security import get_blind_logger, require_blindlog_secret

# Configure logging to output at INFO level so BlindLog middleware logs to terminal
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("recover_net")

app = FastAPI(
    title="Recover-Net Payment Recovery API",
    description="Payment failure recovery pipeline with deterministic PII pseudonymization and guardrailed LLM decision engine.",
    version="0.1.0",
)

# Attach BlindLog middleware to intercept and mask request/response bodies and headers
app.add_middleware(
    BlindLogFastAPIMiddleware,
    blind_logger=get_blind_logger(),
)


@app.get("/health", status_code=status.HTTP_200_OK)
def health_check():
    return {"status": "ok", "service": "recover-net"}


@app.post("/webhook/payment-failure", status_code=status.HTTP_201_CREATED)
def payment_failure_webhook(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
):
    """
    Ingest a payment failure webhook event into the database with BlindLog PII hashing.
    """
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
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to process webhook: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error processing webhook: {str(e)}",
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
