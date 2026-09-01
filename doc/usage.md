# Usage Guide

This guide covers environment configuration, starting Recover-Net services, authenticating webhooks with HMAC signatures, executing concurrent batch simulations, and validating test suites.

---

## Documentation Navigation
- [Usage Guide](usage.md) (Current)
- [System Architecture](architecture.md) — Pipeline flow, component boundaries, and security design
- [Database Reference](database.md) — PostgreSQL schema, models, indexes, and audit logs
- [Conversation & Audit Log](../docs/CONVERSATION_LOG.md) — Chronological log of changes and decisions
- [Project Overview & Quickstart](../README.md) — Root documentation and Stripe-style reference

---

## Prerequisites & Environment Setup

Recover-Net requires:
1. Python 3.12+ and [uv](https://docs.astral.sh/uv/)
2. PostgreSQL 16 (local instance or Docker container)
3. An AWS Bedrock bearer token or API key — see [AWS Bedrock Console](https://console.aws.amazon.com/bedrock/)

### 1. Environment Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | `postgresql+psycopg2://postgres:postgres@localhost:5432/recover_net` | PostgreSQL connection string |
| `BLINDLOG_SECRET` | Yes | — | Secret key for deterministic PII pseudonymization |
| `OPENAI_API_KEY` | Yes | — | AWS Bedrock bearer token / API key |
| `OPENAI_BASE_URL` | Yes | — | Bedrock OpenAI-compatible endpoint (e.g. `https://bedrock-mantle.ap-southeast-2.api.aws/v1`) |
| `BEDROCK_MODEL` | No | `mistral.ministral-3-8b-instruct` | Bedrock model ID for classification |
| `WEBHOOK_SECRET` | Yes | — | HMAC-SHA256 secret key for inbound request signing |

> **Fail-Closed Guarantee**: The FastAPI application validates that `BLINDLOG_SECRET`, `OPENAI_API_KEY`, and `WEBHOOK_SECRET` are non-empty at startup. If any secret is missing, server startup aborts immediately.

---

## Starting the Application

### Option A: Local Development

```bash
# 1. Start PostgreSQL via Docker Compose
docker compose up -d postgres

# 2. Run database migrations
uv run alembic upgrade head

# 3. Start FastAPI with Uvicorn
uv run uvicorn recover_net.core.app:app --reload --port 8000
```

### Option B: Full Docker Stack

```bash
docker compose up --build
```

Verify service liveness:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{ "status": "ok", "service": "recover-net" }
```

---

## Sending Authenticated Webhooks

All webhook endpoints require an HMAC-SHA256 signature passed in the `X-Webhook-Signature` header.

### Signature Computation (Python)

```python
import hashlib
import hmac
import json
import requests

secret = "your-webhook-secret"
payload = {
    "transaction_id": "txn_sample_123",
    "user_email": "customer@example.com",
    "phone": "+91-9876543210",
    "merchant_id": "default",
    "amount": 4500.00,
    "error_code": "gateway_timeout",
    "past_success_rate": 0.85,
}

raw_bytes = json.dumps(payload).encode("utf-8")
signature = hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()

headers = {
    "Content-Type": "application/json",
    "X-Webhook-Signature": f"sha256={signature}",
}

response = requests.post(
    "http://localhost:8000/webhook/payment-failure/recover",
    data=raw_bytes,
    headers=headers,
)
print(response.status_code, response.json())
```

---

## Webhook Endpoints

### 1. Ingest Only (`POST /webhook/payment-failure`)

Stores the transaction with pseudonymized PII. Does not trigger LLM classification or guardrail evaluation.

**Response `201 Created`**:

```json
{
  "status": "success",
  "message": "Payment failure webhook ingested successfully",
  "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "source_transaction_id": "txn_sample_123",
  "masked_user_email": "blnd_ref_8f3a1b...",
  "masked_phone": "blind:5a3143..."
}
```

### 2. Full Recovery Pipeline (`POST /webhook/payment-failure/recover`)

Executes the complete pipeline: PII masking → Bedrock classification → Dynamic guardrail → Audit ledger commit.

**Response `201 Created`**:

```json
{
  "status": "success",
  "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "audit_log_id": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
  "llm_proposed_intent": "offer_emi",
  "llm_confidence": 0.94,
  "final_intent": "offer_emi",
  "final_status": "EMI_OFFERED",
  "guardrail_overridden": false,
  "rule_applied": "RULE_DISCOUNT_CLAMP",
  "action": "MODIFIED",
  "modified_parameters": {
    "parameter": "discount",
    "proposed": 15.0,
    "applied": 10.0,
    "max_allowed": 10.0
  }
}
```

---

## Batch Concurrency Engine

Use `scripts/batch_runner.py` to blast concurrent transactions, test throughput, and evaluate guardrail intercepts. AWS Bedrock's on-demand quota is ~10,000 RPM — no client-side rate limiting is applied.

```bash
# 1. Generate a synthetic test dataset (75 records: 60% standard, 20% high-risk, 20% fraud)
uv run python scripts/generate_batch.py

# 2. Run the concurrent batch runner (signs requests with $WEBHOOK_SECRET automatically)
uv run python scripts/batch_runner.py --concurrency 20

# 3. Custom options
uv run python scripts/batch_runner.py \
  --url http://localhost:8000 \
  --concurrency 20 \
  --secret your-webhook-secret \
  --report-json batch_results.json
```

**Expected throughput**: 75 transactions at `--concurrency 20` complete in roughly 12–20 seconds.

**CLI flags**:

| Flag | Default | Description |
|---|---|---|
| `--url` | `http://localhost:8000` | Base URL of the FastAPI server |
| `--batch` | `batch_payload.json` | Path to the batch JSON file |
| `--concurrency` | `20` | Maximum simultaneous in-flight requests |
| `--secret` | `$WEBHOOK_SECRET` | HMAC-SHA256 signing secret |
| `--report-json` | — | Optional path to write full results as JSON |

---

## Running Automated Tests

Run the complete test suite:

```bash
uv run pytest
```

Execute specific test modules:

```bash
uv run pytest tests/test_classifier.py   # Bedrock classifier + schema validation
uv run pytest tests/test_guardrail.py    # Deterministic guardrail rules
uv run pytest tests/test_webhook.py      # HMAC auth + ingest endpoint
uv run pytest tests/test_workflow.py     # Full pipeline + FastAPI integration
uv run pytest tests/test_security.py     # BlindLog PII masking
uv run pytest tests/test_models.py       # SQLAlchemy ORM + PII column validation
```

All 88 tests cover: classifier schemas, PII sanitization, model boundaries, guardrail rules, HMAC authentication, and full pipeline integration.

---

## Cross-Document References
- [System Architecture](architecture.md)
- [Database Reference](database.md)
- [Conversation Log](../docs/CONVERSATION_LOG.md)
- [Root README](../README.md)
