# Database Reference

Recover-Net uses PostgreSQL 16. The schema has three tables: `transactions`, `merchant_policies`, and `audit_logs`. Every pipeline run produces one transaction and one audit row, committed atomically.

---

## Overview

```
transactions                        audit_logs
────────────────────────────────    ─────────────────────────────────────
transaction_id        UUID (PK)     log_id               UUID (PK)
source_transaction_id VARCHAR(64)   transaction_id       UUID (FK → transactions)
user_email            VARCHAR(255)  llm_proposed_action  TEXT  (JSON)
phone                 VARCHAR(100)  guardrail_decision   TEXT  (JSON)
amount                NUMERIC(12,2) final_status         VARCHAR(50)
error_code            VARCHAR(100)  timestamp            TIMESTAMPTZ
past_success_rate     FLOAT
raw_payload           JSONB
created_at            TIMESTAMPTZ

merchant_policies
────────────────────────────────
merchant_id           VARCHAR(100) (PK)
max_discount_allowed  NUMERIC(5,2)
```

`audit_logs.transaction_id` is a foreign key with `ON DELETE CASCADE` — deleting a transaction removes its audit logs.

---

## Table: `transactions`

Stores the ingested webhook event. PII fields are pseudonymized before any data reaches this table.

### Columns

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `transaction_id` | `UUID` | No | `uuid4()` | Internal primary key. Always generated server-side. Never taken from the inbound webhook. |
| `source_transaction_id` | `VARCHAR(64)` | Yes | — | Your external transaction ID, stored as-is. Enforced unique via `uq_transactions_source_transaction_id`. |
| `merchant_id` | `VARCHAR(100)` | No | `default` | Merchant whose financial policy gates the recovery action. |
| `user_email` | `VARCHAR(255)` | No | — | BlindLog-pseudonymized email. The raw value is never stored. |
| `phone` | `VARCHAR(100)` | No | — | BlindLog-pseudonymized phone number. The raw value is never stored. |
| `amount` | `NUMERIC(12,2)` | No | — | Transaction amount. Two decimal places of precision. |
| `error_code` | `VARCHAR(100)` | No | — | Failure reason from the webhook (e.g. `insufficient_funds`, `gateway_timeout`). |
| `past_success_rate` | `FLOAT` | Yes | `NULL` | User's historical payment success rate (0.0–1.0). Used by the guardrail high-risk rule. |
| `raw_payload` | `JSONB` | Yes | — | Full webhook payload with all PII masked via BlindLog. Never contains raw email or phone. |
| `created_at` | `TIMESTAMPTZ` | No | `now()` | Ingestion timestamp. Set by the database server. |

### Indexes

| Index | Column(s) | Type |
|---|---|---|
| `transactions_pkey` | `transaction_id` | Primary key |
| `uq_transactions_source_transaction_id` | `source_transaction_id` | Unique |
| `ix_transactions_user_email` | `user_email` | B-tree |
| `ix_transactions_phone` | `phone` | B-tree |
| `ix_transactions_error_code` | `error_code` | B-tree |
| `ix_transactions_merchant_id` | `merchant_id` | B-tree |

`user_email` and `phone` are indexed on their pseudonymized values, so you can look up a user's transaction history using the masked token without ever needing the raw PII.

### PII guarantees

The ORM enforces masking at the column level via SQLAlchemy `@validates` hooks. It is not possible to bypass masking by assigning directly to `transaction.user_email` — the validator always runs BlindLog before the value is stored.

Additionally, `Transaction.from_webhook()` verifies the masked output before returning:
- If `tx.user_email` equals the raw email string, a `MaskingError` is raised.
- If `raw_payload["user_email"]` equals the raw email string, a `MaskingError` is raised.
- If the internal `transaction_id` equals the inbound `source_transaction_id`, a `MaskingError` is raised.

None of these checks can be disabled.

### Example row

```sql
SELECT transaction_id, source_transaction_id, merchant_id, user_email, phone, amount, error_code, past_success_rate, created_at
FROM transactions
LIMIT 1;
```

```
transaction_id        | a1b2c3d4-e5f6-7890-abcd-ef1234567890
source_transaction_id | txn_abc123
user_email            | blnd_e3f7a9c2b1d4f5e6...
phone                 | blnd_91c4b7a3d2e1f0...
amount                | 4999.00
error_code            | insufficient_funds
past_success_rate     | 0.82
created_at            | 2026-08-25 06:30:12.345678+00
```

---

## Table: `merchant_policies`

Stores the merchant-configured financial ceiling used by the deterministic guardrail.

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `merchant_id` | `VARCHAR(100)` | No | — | Policy key referenced by `transactions.merchant_id`. |
| `max_discount_allowed` | `NUMERIC(5,2)` | No | `10.00` | Maximum EMI interest discount percentage. |

The migration seeds the `default` merchant policy at 10%. A missing merchant policy also fails safe to the 10% application default; configure an explicit row for each merchant in production.

## Table: `audit_logs`

Stores the full decision provenance for every pipeline run. One row per transaction processed through `POST /webhook/payment-failure/recover`. Immutable once written — no update path exists in the application.

### Columns

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `log_id` | `UUID` | No | `uuid4()` | Primary key. |
| `transaction_id` | `UUID` | No | — | Foreign key to `transactions.transaction_id`. |
| `llm_proposed_action` | `TEXT` | Yes | — | JSON-serialized `RecoveryDecision`. The raw LLM output before guardrail evaluation. |
| `guardrail_decision` | `TEXT` | Yes | — | JSON-serialized `GuardrailResult`. The guardrail's verdict including override details. |
| `action` | `VARCHAR(20)` | No | `APPROVED` | Guardrail outcome: `APPROVED`, `MODIFIED`, or `OVERRIDDEN`. |
| `modified_parameters` | `JSONB` | Yes | — | Proposed, applied, and maximum allowed values when a parameter is clamped. |
| `final_status` | `VARCHAR(50)` | No | — | The final outcome label: `RETRIED`, `EMI_OFFERED`, or `ESCALATED`. |
| `timestamp` | `TIMESTAMPTZ` | No | `now()` | Decision timestamp. Set by the database server. |

### Indexes

| Index | Column(s) | Type |
|---|---|---|
| `audit_logs_pkey` | `log_id` | Primary key |
| `ix_audit_logs_transaction_id` | `transaction_id` | B-tree |
| `ix_audit_logs_final_status` | `final_status` | B-tree |
| `ix_audit_logs_timestamp` | `timestamp` | B-tree |

### `llm_proposed_action` schema

JSON string stored in `TEXT`. Always parseable as:

```json
{
  "intent": "offer_emi",
  "confidence": 0.91
}
```

| Field | Type | Description |
|---|---|---|
| `intent` | string | One of `retry_now`, `offer_emi`, `escalate_to_human` |
| `confidence` | number | 0.0–1.0. The LLM's confidence in its classification. |

### `guardrail_decision` schema

JSON string stored in `TEXT`. Always parseable as:

```json
{
  "final_intent": "escalate_to_human",
  "overridden": true,
  "rule_applied": "RULE_HIGH_RISK_ESCALATE",
  "original_intent": "offer_emi"
}
```

For a financial modification, the audit row also contains:

```json
{
  "action": "MODIFIED",
  "modified_parameters": {
    "parameter": "discount",
    "proposed": 15.0,
    "applied": 10.0,
    "max_allowed": 10.0
  }
}
```

| Field | Type | Description |
|---|---|---|
| `final_intent` | string | The validated action after guardrail evaluation. |
| `overridden` | boolean | `true` if the guardrail changed the LLM's proposed intent. |
| `rule_applied` | string \| null | The rule that fired, including `RULE_DISCOUNT_CLAMP`, or `null` if no rule fired. |
| `original_intent` | string | What the LLM proposed before guardrail evaluation. Equal to `final_intent` when `overridden` is `false`. |

### `final_status` values

| Value | Meaning |
|---|---|
| `RETRIED` | Transaction was routed for immediate retry. |
| `EMI_OFFERED` | EMI installment plan was offered to the user. |
| `ESCALATED` | Transaction was routed to human review. |

`action` is independent of `final_status`: a clamped EMI remains `EMI_OFFERED`, but its audit action is `MODIFIED`.

### Example row

```sql
SELECT log_id, transaction_id, llm_proposed_action::json, guardrail_decision::json, action, modified_parameters, final_status, timestamp
FROM audit_logs
WHERE transaction_id = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890';
```

```
log_id              | f9e8d7c6-b5a4-3210-fedc-ba9876543210
transaction_id      | a1b2c3d4-e5f6-7890-abcd-ef1234567890
llm_proposed_action | {"intent": "offer_emi", "confidence": 0.91}
guardrail_decision  | {"final_intent": "escalate_to_human", "overridden": true,
                    |  "rule_applied": "RULE_HIGH_RISK_ESCALATE",
                    |  "original_intent": "offer_emi"}
final_status        | ESCALATED
timestamp           | 2026-08-25 06:30:12.401234+00
```

---

## Relationships

```
transactions  1 ──────── * audit_logs
              (transaction_id)
```

One transaction can have multiple audit logs if you implement reprocessing. In the current implementation, each call to `POST /webhook/payment-failure/recover` creates exactly one `Transaction` and one `AuditLog` in a single atomic commit.

Deleting a `Transaction` cascades to delete all associated `AuditLog` rows (`ON DELETE CASCADE`).

---

## Migrations

Recover-Net uses Alembic for schema versioning. Migration files live in `alembic/versions/`.

**Apply all pending migrations:**

```bash
uv run alembic upgrade head
```

**Check current revision:**

```bash
uv run alembic current
```

**Roll back one revision:**

```bash
uv run alembic downgrade -1
```

**View migration history:**

```bash
uv run alembic history
```

### Current migrations

| Revision | Description |
|---|---|
| `0001_initial` | Creates `transactions` and `audit_logs` tables with all indexes and constraints. |
| `0002_dynamic_policies` | Adds `merchant_policies`, `transactions.merchant_id`, and audit modification fields; seeds the 10% default ceiling. |

The migration uses PostgreSQL-native types (`postgresql.UUID`, `postgresql.JSONB`) directly. If you need to run against a different database for testing, the ORM models use `with_variant()` to fall back to generic SQLAlchemy types automatically.

---

## Connection configuration

The database URL is read from `DATABASE_URL` in the environment. The default points to the Docker Compose container:

```
postgresql+psycopg2://postgres:postgres@localhost:5432/recover_net
```

The SQLAlchemy engine is configured with:

```python
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # validates connections before use; handles idle drops
    echo=False,           # set to True to log all SQL to stdout
)
```

**Session management** uses the `get_db()` dependency injected into each FastAPI route. Sessions are never shared across requests.

---

## Querying the data

**Find all escalated transactions in the last 24 hours:**

```sql
SELECT t.source_transaction_id, t.amount, t.error_code, a.timestamp, a.guardrail_decision
FROM audit_logs a
JOIN transactions t ON t.transaction_id = a.transaction_id
WHERE a.final_status = 'ESCALATED'
  AND a.timestamp > now() - interval '24 hours'
ORDER BY a.timestamp DESC;
```

**Count guardrail overrides by rule:**

```sql
SELECT
  guardrail_decision->>'rule_applied' AS rule,
  COUNT(*) AS overrides
FROM audit_logs
WHERE (guardrail_decision::json->>'overridden')::boolean = true
GROUP BY rule
ORDER BY overrides DESC;
```

**Find all transactions for a specific masked email:**

```sql
-- First get the masked token using BlindLog externally, then:
SELECT * FROM transactions WHERE user_email = 'blnd_e3f7a9c2...';
```

**Revenue recovered vs escalated (last 7 days):**

```sql
SELECT
  final_status,
  COUNT(*) AS count,
  SUM(t.amount) AS total_amount
FROM audit_logs a
JOIN transactions t ON t.transaction_id = a.transaction_id
WHERE a.timestamp > now() - interval '7 days'
GROUP BY final_status
ORDER BY total_amount DESC;
```

---

## Docker setup

The Docker Compose file starts a PostgreSQL 16 Alpine container with a named volume:

```bash
docker compose up -d          # start in background
docker compose down           # stop (data persists in volume)
docker compose down -v        # stop and delete all data
```

**Connect directly:**

```bash
docker exec -it recover_net_postgres psql -U postgres -d recover_net
```

**Check container health:**

```bash
docker inspect recover_net_postgres --format='{{.State.Health.Status}}'
```

The health check runs `pg_isready` every 5 seconds. The FastAPI server's `pool_pre_ping=True` will transparently recover from any brief container restarts without requiring a server restart.
