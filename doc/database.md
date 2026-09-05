# Database Reference

Recover-Net uses PostgreSQL 16 (or SQLite in transactional test environments). The relational model consists of three core tables: `transactions`, `merchant_policies`, and `audit_logs`. Every completed recovery pipeline execution atomically writes an immutable transaction and audit record.

---

## Documentation Navigation
- [Database Reference](database.md) (Current)
- [System Architecture](architecture.md) — Pipeline flow, component boundaries, and security design
- [Usage Guide](usage.md) — Endpoint requests, HMAC signing, batch processing, and testing
- [Project Overview & Quickstart](../README.md) — Root documentation and Stripe-style reference

---

## Schema Overview

```
transactions                        audit_logs
────────────────────────────────    ─────────────────────────────────────
transaction_id        UUID (PK)     log_id               UUID (PK)
source_transaction_id VARCHAR(64)   transaction_id       UUID (FK → transactions)
merchant_id           VARCHAR(100)  llm_proposed_action  TEXT  (JSON)
user_email            VARCHAR(255)  guardrail_decision   TEXT  (JSON)
phone                 VARCHAR(100)  action               VARCHAR(20)
amount                NUMERIC(12,2) modified_parameters  JSON / JSONB
error_code            VARCHAR(100)  final_status         VARCHAR(50)
past_success_rate     FLOAT         timestamp            TIMESTAMPTZ
raw_payload           JSON / JSONB
created_at            TIMESTAMPTZ

merchant_policies
────────────────────────────────
merchant_id           VARCHAR(100) (PK)
max_discount_allowed  NUMERIC(5,2)
```

`audit_logs.transaction_id` references `transactions.transaction_id` with `ON DELETE CASCADE` — deleting a transaction automatically removes related audit records.

---

## Table: `transactions`

Stores the ingested webhook event. PII fields (`user_email`, `phone`) are hashed and pseudonymized via BlindLog prior to persistence.

### Columns

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `transaction_id` | `UUID` | No | `uuid4()` | Internal primary key. Generated server-side. Never derived from inbound webhook IDs. |
| `source_transaction_id` | `VARCHAR(64)` | Yes | — | Inbound transaction ID from payment gateway. Enforced unique via `uq_transactions_source_transaction_id`. |
| `merchant_id` | `VARCHAR(100)` | No | `default` | Merchant identifier whose policy governs recovery ceilings. |
| `user_email` | `VARCHAR(255)` | No | — | BlindLog-pseudonymized email hash (e.g. `blnd_ref_...`). |
| `phone` | `VARCHAR(100)` | No | — | BlindLog-pseudonymized phone hash (e.g. `blind:...`). |
| `amount` | `NUMERIC(12,2)` | No | — | Transaction amount in currency units. |
| `error_code` | `VARCHAR(100)` | No | — | Payment failure code (`insufficient_funds`, `gateway_timeout`, `fraud_suspected`, `invalid_cvv`). |
| `past_success_rate` | `FLOAT` | Yes | `NULL` | Historical success rate of the user (0.0 to 1.0). |
| `raw_payload` | `JSONB` / `JSON` | Yes | — | Sanitized webhook payload copy where all PII fields are masked. |
| `created_at` | `TIMESTAMPTZ` | No | `now()` | Ingestion timestamp. |

### Indexes

| Index Name | Column(s) | Type |
|---|---|---|
| `transactions_pkey` | `transaction_id` | Primary Key |
| `uq_transactions_source_transaction_id` | `source_transaction_id` | Unique Constraint |
| `ix_transactions_user_email` | `user_email` | B-tree |
| `ix_transactions_phone` | `phone` | B-tree |
| `ix_transactions_error_code` | `error_code` | B-tree |
| `ix_transactions_merchant_id` | `merchant_id` | B-tree |

---

## Table: `merchant_policies`

Maintains merchant-specific policy rules and financial guardrail ceilings.

### Columns

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `merchant_id` | `VARCHAR(100)` | No | — | Primary key identifying the merchant (e.g. `merchant-acme`, `default`). |
| `max_discount_allowed` | `NUMERIC(5,2)` | No | `10.00` | Maximum allowable EMI recovery discount percentage. |

> **Fail-Closed Rule**: If a transaction webhook specifies an unconfigured `merchant_id`, the orchestrator rejects the request with `HTTP 422 Unprocessable Content` rather than allowing unverified default discounts. See [System Architecture](architecture.md).

---

## Table: `audit_logs`

Provides an immutable, tamper-evident record of all AI classification proposals and deterministic guardrail outcomes.

### Columns

| Column | Type | Nullable | Default | Description |
|---|---|---|---|---|
| `log_id` | `UUID` | No | `uuid4()` | Primary key for the audit log entry. |
| `transaction_id` | `UUID` | No | — | Foreign key reference to `transactions.transaction_id`. |
| `llm_proposed_action` | `TEXT` | Yes | — | JSON-serialized `RecoveryDecision` from Bedrock classifier (`intent`, `confidence`, `discount`). |
| `guardrail_decision` | `TEXT` | Yes | — | JSON-serialized `GuardrailResult` (`final_intent`, `overridden`, `rule_applied`). |
| `action` | `VARCHAR(20)` | No | `APPROVED` | Decision outcome: `APPROVED`, `MODIFIED`, or `OVERRIDDEN`. |
| `modified_parameters` | `JSONB` / `JSON` | Yes | `NULL` | Provenance dictionary recording proposed, applied, and max parameter values when bounded. |
| `final_status` | `VARCHAR(50)` | No | — | Status label (`RETRIED`, `EMI_OFFERED`, `ESCALATED`). |
| `timestamp` | `TIMESTAMPTZ` | No | `now()` | Immutable timestamp recorded at write. |

---

## Migrations (Alembic)

Database schema revisions are managed via Alembic:

| Revision ID | Description |
|---|---|
| `0001_initial` | Initial schema for `transactions` and `audit_logs` tables. |
| `0002_dynamic_policies` | Adds `merchant_policies` table, `merchant_id` column, and audit `action` / `modified_parameters` columns. |

```bash
# Run latest migrations
uv run alembic upgrade head

# Rollback one revision
uv run alembic downgrade -1
```

---

## Example Operational Queries

### 1. View Decision Provenance for a Transaction
```sql
SELECT 
    t.source_transaction_id,
    t.merchant_id,
    t.user_email AS masked_email,
    t.amount,
    t.error_code,
    a.final_status,
    a.action,
    a.guardrail_decision,
    a.timestamp
FROM transactions t
JOIN audit_logs a ON t.transaction_id = a.transaction_id
WHERE t.source_transaction_id = 'txn_12345';
```

### 2. Count Guardrail Interventions by Rule
```sql
SELECT 
    (guardrail_decision::json->>'rule_applied') AS rule_name,
    COUNT(*) AS trigger_count
FROM audit_logs
WHERE action IN ('OVERRIDDEN', 'MODIFIED')
GROUP BY rule_name
ORDER BY trigger_count DESC;
```

---

## Cross-Document References
- [Usage Guide](usage.md)
- [System Architecture](architecture.md)
- [Root README](../README.md)
