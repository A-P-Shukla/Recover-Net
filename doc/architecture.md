# System Architecture

Recover-Net is a high-performance, single-service FastAPI application designed for intelligent payment failure recovery. Every inbound payment failure event passes through a four-stage pipeline before a bounded recovery action is committed. Each stage has a single responsibility and a hard contract with the next.

---

## Documentation Navigation
- [System Architecture](architecture.md) (Current)
- [Usage Guide](usage.md) — Running the stack, payloads, batch runner, and integration patterns
- [Database Reference](database.md) — PostgreSQL schema, models, indexes, and audit logs
- [Conversation & Audit Log](../docs/CONVERSATION_LOG.md) — Chronological record of decisions and updates
- [Project Overview & Quickstart](../README.md) — Root documentation and Stripe-style reference

---

## High-level overview

```
Client / Payment Gateway
        │
        │  POST /webhook/payment-failure/recover
        │  Header: X-Webhook-Signature: sha256=<hex>
        ▼
┌──────────────────────────────────────────────────────────┐
│              FastAPI (recover_net.core.app)              │
│  1. HMAC-SHA256 signature verification (fail-closed)     │
│  2. BlindLogFastAPIMiddleware (ASGI layer)               │
│     — masks all request/response bodies in logs          │
└────────────────────────────┬─────────────────────────────┘
                             │
                             ▼
                 ┌───────────────────────┐
                 │  engine/pipeline.py   │   ← The Orchestrator
                 │  run_recovery_pipeline│
                 └───────────┬───────────┘
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
                  │  AWS Bedrock        │
                  │  inference          │
                  └──────────┬──────────┘
                             │  RecoveryDecision(intent, confidence, discount?)
                  ┌──────────▼──────────┐
                  │  Stage 3: Guardrail │
                  │  guardrails/engine.py│
                  │  Dynamic policy     │
                  │  Deterministic rules│
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

The pipeline is orchestrated entirely in `recover_net.engine.pipeline::run_recovery_pipeline()`. Merchant policy is queried before guardrail evaluation; if the merchant is unregistered, the pipeline fails closed. The `Transaction` and `AuditLog` rows are committed atomically at the HTTP boundary. Either both are written or neither is.

---

## Components

### FastAPI Application (`src/recover_net/core/app.py`)

The entry point. Exposes three routes:

| Route | Method | Purpose | Authentication |
|---|---|---|---|
| `/health` | `GET` | Liveness & health check | Public |
| `/webhook/payment-failure` | `POST` | Ingest only — PII masking and storage | HMAC-SHA256 (`X-Webhook-Signature`) |
| `/webhook/payment-failure/recover` | `POST` | Full recovery pipeline & decision engine | HMAC-SHA256 (`X-Webhook-Signature`) |

#### Key Lifecycle Behaviors:
- **Lifespan Startup Check**: The application validates `BLINDLOG_SECRET`, `OPENAI_API_KEY`, and `WEBHOOK_SECRET` on boot. If any required secret is missing or empty, startup is aborted with `RuntimeError`.
- **BlindLog ASGI Middleware**: `BlindLogFastAPIMiddleware` intercepts every request/response body before reaching terminal logs, guaranteeing zero raw PII emission.

---

### Security & Ingest Layer (`src/recover_net/api/webhooks.py`, `src/recover_net/core/security.py`)

1. **HMAC-SHA256 Webhook Verification**:
   - Inbound webhook payloads are verified against `WEBHOOK_SECRET` using `hmac.compare_digest`.
   - Callers must pass `X-Webhook-Signature: sha256=<hex_digest>`.
   - Missing headers, wrong prefixes, or tampered payloads immediately return `HTTP 401 Unauthorized`.
   - Missing server secret fails closed with `HTTP 500`.

2. **Deterministic PII Masking (BlindLog)**:
   - Email addresses and phone numbers are hashed into pseudonymized tokens before touching database storage, logs, or LLM prompts.
   - `mask_payload()` guarantees deterministic tokens (e.g. `blnd_ref_...` and `blind:...`) enabling database joins without raw PII.
   - `MaskingError` is a hard stop: if masking fails or returns raw values, requests are rejected immediately.

---

### Orchestrator Pipeline (`src/recover_net/engine/pipeline.py`)

`run_recovery_pipeline(raw_payload, db)` coordinates the decision cycle:
1. **Ingest**: Generates internal UUID `transaction_id`, isolates external `source_transaction_id`, masks PII, and flushes `Transaction` to the session.
2. **Classify**: Invokes AWS Bedrock (via OpenAI-compatible client) with JSON schema output format, obtaining `RecoveryDecision(intent, confidence, discount)`.
3. **Policy & Guardrails**:
   - Queries `MerchantPolicy` for `tx.merchant_id`. If unregistered, raises `ValueError` (fails closed with `HTTP 422`).
   - Evaluates deterministic safety rules (fraud escalations, CVV escalations, high-risk flags, timeout corrections) and clamps financial discount ceilings.
4. **Audit Ledger**: Creates an `AuditLog` row recording full provenance (`llm_proposed_action`, `guardrail_decision`, `action`, `modified_parameters`, `final_status`).
5. **Commit Boundary**: Database commits are executed at the HTTP route handler in `api/webhooks.py` for transactional rollback safety.

---

### Deterministic Guardrail Engine (`src/recover_net/guardrails/engine.py`)

Pure Python business logic. No hallucinations, no LLM probabilistic drift.

| Rule | Condition | Outcome | Action |
|---|---|---|---|
| `RULE_FRAUD_ESCALATE` | `error_code == "fraud_suspected"` and LLM did not escalate | Force `escalate_to_human` | `OVERRIDDEN` |
| `RULE_INVALID_CVV_ESCALATE` | `error_code == "invalid_cvv"` and LLM did not escalate | Force `escalate_to_human` | `OVERRIDDEN` |
| `RULE_HIGH_RISK_ESCALATE` | `amount > 10000` and `past_success_rate < 0.2` | Force `escalate_to_human` | `OVERRIDDEN` |
| `RULE_TIMEOUT_EMI_CORRECT` | `error_code == "gateway_timeout"` and LLM proposed `offer_emi` | Correct to `retry_now` | `OVERRIDDEN` |
| `RULE_DISCOUNT_CLAMP` | Proposed EMI discount > `max_discount_allowed` | Clamp to ceiling | `MODIFIED` |

---

### Database Layer (`src/recover_net/db/`)

See [Database Reference](database.md) for full column schemas and queries.

- `transactions`: Internal UUID PK, unique `source_transaction_id`, masked `user_email`, masked `phone`, numeric `amount`, `merchant_id`.
- `merchant_policies`: Merchant-configured policy ceilings (`max_discount_allowed`).
- `audit_logs`: Foreign key to `transactions.transaction_id`, JSON provenance records, `final_status`, timestamp.

---

## Security Boundaries

| Boundary | Protection Mechanism |
|---|---|
| **Webhook Authenticity** | HMAC-SHA256 signature verification (`X-Webhook-Signature`), fail-closed |
| **PII at Rest** | Column-level BlindLog hashing validators, `MaskingError` hard stop |
| **PII in Logs** | `BlindLogFastAPIMiddleware` intercepts ASGI request/response buffers |
| **PII in LLM Calls** | `mask_payload()` sanitization applied before LLM prompt assembly |
| **Model Hallucination** | Deterministic Python guardrails with hard policy ceilings |
| **Merchant Authorization** | Strict `merchant_policies` lookup; unregistered merchants rejected (`HTTP 422`) |
| **Replay Attacks** | Database uniqueness constraint on `source_transaction_id` (`HTTP 409`) |

---

## Module Dependency Graph

```
recover_net.core.app
   ├── recover_net.api.webhooks
   │     ├── recover_net.engine.pipeline
   │     │     ├── recover_net.llm.classifier
   │     │     │     └── recover_net.core.security
   │     │     ├── recover_net.guardrails.engine
   │     │     │     └── recover_net.llm.classifier (RecoveryIntent enum)
   │     │     └── recover_net.db.models
   │     │           ├── recover_net.db.session
   │     │           └── recover_net.core.security
   │     └── recover_net.core.security
   └── recover_net.core.security
```

---

## Cross-Document References
- [Usage Guide](usage.md)
- [Database Reference](database.md)
- [Conversation Log](../docs/CONVERSATION_LOG.md)
- [Root README](../README.md)
