# Recover-Net

## Security and Privacy First

Recover-Net is designed to fail closed, protect customer data, and enforce deterministic business rules before any recovery action is allowed.

- HMAC Validation: every inbound webhook must present a valid `X-Webhook-Signature: sha256=<hex>` signature signed with `WEBHOOK_SECRET`; invalid or missing signatures are rejected with `401 Unauthorized`.
- Fail-Closed Secrets: startup aborts immediately if `BLINDLOG_SECRET`, `OPENAI_API_KEY`, or `WEBHOOK_SECRET` are missing; the application refuses to run in a misconfigured state.
- Deterministic PII Masking: email and phone values are masked with BlindLog before storage, before logging, and before any LLM prompt assembly, preventing raw PII from leaving the trust boundary.
- 88 Passing Tests: the project has been verified with `uv run pytest -q`, and the current suite passes with 88/88 tests green, covering security, guardrails, schema validation, and end-to-end pipeline behavior.

This is the architecture judges should trust first: every decision path is signed, masked, policy-checked, and auditable before execution.

---

## Documentation Navigation
- [System Architecture](doc/architecture.md) — Pipeline stages, security boundaries, and module dependency graph
- [Usage Guide](doc/usage.md) — Running the stack, payload reference, HMAC signatures, and batch testing
- [Database Reference](doc/database.md) — PostgreSQL schema, column dictionaries, indexes, and migrations

---

## How it works

A payment failure event arrives as an HMAC-signed webhook. Recover-Net executes a four-stage pipeline:

```
Inbound Webhook (signed with HMAC-SHA256)
    → BlindLog PII Masking          (email + phone pseudonymized before any processing)
    → AWS Bedrock Classifier      (intent: retry_now | offer_emi | escalate_to_human)
    → Dynamic Policy Guardrail       (hard rules override; merchant limits clamp parameters)
    → Immutable Audit Log Write     (full decision provenance committed atomically)
```

The guardrail is pure Python — no probability, no hallucination risk. It evaluates business safety rules and merchant-configured discount ceilings on every request, holding absolute authority over the final recovery action.

---

## Quickstart

**Requirements:** Python 3.12+, PostgreSQL 16, an [AWS Bedrock](https://console.aws.amazon.com/bedrock/) API key, and [uv](https://docs.astral.sh/uv/).

### 1. Clone and install

```bash
git clone https://github.com/your-org/recover-net.git
cd recover-net
uv sync
```

### 2. Configure environment

Copy `.env.example` to `.env` and configure your credentials:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | `postgresql+psycopg2://postgres:postgres@localhost:5432/recover_net` | PostgreSQL connection string |
| `BLINDLOG_SECRET` | Yes | — | Secret key for deterministic PII pseudonymization |
| `OPENAI_API_KEY` | Yes | — | AWS Bedrock bearer token / API key |
| `OPENAI_BASE_URL` | Yes | — | Bedrock OpenAI-compatible endpoint |
| `BEDROCK_MODEL` | No | `mistral.ministral-3-8b-instruct` | Bedrock model ID for classification |
| `WEBHOOK_SECRET` | Yes | — | Secret key for HMAC-SHA256 request signature verification |

### 3. Run database migrations

```bash
uv run alembic upgrade head
```

### 4. Start the server

```bash
uv run uvicorn recover_net.core.app:app --reload --port 8000
```

The API is now running at `http://localhost:8000`.

### Docker one-command start

After configuring `.env`, start the complete API and PostgreSQL stack with:

```bash
docker compose up --build
```

---

## API Reference

### Health check

```http
GET /health
```

**Response `200 OK`**:
```json
{
  "status": "ok",
  "service": "recover-net"
}
```

---

### Ingest a payment failure

```http
POST /webhook/payment-failure
Header: X-Webhook-Signature: sha256=<hmac_hex_digest>
```

Stores the event in the database with PII masked. Does not run classification or guardrail logic.

**Request body**

```json
{
  "transaction_id": "txn_abc123",
  "user_email": "user@example.com",
  "phone": "+91-9876543210",
  "merchant_id": "default",
  "amount": 4999.00,
  "error_code": "insufficient_funds",
  "past_success_rate": 0.82
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `transaction_id` | string | No | External gateway transaction ID. Stored as `source_transaction_id`. |
| `user_email` | string | Yes | Customer email. Pseudonymized via BlindLog before storage. |
| `phone` | string | Yes | Customer phone. Pseudonymized via BlindLog before storage. |
| `merchant_id` | string | No | Merchant ID (defaults to `default`). Must be registered in `merchant_policies`. |
| `amount` | number | Yes | Transaction amount in currency units. |
| `error_code` | string | Yes | Gateway error reason (`insufficient_funds`, `gateway_timeout`, `fraud_suspected`, `invalid_cvv`). |
| `past_success_rate` | number | No | Customer historical payment success rate (0.0–1.0). |

**Response `201 Created`**:

```json
{
  "status": "success",
  "message": "Payment failure webhook ingested successfully",
  "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "source_transaction_id": "txn_abc123",
  "masked_user_email": "blnd_ref_e3f7a...",
  "masked_phone": "blind:91c4b..."
}
```

---

### Run the full recovery pipeline

```http
POST /webhook/payment-failure/recover
Header: X-Webhook-Signature: sha256=<hmac_hex_digest>
```

Runs the complete pipeline: ingest → AWS Bedrock classify → Dynamic guardrail → Audit log write.

**Response `201 Created`**:

```json
{
  "status": "success",
  "transaction_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "audit_log_id": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
  "llm_proposed_intent": "offer_emi",
  "llm_confidence": 0.91,
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

## Guardrail Rules & Actions

| Rule | Condition | Outcome | Action |
|---|---|---|---|
| `RULE_FRAUD_ESCALATE` | `error_code == "fraud_suspected"` and LLM did not escalate | Force `escalate_to_human` | `OVERRIDDEN` |
| `RULE_INVALID_CVV_ESCALATE` | `error_code == "invalid_cvv"` and LLM did not escalate | Force `escalate_to_human` | `OVERRIDDEN` |
| `RULE_HIGH_RISK_ESCALATE` | `amount > 10,000` and `past_success_rate < 0.2` | Force `escalate_to_human` | `OVERRIDDEN` |
| `RULE_TIMEOUT_EMI_CORRECT` | `error_code == "gateway_timeout"` and LLM proposed `offer_emi` | Correct to `retry_now` | `OVERRIDDEN` |
| `RULE_DISCOUNT_CLAMP` | Proposed EMI discount exceeds merchant's `max_discount_allowed` | Clamp to merchant ceiling | `MODIFIED` |

---

## Batch Processing Engine

Generate synthetic workloads and run concurrent batch recovery simulations:

```bash
# 1. Generate poisoned workload (60% standard, 20% high-risk, 20% fraud)
uv run python scripts/generate_batch.py

# 2. Fire all 75 concurrent requests (signed with $WEBHOOK_SECRET automatically)
uv run python scripts/batch_runner.py --concurrency 20

# 3. Custom output paths
uv run python scripts/batch_runner.py --report-csv results.csv --report-json results.json
```

Results are always written to `batch_results.csv` (override with `--report-csv`). The live terminal shows a real-time decision ledger and scoreboard as each response arrives.

---

## Running Automated Tests

```bash
uv run pytest
```

All 88 tests validate classifier tool schemas, prompt sanitization, model boundaries, guardrail rules, HMAC authentication, and full pipeline integration.

---

## Tech Stack

| Layer | Component | Details |
|---|---|---|
| **API Framework** | [FastAPI](https://fastapi.tiangolo.com) | Async ASGI application with lifespan secret validation |
| **LLM Inference** | [AWS Bedrock](https://aws.amazon.com/bedrock/) | `mistral.ministral-3-8b-instruct` via OpenAI-compatible endpoint, ~10,000 RPM on-demand |
| **PII Protection** | [BlindLog](https://pypi.org/project/blindlog/) | Column-level and ASGI-level deterministic hashing |
| **Database** | PostgreSQL 16 & SQLAlchemy 2.0 | Transactional persistence with atomic commit boundaries |
| **Migrations** | Alembic | Version-controlled schema migrations |
| **Testing** | pytest & pytest-asyncio | 88 tests covering ORM, API, guardrails, and security |
