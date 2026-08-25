# Recover-Net

Recover-Net is a payment failure recovery API that combines a Groq-powered LLM classifier with a deterministic guardrail engine to triage failed transactions and route them to the right recovery action — automatically.

Every decision is immutably logged. No PII ever leaves the system unmasked.

**Docs**
- [System Architecture](doc/architecture.md) — pipeline stages, data flow, security boundaries, module graph
- [Usage Guide](doc/usage.md) — running the stack, payload reference, batch processing, integration patterns
- [Database Reference](doc/database.md) — schema, columns, indexes, migrations, example queries

---

## How it works

A payment failure event arrives as a webhook. Recover-Net runs it through a four-stage pipeline:

```
Inbound webhook
    → BlindLog PII masking          (email + phone pseudonymized before any processing)
    → Groq LLM classifier           (intent: retry_now | offer_emi | escalate_to_human)
    → Deterministic guardrail       (hard rules override the LLM when it cannot be trusted)
    → Immutable audit log write     (full decision provenance committed atomically)
```

The guardrail is pure Python — no probability, no hallucination risk. It runs after the LLM on every request and has the final say.

---

## Quickstart

**Requirements:** Python 3.12+, PostgreSQL, a [Groq API key](https://console.groq.com), and [uv](https://docs.astral.sh/uv/).

**1. Clone and install**

```bash
git clone https://github.com/your-org/recover-net.git
cd recover-net
uv sync
```

**2. Configure environment**

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `BLINDLOG_SECRET` | Secret key for deterministic PII pseudonymization |
| `GROQ_API_KEY` | Your Groq API key |
| `GROQ_MODEL_ID` | Model ID (default: `openai/gpt-oss-20b`) |

**3. Run database migrations**

```bash
uv run alembic upgrade head
```

**4. Start the server**

```bash
uv run uvicorn main:app --reload
```

The API is now running at `http://localhost:8000`.

---

## API reference

### Health check

```
GET /health
```

Returns the service status.

**Response**

```json
{
  "status": "ok",
  "service": "recover-net"
}
```

---

### Ingest a payment failure

```
POST /webhook/payment-failure
```

Stores the event in the database with PII masked. Does not run classification or guardrail logic.

**Request body**

```json
{
  "transaction_id": "txn_abc123",
  "user_email": "user@example.com",
  "phone": "+91-9876543210",
  "amount": 4999.00,
  "error_code": "insufficient_funds",
  "past_success_rate": 0.82
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `transaction_id` | string | No | Your external transaction ID. Stored as `source_transaction_id`. |
| `user_email` | string | Yes | User's email. Pseudonymized before storage. |
| `phone` | string | Yes | User's phone number. Pseudonymized before storage. |
| `amount` | number | Yes | Transaction amount. |
| `error_code` | string | Yes | Failure reason (e.g. `insufficient_funds`, `gateway_timeout`, `fraud_suspected`). |
| `past_success_rate` | number | No | Historical payment success rate for this user (0.0–1.0). |

**Response** `201 Created`

```json
{
  "status": "success",
  "message": "Payment failure webhook ingested successfully",
  "transaction_id": "a1b2c3d4-...",
  "source_transaction_id": "txn_abc123",
  "masked_user_email": "blnd_e3f7a...",
  "masked_phone": "blnd_91c4b..."
}
```

---

### Run the full recovery pipeline

```
POST /webhook/payment-failure/recover
```

Runs the complete pipeline: ingest → classify → guardrail → audit log. Returns the final recovery action with full decision provenance.

**Request body**

Same schema as `POST /webhook/payment-failure`.

**Response** `201 Created`

```json
{
  "status": "success",
  "transaction_id": "a1b2c3d4-...",
  "audit_log_id": "f9e8d7c6-...",
  "llm_proposed_intent": "offer_emi",
  "llm_confidence": 0.91,
  "final_intent": "escalate_to_human",
  "final_status": "ESCALATED",
  "guardrail_overridden": true,
  "rule_applied": "RULE_HIGH_RISK_ESCALATE"
}
```

| Field | Type | Description |
|---|---|---|
| `transaction_id` | string | Internal UUID of the stored transaction. |
| `audit_log_id` | string | UUID of the written audit log entry. |
| `llm_proposed_intent` | string | What the LLM originally recommended. |
| `llm_confidence` | number | LLM confidence score (0.0–1.0). |
| `final_intent` | string | The validated action to execute. |
| `final_status` | string | Status label written to the audit log. |
| `guardrail_overridden` | boolean | `true` if the guardrail corrected the LLM. |
| `rule_applied` | string \| null | The guardrail rule that fired, or `null` if the LLM was correct. |

---

## Recovery intents

| Intent | Meaning |
|---|---|
| `retry_now` | Transient failure — retry the charge immediately. |
| `offer_emi` | Insufficient funds on a high-value transaction — offer a payment plan. |
| `escalate_to_human` | Suspected fraud, repeated CVV failures, or very poor customer history. |

---

## Guardrail rules

The guardrail evaluates rules in priority order. The first match wins.

| Rule | Condition | Override |
|---|---|---|
| `RULE_FRAUD_ESCALATE` | `error_code == "fraud_suspected"` and LLM did not propose escalation | Forces `escalate_to_human` |
| `RULE_HIGH_RISK_ESCALATE` | `amount > 10,000` and `past_success_rate < 0.2` | Forces `escalate_to_human` |
| `RULE_TIMEOUT_EMI_CORRECT` | `error_code == "gateway_timeout"` and LLM proposed `offer_emi` | Corrects to `retry_now` |

If no rule fires, the LLM's decision passes through unchanged.

---

## Error responses

Recover-Net returns standard HTTP error codes with a `detail` field.

| Status | Cause |
|---|---|
| `409 Conflict` | A transaction with this `transaction_id` has already been processed. |
| `422 Unprocessable Entity` | The request body is missing required fields. |
| `500 Internal Server Error` | PII masking failed, or an unexpected error occurred. A masking failure is a hard stop — the request is rejected rather than stored with unmasked data. |

**Example error**

```json
{
  "detail": "Webhook event with transaction_id 'txn_abc123' has already been processed."
}
```

---

## Batch processing

For load testing or bulk replay, use the included batch runner. It fires transactions concurrently using `asyncio` + `aiohttp` and prints a summary report.

**Generate a test batch**

```bash
uv run python generate_batch.py
```

This creates `batch_payload.json` with a mix of normal, fraud, high-risk, and timeout transactions.

**Run the batch**

```bash
uv run python batch_runner.py
```

```bash
# Custom target and concurrency
uv run python batch_runner.py --url http://localhost:8000 --concurrency 10

# Write full results to a JSON file
uv run python batch_runner.py --report-json batch_results.json
```

**Sample output**

```
=====================================================
      RECOVER-NET: BATCH EXECUTION REPORT
=====================================================
  Total Failed Transactions Processed : 25
  Total Value at Risk                  : ₹4,87,500
  Processing Time                      : 6.43 seconds
  Average Latency per Webhook          : ~257 ms (via Groq LPUs)

  --- REVENUE RECOVERED ---
  Recovered via Automated Retry        : 12 (₹1,20,000)
  Recovered via EMI Intervention       : 5 (₹87,500)
  Total Revenue Secured                : ₹2,07,500 (42.6%)

  --- COMPLIANCE & ESCALATION ---
  Escalated to Human Review            : 8
  Fraud Attempts Blocked by Guardrail  : 5
  PII Leaks Detected in Logs           : 0 (Secured via BlindLog)
=====================================================
```

---

## PII and security

All email addresses and phone numbers are pseudonymized using [BlindLog](https://pypi.org/project/blindlog/) before any data is written to the database, passed to the LLM, or logged.

- Masking is **deterministic** — the same input always produces the same token, so you can correlate records without ever storing the raw value.
- Masking is a **hard stop** — if BlindLog cannot mask a field, the request is rejected with a `500` error. Recover-Net never stores unmasked PII.
- `BLINDLOG_SECRET` is required at startup. The server refuses to run without it.
- `BLINDLOG_DEBUG=true` is explicitly blocked in production paths.

---

## Database schema

**`transactions`**

| Column | Type | Notes |
|---|---|---|
| `transaction_id` | UUID | Internal primary key. Always generated server-side. |
| `source_transaction_id` | string | Your external transaction ID. Unique. |
| `user_email` | string | BlindLog-pseudonymized email. |
| `phone` | string | BlindLog-pseudonymized phone. |
| `amount` | decimal | Transaction amount. |
| `error_code` | string | Failure reason from the webhook. |
| `past_success_rate` | float | Historical success rate (nullable). |
| `raw_payload` | JSON | Full webhook payload with PII masked. |
| `created_at` | timestamp | Ingestion time (server default). |

**`audit_logs`**

| Column | Type | Notes |
|---|---|---|
| `log_id` | UUID | Primary key. |
| `transaction_id` | UUID | Foreign key to `transactions`. |
| `llm_proposed_action` | text | Serialized `RecoveryDecision` JSON. |
| `guardrail_decision` | text | Serialized `GuardrailResult` JSON. |
| `final_status` | string | `RETRIED`, `EMI_OFFERED`, or `ESCALATED`. |
| `timestamp` | timestamp | Decision time (server default). |

---

## Running tests

```bash
uv run pytest
```

Tests cover the classifier, guardrail, models, security layer, webhook endpoints, and the full pipeline.

---

## Tech stack

| Component | Technology |
|---|---|
| API framework | [FastAPI](https://fastapi.tiangolo.com) |
| LLM inference | [Groq](https://groq.com) (structured JSON output, sub-200ms) |
| PII masking | [BlindLog](https://pypi.org/project/blindlog/) |
| Database | PostgreSQL via SQLAlchemy 2.0 |
| Migrations | Alembic |
| Async HTTP | aiohttp |
| Runtime | Python 3.12+, [uv](https://docs.astral.sh/uv/) |
