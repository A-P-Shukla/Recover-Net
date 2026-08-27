"""
db/models.py

SQLAlchemy 2.0 ORM models for Recover-Net.
- Table 1: transactions (webhook data with BlindLog-hashed email and phone)
- Table 2: audit_logs (log_id, transaction_id, llm_proposed_action, guardrail_decision, final_status, timestamp)

transaction_id is an internal primary key. Inbound webhook IDs are stored on
source_transaction_id and are never used as the row identity.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates
from sqlalchemy.types import JSON, UUID

from recover_net.db.session import Base
from recover_net.core.security import MaskingError, mask_email, mask_payload, mask_phone, using_secret

CompatibleUUID = UUID(as_uuid=True).with_variant(PG_UUID(as_uuid=True), "postgresql")
CompatibleJSON = JSON().with_variant(JSONB(), "postgresql")


class Transaction(Base):
    """
    Table: transactions
    Stores webhook event data. Email and phone MUST be hashed via BlindLog.
    """
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint(
            "source_transaction_id",
            name="uq_transactions_source_transaction_id",
        ),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        CompatibleUUID,
        primary_key=True,
        default=uuid.uuid4,
        doc="Internal primary key (UUID v4). Never taken from inbound webhooks.",
    )

    source_transaction_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        doc="Inbound webhook transaction id (unique external reference, not the PK)",
    )

    merchant_id: Mapped[str] = mapped_column(
        String(100), nullable=False, default="default", index=True
    )

    user_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        doc="Deterministic pseudonymized/masked email via BlindLog",
    )

    phone: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        doc="Deterministic pseudonymized/masked phone number via BlindLog",
    )

    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
        doc="Transaction amount in currency units",
    )

    error_code: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        doc="Webhook failure error code (e.g., insufficient_funds, gateway_timeout)",
    )

    past_success_rate: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        doc="User historical success rate (0.0 - 1.0)",
    )

    raw_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        CompatibleJSON,
        nullable=True,
        doc="Full webhook payload with sensitive PII blinded via BlindLog",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        doc="Timestamp when the transaction record was ingested",
    )

    audit_logs: Mapped[List["AuditLog"]] = relationship(
        "AuditLog",
        back_populates="transaction",
        cascade="all, delete-orphan",
        order_by="AuditLog.timestamp.desc()",
        lazy="selectin",
    )

    @validates("user_email")
    def validate_and_mask_email(self, key: str, value: str) -> str:
        """Always run BlindLog. Do not skip on blnd_ / @masked.com prefixes."""
        if not value or not str(value).strip():
            raise ValueError("user_email is required")
        masked = mask_email(str(value))
        assert masked is not None
        return masked

    @validates("phone")
    def validate_and_mask_phone(self, key: str, value: str) -> str:
        """Always run BlindLog. Do not skip on a blind: prefix."""
        if not value or not str(value).strip():
            raise ValueError("phone is required")
        masked = mask_phone(str(value))
        assert masked is not None
        return masked

    @classmethod
    def from_webhook(
        cls, raw_data: Dict[str, Any], secret_key: Optional[str] = None
    ) -> "Transaction":
        """
        Ingest a raw webhook dictionary.

        Internal transaction_id is always generated here. The inbound id is
        stored on source_transaction_id. Email/phone columns receive the raw
        values so validators mask them; raw_payload is independently masked
        and never falls back to the original PII.
        """
        if secret_key:
            with using_secret(secret_key):
                return cls._from_webhook(raw_data, secret_key)
        return cls._from_webhook(raw_data, secret_key)

    @classmethod
    def _from_webhook(
        cls, raw_data: Dict[str, Any], secret_key: Optional[str]
    ) -> "Transaction":
        raw_email = raw_data.get("user_email")
        raw_phone = raw_data.get("phone")
        if not raw_email or not str(raw_email).strip():
            raise ValueError("user_email is required")
        if not raw_phone or not str(raw_phone).strip():
            raise ValueError("phone is required")

        masked_payload = mask_payload(raw_data, secret_key=secret_key)

        source_id = raw_data.get("transaction_id")
        if source_id is not None:
            source_id = str(source_id)

        amount_raw = raw_data.get("amount", 0)
        amount = Decimal(str(amount_raw)) if amount_raw is not None else Decimal("0.00")
        success_rate_raw = raw_data.get("past_success_rate")
        success_rate = (
            float(success_rate_raw) if success_rate_raw is not None else None
        )

        tx = cls(
            transaction_id=uuid.uuid4(),
            user_email=str(raw_email),
            phone=str(raw_phone),
            amount=amount,
            error_code=str(raw_data.get("error_code", "unknown")),
            past_success_rate=success_rate,
            source_transaction_id=source_id,
            merchant_id=str(raw_data.get("merchant_id", "default")),
            raw_payload=masked_payload,
        )

        if tx.user_email == str(raw_email):
            raise MaskingError("Refusing to persist unmasked user_email")
        if tx.phone == str(raw_phone):
            raise MaskingError("Refusing to persist unmasked phone")
        if masked_payload.get("user_email") == str(raw_email):
            raise MaskingError("Refusing to persist unmasked user_email in raw_payload")
        if masked_payload.get("phone") == str(raw_phone):
            raise MaskingError("Refusing to persist unmasked phone in raw_payload")
        if source_id and str(tx.transaction_id) == source_id:
            raise MaskingError("Internal transaction_id must not equal the inbound webhook id")

        return tx

    def __repr__(self) -> str:
        return (
            f"<Transaction(id={self.transaction_id}, email={self.user_email}, "
            f"amount={self.amount}, error={self.error_code})>"
        )


class AuditLog(Base):
    """
    Table: audit_logs
    Columns: log_id, transaction_id, llm_proposed_action, guardrail_decision, final_status, timestamp
    """
    __tablename__ = "audit_logs"

    log_id: Mapped[uuid.UUID] = mapped_column(
        CompatibleUUID,
        primary_key=True,
        default=uuid.uuid4,
        doc="Primary key identifier for the audit log entry",
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        CompatibleUUID,
        ForeignKey("transactions.transaction_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        doc="Foreign key reference to the related transaction",
    )

    llm_proposed_action: Mapped[Optional[Union[str, Dict[str, Any]]]] = mapped_column(
        Text,
        nullable=True,
        doc="Proposed action generated by LLM (e.g. retry strategy, routing)",
    )

    guardrail_decision: Mapped[Optional[Union[str, Dict[str, Any]]]] = mapped_column(
        Text,
        nullable=True,
        doc="Decision, score, or validation output produced by the guardrail filter",
    )

    action: Mapped[str] = mapped_column(
        String(20), nullable=False, default="APPROVED",
        doc="Guardrail outcome: APPROVED, MODIFIED, or OVERRIDDEN",
    )

    modified_parameters: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        CompatibleJSON,
        nullable=True,
        doc="Original and bounded parameter values when action is MODIFIED",
    )

    final_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        doc="Final status of the decision workflow (e.g. RETRIED, EMI_OFFERED, ESCALATED)",
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        doc="Timestamp when the audit log decision was recorded",
    )

    transaction: Mapped["Transaction"] = relationship(
        "Transaction",
        back_populates="audit_logs",
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog(log_id={self.log_id}, tx_id={self.transaction_id}, "
            f"status={self.final_status}, timestamp={self.timestamp})>"
        )


class MerchantPolicy(Base):
    """Merchant-configured financial action limits."""

    __tablename__ = "merchant_policies"

    merchant_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    max_discount_allowed: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("10.00")
    )
