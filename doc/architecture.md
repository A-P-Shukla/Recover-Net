# System Architecture

Recover-Net is a single-service FastAPI application. Every inbound payment failure event passes through a four-stage pipeline before a bounded recovery action is committed. Each stage has a single responsibility and a hard contract with the next.

---

## High-level overview

```
Client / Payment Gateway
        │
        │  POST /webhook/payment-failure/recover
        ▼
┌───────────────────────────────────────────────────────┐
│             FastAPI (recover_net.core.app)             │
│         BlindLogFastAPIMiddleware (ASGI layer)        │
│         — masks all request/response bodies in logs   │
└───────────────────┬───────────────────────────────────┘
                    │
                    ▼
        ┌───────────────────────┐
      │  engine/pipeline.py   │   ← The Orchestrator
        │  run_recovery_pipeline│
        └──────┬────────────────┘
               │
    ┌──────────▼──────────┐
    │  Stage 1: Ingest    │
    │  Transaction.from_  │
    │  webhook()          │
    │  BlindLog PII mask  │
    └──────────┬──────────┘
               │  Transaction row flushed (not committed)
    ┌──────────▼──────────┐
    │  Stage 2: Classify  │
   │  llm/classifier.py  │
    │  Groq LPU inference │
    │  <200ms             │
    └──────────┬──────────┘
               │  RecoveryDecision(intent, confidence, discount?)
    ┌──────────▼──────────┐
    │  Stage 3: Guardrail │
   │  guardrails/engine.py│
    │  Deterministic rules│
    │  Pure Python        │
    └──────────┬──────────┘
               │  GuardrailResult(final_intent, action, modified_parameters)
    ┌──────────▼──────────┐
    │  Stage 4: Audit     │
    │  AuditLog INSERT    │
    │  Full provenance    │
    │  Atomic commit      │
    └──────────┬──────────┘
               │
               ▼
      PostgreSQL 16
      transactions + merchant_policies + audit_logs
```

The pipeline is orchestrated entirely in `engine/pipeline.py::run_recovery_pipeline()`. A merchant policy is read before guardrail evaluation. The `Transaction` and `AuditLog` rows are committed atomically. Either both are written or neither is.

---

## Components

### FastAPI application (`core/app.py`)

The entry point. Exposes three routes:

| Route | Purpose |
|---|---|
| `GET /health` | Liveness probe |
| `POST /webhook/payment-failure` | Ingest only — no classification |
| `POST /webhook/payment-failure/recover` | Full pipeline |

`BlindLogFastAPIMiddleware` is attached as an ASGI middleware layer. It intercepts every request and response body before they reach the application logger, applying BlindLog masking to any PII fields. This means even debug logs and error traces never contain raw email or phone values.

---

### PII masking layer (`core/security.py`)

All PII is destroyed at the earliest possible point — before the payload touches the database, the LLM, or any log.

BlindLog performs **deterministic pseudonymization**: the same input always produces the same masked token. This means you can join records by masked email without ever storing the real value.

```
raw email: user@example.com
masked:    blnd_e3f7a9c2...   ← same value every time, for the same secret
```

Key behaviors:
- `BLINDLOG_SECRET` is required. The application refuses to start without it.
- `BLINDLOG_DEBUG=true` is explicitly blocked — it would make masking a no-op.
- If BlindLog returns the original value unchanged, a `MaskingError` is raised and the request is rejected with HTTP 500.
- The logger is cached by secret string (`lru_cache`) to avoid re-instantiating it on every request.

---

### Orchestrator (`engine/pipeline.py`)

`run_recovery_pipeline()` is the central wiring point. It calls each stage in sequence, passing only the data each stage needs.

The guardrail receives a stripped dict with only `error_code`, `amount`, and `past_success_rate` — no PII, no raw email, no names. The merchant policy lookup uses only `merchant_id`. The LLM receives a BlindLog-sanitized copy of the full payload and may propose an EMI discount. Neither stage sees more than it needs.

The function signature accepts optional `groq_client`, `groq_model`, and `secret_key` overrides specifically to support test injection without mocking global state.

---

### Classifier (`llm/classifier.py`)

Sends the sanitized payload to Groq using the native Groq Python SDK. Uses `response_format` with a strict JSON schema to guarantee structured output — no regex parsing, no markdown stripping, no tool-call overhead.

```python
response_format = {
    "type": "json_schema",
    "json_schema": {
        "name": "recovery_action",
        "schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": ["retry_now", "offer_emi", "escalate_to_human"]},
                "confidence": {"type": "number"},
                "discount": {"type": ["number", "null"], "minimum": 0, "maximum": 100}
         },
         "required": ["intent", "confidence"]
      }
   }
}
```

The model must return `intent` and `confidence`, and may return `discount` for `offer_emi`. If the response is empty or not valid JSON, a `RuntimeError` is raised and the pipeline aborts.

Default model: `openai/gpt-oss-20b` (overridable via `GROQ_MODEL_ID`). Temperature is fixed at `0.0` for deterministic output.

---

### Guardrail (`guardrails/engine.py`)

The last line of defense before any action is executed. Rules are evaluated in priority order. Escalation and retry-safety rules run before financial modification; the first match wins.

```
Rule 1 (highest priority)
  IF error_code == "fraud_suspected" AND intent != "escalate_to_human"
  → OVERRIDE to escalate_to_human  [RULE_FRAUD_ESCALATE]

Rule 2
  IF amount > 10,000 AND past_success_rate < 0.2
  → OVERRIDE to escalate_to_human  [RULE_HIGH_RISK_ESCALATE]

Rule 3 (lowest priority)
  IF error_code == "gateway_timeout" AND intent == "offer_emi"
  → OVERRIDE to retry_now          [RULE_TIMEOUT_EMI_CORRECT]

Rule 4 (financial impact)
   IF intent == "offer_emi" AND proposed_discount > merchant.max_discount_allowed
   → MODIFY discount to merchant.max_discount_allowed [RULE_DISCOUNT_CLAMP]
   → action = MODIFIED

No rule matched
  → Pass LLM intent through unchanged
```

The guardrail is pure Python with no external dependencies. It cannot hallucinate. Every correction or modification is tagged with a named rule identifier (`rule_applied`). A modification preserves `offer_emi` while recording proposed, applied, and maximum allowed values.

---

### Database layer (`db/session.py`, `db/models.py`)

SQLAlchemy 2.0 ORM with three tables. `MerchantPolicy` stores the merchant financial ceiling. The ORM model validators (`@validates`) ensure that any direct column assignment also goes through BlindLog — it is not possible to bypass masking by writing directly to `transaction.user_email`.

`init_db()` calls `require_blindlog_secret()` before creating any tables, so a process that boots the schema cannot run without a hashing key configured.

Connection pool is configured with `pool_pre_ping=True` to transparently recover from idle connection drops.

---

### Migrations (Alembic)

Schema versioning via Alembic. `0001_initial_schema` creates the original transaction and audit tables; `0002_dynamic_policies` adds merchant policies, transaction merchant identity, and modification audit fields.

```bash
uv run alembic upgrade head    # apply all migrations
uv run alembic downgrade -1    # roll back one revision
```

The migration is PostgreSQL-native — it uses `postgresql.UUID` and `postgresql.JSONB` column types directly. The ORM models use `with_variant()` to fall back to generic `UUID` and `JSON` types for SQLite (used in tests).

---

### Infrastructure (`docker-compose.yml`)

A single `postgres:16-alpine` container with a named volume for persistence, a health check, and `restart: unless-stopped`. No other infrastructure is required to run the service.

```
Container:  recover_net_postgres
Port:       5432
Database:   recover_net
User:       postgres
Volume:     postgres_data (persistent)
```

---

## Data flow: a single request

```
1. POST /webhook/payment-failure/recover arrives
   Payload: { transaction_id, merchant_id, user_email, phone, amount, error_code, past_success_rate }

2. BlindLogFastAPIMiddleware intercepts the request body
   Logs a masked copy — raw PII never appears in application logs

3. Endpoint handler calls run_recovery_pipeline(payload, db)

4. Transaction.from_webhook(payload)
   - Generates a new internal transaction_id (UUID v4)
   - Stores source transaction_id as source_transaction_id
   - @validates hooks call mask_email() and mask_phone() via BlindLog
   - mask_payload() produces a sanitized copy of the full payload
   - MaskingError raised if any field is unchanged after masking
   - db.flush() — row exists in session, not yet committed

   - merchant_id defaults to `default` when omitted

5. classify_payment_failure(raw_payload, client=groq_client)
   - mask_payload() called again on the input
   - Sends sanitized JSON to Groq with response_format JSON schema
   - Returns RecoveryDecision(intent="retry_now", confidence=0.95, discount=None)

6. MerchantPolicy lookup and evaluate_action(guardrail_input, groq_decision.intent, groq_decision.discount, max_discount_allowed)
   - Only receives error_code, amount, past_success_rate
   - Rules evaluated in priority order
   - Returns GuardrailResult(final_intent="offer_emi", action="MODIFIED", modified_parameters={...}) when a discount is clamped

7. AuditLog created
   - llm_proposed_action = JSON-serialized RecoveryDecision
   - guardrail_decision = JSON-serialized GuardrailResult
   - action = `APPROVED`, `MODIFIED`, or `OVERRIDDEN`
   - modified_parameters = proposed/applied/max values when clamped
   - final_status = "RETRIED"
   - action = "APPROVED" or "MODIFIED" or "OVERRIDDEN"

8. db.commit()
   - Both Transaction and AuditLog written atomically
   - Either both rows land or neither does

9. HTTP 201 response
    { transaction_id, audit_log_id, final_intent, final_status,
       guardrail_overridden, rule_applied, action, modified_parameters,
       llm_proposed_intent, llm_confidence }
```

---

## Security boundaries

| Boundary | Control |
|---|---|
| PII in database | BlindLog pseudonymization; `MaskingError` hard-stop if masking is bypassed |
| PII in logs | `BlindLogFastAPIMiddleware` on all request/response bodies |
| PII to LLM | `mask_payload()` called before every Groq API call |
| LLM autonomy | Guardrail overrides unsafe intents and clamps financial parameters |
| Replay attacks | `UNIQUE` constraint on `source_transaction_id`; duplicate returns HTTP 409 |
| Secret at rest | `BLINDLOG_SECRET` loaded from environment only; no hardcoded defaults |
| Debug mode | `BLINDLOG_DEBUG=true` explicitly rejected at startup |

---

## Module dependency graph

```
recover_net.core.app
   ├── recover_net.engine.pipeline
   │     ├── recover_net.llm.classifier
   │     │     └── recover_net.core.security
   │     ├── recover_net.guardrails.engine
   │     │     └── recover_net.llm.classifier (RecoveryIntent type only)
   │     └── recover_net.db.models
   │           ├── recover_net.db.session
   │           └── recover_net.core.security
   └── recover_net.core.security
        └── blindlog (external)
```

There are no circular imports. `security.py` is the only module imported by everything else — it has no internal dependencies beyond `blindlog`.
