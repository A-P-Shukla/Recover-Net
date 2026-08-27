"""
core/app.py

FastAPI application factory: lifespan, middleware, and route registration.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, status

from blindlog.integrations.fastapi import (  # pyright: ignore[reportMissingTypeStubs]
    BlindLogFastAPIMiddleware,
)

from recover_net.core.security import get_blind_logger, require_blindlog_secret
from recover_net.api.webhooks import ingest_webhook, recover_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("recover_net")


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


def create_app() -> FastAPI:
    """Construct and return the configured FastAPI application."""
    app = FastAPI(
        title="Recover-Net Payment Recovery API",
        description=(
            "Payment failure recovery pipeline with deterministic PII pseudonymization "
            "and guardrailed LLM decision engine."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # Attach BlindLog middleware — masks all request/response bodies in logs
    app.add_middleware(
        BlindLogFastAPIMiddleware,
        blind_logger=get_blind_logger(),
    )

    # Health check
    @app.get("/health", status_code=status.HTTP_200_OK)
    def health_check():
        return {"status": "ok", "service": "recover-net"}

    # Webhook routes
    app.post("/webhook/payment-failure", status_code=status.HTTP_201_CREATED)(
        ingest_webhook
    )
    app.post("/webhook/payment-failure/recover", status_code=status.HTTP_201_CREATED)(
        recover_webhook
    )

    return app


app = create_app()
