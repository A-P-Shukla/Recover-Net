# Usage Guide

This guide covers how to run Recover-Net, when to use each endpoint, how to send payloads, and how to interpret responses.

---

## Before you start

Recover-Net needs three things running before any API call will work:

1. PostgreSQL (via Docker or your own instance)
2. The FastAPI server
3. A valid `.env` file

If any one of these is missing, requests will either fail to connect or return a 500 error at startup.

---

## Starting the stack

**Start the complete stack**

The included Docker setup builds the API image, starts PostgreSQL, applies all Alembic migrations, and launches Uvicorn:

```bash
docker compose up --build
```

The API is available at `http://localhost:8000`. Stop the stack with `Ctrl+C`, or run `docker compose down` from another terminal.

**Start PostgreSQL only**

```bash
docker compose up -d
```

Verify it's healthy:

```bash
docker ps
```

You should see `recover_net_postgres` with status `healthy`.

**Apply migrations**

Only needed on first run or after a schema change:

```bash
uv run alembic upgrade head
```

**Start the API server**

```bash
uv run uvicorn recover_net.core.app:app --reload --port 8000
```

The server is ready when you see:

```
INFO:     Application startup complete.
```

Verify with a health check:

```bash
curl http://localhost:8000/health
```

```json
{ "status": "ok", "service": "recover-net" }
```

---

## Environment variables

Copy `.env.example` to `.env` and set these before starting anything:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string. Default: `postgresql+psycopg2://postgres:postgres@localhost:5432/recover_net` |
| `BLINDLOG_SECRET` | Yes | Secret key for PII pseudonymization. Must be a non-empty string. Never hardcode this. |
| `GROQ_API_KEY` | Yes | Your Groq API key from [console.groq.com](https://console.groq.com) |
| `GROQ_MODEL_ID` | No | Model to use for classification. Default: `openai/gpt-oss-20b` |

The server will refuse to start if `BLINDLOG_SECRET` is missing or empty.

---

## Sending a payment failure event

### When to call which endpoint

Use `POST /webhook/payment-failure` when you want to store an event for later processing or audit purposes without triggering classification.

Use `POST /webhook/payment-failure/recover` when you want an immediate recovery decision. This is the primary endpoint — it runs the full pipeline and tells you what to do with the failed transaction.

### Payload structure

Both endpoints accept the same payload shape:

```json
{
  "transaction_id": "txn_abc123",
  "user_email": "user@example.com",
  "phone": "+919876543210",
  "merchant_id": "merchant-acme",
  "amount": 4999.00,
  "error_code": "insufficient_funds",
  "past_success_rate": 0.82
}
```

**`transaction_id`** — Your own identifier for this transaction. Stored as `source_transaction_id` in the database. If you send the same value twice, the second request returns HTTP 409. You can omit this field if you don't have an external ID.

**`merchant_id`** — Optional policy key. Defaults to `default`. The merchant policy controls the maximum EMI discount the guardrail may apply.

**`user_email`** and **`phone`** — These are required and are immediately pseudonymized. The raw values are never stored or logged.

**`amount`** — The transaction amount in your local currency. Used by the guardrail to evaluate high-risk thresholds.

**`error_code`** — The failure reason from your payment gateway. The classifier uses this as a primary signal. The guardrail evaluates specific values:
- `fraud_suspected` — always escalates to human review
- `gateway_timeout` — cannot result in an EMI offer

**`past_success_rate`** — Optional. The user's historical payment success rate between 0.0 and 1.0. Used by the guardrail's high-risk rule. If omitted, the high-risk threshold check is skipped.

---

## Reading the response

A successful call to `POST /webhook/payment-failure/recover` returns:

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

**What to act on:** `final_intent` is the validated action. Always use this, not `llm_proposed_intent`.

**What happened to the LLM:** `llm_proposed_intent` shows what the classifier originally recommended. If `guardrail_overridden` is `true`, the guardrail disagreed and `final_intent` is different.

**What happened to parameters:** `action` is `MODIFIED` when a financial parameter was clamped. For EMI discounts, always use `modified_parameters.applied`, never the LLM's proposed value.

**For audit purposes:** `transaction_id` and `audit_log_id` are the UUIDs you should store on your side. Both rows exist in the database and can be queried.

### Interpreting `final_intent`

| Value | What to do |
|---|---|
| `retry_now` | Retry the charge immediately. The failure was likely transient. |
| `offer_emi` | Present the user with an installment payment option. The card likely has insufficient funds but the user has good history. |
| `escalate_to_human` | Route to your support or fraud team. Do not retry automatically. |

### Interpreting `action`

| Value | Meaning |
|---|---|
| `APPROVED` | The proposed intent and parameters passed policy checks. |
| `MODIFIED` | The intent was retained, but one or more parameters were bounded by merchant policy. |
| `OVERRIDDEN` | A safety rule replaced the proposed intent. |

### Interpreting `final_status`

The status label written to the audit log. Maps directly from `final_intent`:

| `final_intent` | `final_status` |
|---|---|
| `retry_now` | `RETRIED` |
| `offer_emi` | `EMI_OFFERED` |
| `escalate_to_human` | `ESCALATED` |

---

## Common error scenarios

**HTTP 409 — Duplicate transaction**

You sent a payload with a `transaction_id` that was already processed.

```json
{ "detail": "Webhook event with transaction_id 'txn_abc123' has already been processed." }
```

This is intentional. Recover-Net is idempotency-safe — re-sending the same event will not create a duplicate decision.

**HTTP 422 — Missing required field**

`user_email` or `phone` is absent or empty.

```json
{ "detail": "user_email is required" }
```

**HTTP 500 — PII masking failed**

BlindLog could not mask a field. This is a hard stop — the request is rejected.

```json
{ "detail": "PII masking failed — request rejected for security." }
```

This should not happen in normal operation. If it does, check that `BLINDLOG_SECRET` is set correctly and that `BLINDLOG_DEBUG` is not set.

---

## Batch processing

For bulk replay, load testing, or proving guardrail behavior at scale, use the batch runner. It uses Rich to show live completion progress and a final decision ledger.

**Step 1: Generate a test batch**

```bash
uv run python scripts/generate_batch.py
```

This produces `batch_payload.json` with 75 records:
- 45 standard failures (60%) — should result in `retry_now` or `offer_emi`
- 15 high-risk transactions (20%) — amount > 10,000, success rate < 0.2
- 15 fraud events (20%) — `error_code: fraud_suspected`

The 40% dangerous payload is intentional to demonstrate that the guardrail catches what the LLM might miss.

**Step 2: Run the batch**

Make sure the server is running first, then:

```bash
uv run python scripts/batch_runner.py
```

**Options:**

```bash
uv run python scripts/batch_runner.py --url http://localhost:8000   # default
uv run python scripts/batch_runner.py --concurrency 10              # default is 5
uv run python scripts/batch_runner.py --batch custom_batch.json     # custom batch file
uv run python scripts/batch_runner.py --report-json results.json    # save full results
```

**Why the default concurrency is 5**

Groq's free tier allows approximately 30 requests per minute. With 5 concurrent requests and Groq's sub-200ms inference latency, you stay comfortably within rate limits. The batch runner retries automatically on HTTP 429 with exponential backoff (up to 3 attempts).

If you have a paid Groq plan, you can increase concurrency:

```bash
uv run python scripts/batch_runner.py --concurrency 20
```

The dashboard labels successful non-escalated actions `RECOVERED` in green, escalated actions `BLOCKED` in red, and request failures `FAILED` in yellow. Each successful response shows a shortened `audit_log_id`. The final table also shows the applied rule or action, including `MODIFIED` when an EMI discount was clamped.

**Generating random test data (not for batch)**

```bash
uv run python scripts/generate_mock_data.py
```

This produces `failed_webhooks.json` with 50 random records using realistic Indian names, phone numbers, and email addresses. Useful for ad-hoc testing, not the structured guardrail proof that `scripts/generate_batch.py` produces.

### Configure merchant policies

The `merchant_policies` table controls financial action limits. The migration creates a `default` policy with a 10% maximum discount. Configure a merchant-specific ceiling with SQL:

```sql
INSERT INTO merchant_policies (merchant_id, max_discount_allowed)
VALUES ('merchant-acme', 10.00)
ON CONFLICT (merchant_id) DO UPDATE
SET max_discount_allowed = EXCLUDED.max_discount_allowed;
```

Send that policy key as `merchant_id` in the webhook. If the LLM proposes a 15% EMI discount and the ceiling is 10%, the API returns `action: "MODIFIED"` and the applied discount is 10%. The request is not rejected and the clamp is recorded in `audit_logs.modified_parameters`.

---

## Running tests

```bash
uv run pytest
```

Tests use an in-memory SQLite database and mock the Groq API — no live network calls, no PostgreSQL required.

**Run a specific test file:**

```bash
uv run pytest tests/test_workflow.py -v
uv run pytest tests/test_guardrail.py -v
uv run pytest tests/test_security.py -v
```

**Run with output:**

```bash
uv run pytest -s
```

Test coverage spans:
- `test_classifier.py` — schema validation, all 3 intents, PII leak prevention, error paths
- `test_guardrail.py` — safety rules, financial discount clamping, boundaries, priority, pass-through
- `test_models.py` — ORM validators, `from_webhook()`, masking enforcement, replay prevention
- `test_security.py` — `mask_email`, `mask_phone`, `mask_payload`, missing secret handling
- `test_webhook.py` — FastAPI endpoint integration, PII in logs assertions
- `test_workflow.py` — full pipeline, all guardrail overrides, DB write assertions, status mapping

---

## Typical integration patterns

**Connect your payment gateway**

Configure your payment provider (Razorpay, Stripe, PayU, etc.) to send failed transaction webhooks to:

```
POST https://your-domain.com/webhook/payment-failure/recover
```

Your gateway should send the `error_code` values that map to the classifier's decision guidelines. If your gateway uses different error codes, map them before forwarding to Recover-Net.

**Use `transaction_id` for deduplication**

Always send your gateway's transaction ID as `transaction_id`. Recover-Net enforces uniqueness on this field, so duplicate webhook deliveries (which payment gateways commonly send) are automatically rejected with HTTP 409.

**Store `audit_log_id` on your side**

The `audit_log_id` in the response is the immutable record of what Recover-Net decided and why. Store it against your transaction in case you need to investigate a decision later.

**Act on `final_intent` and bounded parameters, not raw LLM output**

The guardrail may override the LLM or modify its parameters. `final_intent` is always the safe, validated action. When `action` is `MODIFIED`, use `modified_parameters.applied`; using the proposed discount bypasses the financial safety layer.
