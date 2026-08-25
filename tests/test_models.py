import sys
from pathlib import Path

# Ensure project root is on sys.path when running script directly
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from database import Base
from models import AuditLog, Transaction
from security import MaskingError


def _webhook(**overrides):
    data = {
        "transaction_id": str(uuid.uuid4()),
        "user_email": "aditya.sharma770@protonmail.com",
        "phone": "+916798479837",
        "amount": 25000,
        "error_code": "gateway_timeout",
        "past_success_rate": 0.47,
    }
    data.update(overrides)
    return data


def test_init_db_registers_model_tables(db_session):
    assert "transactions" in Base.metadata.tables
    assert "audit_logs" in Base.metadata.tables
    inspector = inspect(db_session.get_bind())
    assert "transactions" in inspector.get_table_names()
    assert "audit_logs" in inspector.get_table_names()


def test_from_webhook_does_not_use_inbound_id_as_pk(db_session):
    inbound = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tx = Transaction.from_webhook(_webhook(transaction_id=inbound))
    db_session.add(tx)
    db_session.flush()

    assert str(tx.transaction_id) != inbound
    assert tx.source_transaction_id == inbound
    assert isinstance(tx.transaction_id, uuid.UUID)


def test_from_webhook_masks_columns_and_payload(db_session):
    raw = _webhook()
    tx = Transaction.from_webhook(raw)
    db_session.add(tx)
    db_session.flush()

    assert tx.user_email != raw["user_email"]
    assert tx.phone != raw["phone"]
    assert tx.raw_payload is not None
    assert tx.raw_payload["user_email"] != raw["user_email"]
    assert tx.raw_payload["phone"] != raw["phone"]
    assert tx.amount == Decimal("25000")
    assert "blnd_ref_" in tx.user_email


def test_from_webhook_does_not_fall_back_to_raw_pii():
    raw = _webhook()
    tx = Transaction.from_webhook(raw)
    assert tx.user_email != raw["user_email"]
    assert tx.phone != raw["phone"]
    assert raw["user_email"] not in str(tx.raw_payload)
    assert raw["phone"] not in str(tx.raw_payload)


def test_validators_always_mask_spoofed_prefixes(db_session):
    tx = Transaction(
        user_email="attacker@masked.com",
        phone="blind:+919876543210",
        amount=Decimal("10.00"),
        error_code="invalid_cvv",
    )
    db_session.add(tx)
    db_session.flush()
    assert tx.user_email != "attacker@masked.com"
    assert tx.phone != "blind:+919876543210"


def test_empty_email_rejected():
    with pytest.raises((ValueError, MaskingError)):
        Transaction(
            user_email="",
            phone="+916798479837",
            amount=Decimal("1.00"),
            error_code="x",
        )


def test_duplicate_source_transaction_id_is_rejected(db_session):
    inbound = str(uuid.uuid4())
    first = Transaction.from_webhook(_webhook(transaction_id=inbound, user_email="one@example.com"))
    second = Transaction.from_webhook(_webhook(transaction_id=inbound, user_email="two@example.com"))
    db_session.add(first)
    db_session.flush()
    db_session.add(second)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_audit_log_relationship(db_session):
    tx = Transaction.from_webhook(_webhook())
    db_session.add(tx)
    db_session.flush()

    audit = AuditLog(
        transaction_id=tx.transaction_id,
        llm_proposed_action="RETRY_EXPONENTIAL_BACKOFF_2M",
        guardrail_decision="APPROVED: Risk score 0.12 below threshold 0.40",
        final_status="QUEUED_FOR_RETRY",
    )
    db_session.add(audit)
    db_session.commit()

    reloaded = db_session.scalar(
        select(Transaction).where(Transaction.transaction_id == tx.transaction_id)
    )
    assert reloaded is not None
    assert len(reloaded.audit_logs) == 1
    assert reloaded.audit_logs[0].final_status == "QUEUED_FOR_RETRY"


def test_from_webhook_requires_email_and_phone():
    with pytest.raises(ValueError, match="user_email"):
        Transaction.from_webhook(_webhook(user_email=""))
    with pytest.raises(ValueError, match="phone"):
        Transaction.from_webhook(_webhook(phone=None))


def test_session_rollback_isolates_tests(db_session):
    """Sanity check that the transactional fixture starts empty."""
    count = db_session.scalar(select(Transaction))
    assert count is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main(["-s", __file__]))
