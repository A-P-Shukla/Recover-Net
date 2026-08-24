"""Unit tests for BlindLog secret handling and fail-closed masking."""

import pytest

from security import (
    MaskingError,
    clear_logger_cache,
    get_blind_logger,
    mask_email,
    mask_payload,
    mask_phone,
    require_blindlog_secret,
)


def test_require_secret_fails_when_missing(monkeypatch):
    monkeypatch.delenv("BLINDLOG_SECRET", raising=False)
    clear_logger_cache()
    with pytest.raises(MaskingError, match="BLINDLOG_SECRET"):
        require_blindlog_secret()


def test_mask_email_fails_when_secret_missing(monkeypatch):
    monkeypatch.delenv("BLINDLOG_SECRET", raising=False)
    clear_logger_cache()
    with pytest.raises(MaskingError, match="BLINDLOG_SECRET"):
        mask_email("aditya.sharma770@protonmail.com")


def test_debug_mode_is_refused(monkeypatch):
    monkeypatch.setenv("BLINDLOG_DEBUG", "true")
    clear_logger_cache()
    with pytest.raises(MaskingError, match="BLINDLOG_DEBUG"):
        get_blind_logger()


def test_mask_email_and_phone_change_values():
    raw_email = "aditya.sharma770@protonmail.com"
    raw_phone = "+916798479837"
    masked_email = mask_email(raw_email)
    masked_phone = mask_phone(raw_phone)
    assert masked_email != raw_email
    assert masked_phone != raw_phone
    assert "blnd_ref_" in masked_email
    assert "@masked.com" in masked_email
    assert masked_phone.startswith("blind:") or "blnd_ph_" in masked_phone


def test_mask_payload_never_returns_original_pii():
    payload = {
        "transaction_id": "7bf5d920-0619-4d3e-9d00-108faf65028c",
        "user_email": "vihaaniyer911@gmail.com",
        "phone": "+916772495977",
        "amount": 45735,
        "error_code": "invalid_cvv",
    }
    masked = mask_payload(payload)
    assert masked is not payload
    assert masked["user_email"] != payload["user_email"]
    assert masked["phone"] != payload["phone"]
    assert masked["amount"] == 45735
    assert masked["transaction_id"] == payload["transaction_id"]


def test_mask_payload_rejects_non_dict():
    with pytest.raises(MaskingError, match="dict"):
        mask_payload("not-a-dict")  # type: ignore[arg-type]


def test_empty_email_and_phone_are_rejected():
    with pytest.raises(MaskingError):
        mask_email("")
    with pytest.raises(MaskingError):
        mask_phone("   ")


def test_spoofed_masked_email_is_still_hashed():
    spoofed = "attacker@masked.com"
    masked = mask_email(spoofed)
    assert masked != spoofed
    assert masked.startswith("blnd_ref_")


def test_spoofed_blind_prefix_phone_is_still_hashed():
    spoofed = "blind:+919876543210"
    masked = mask_phone(spoofed)
    assert masked != spoofed


def test_logger_cache_is_keyed_by_secret():
    email = "cache-test@example.com"
    a = mask_email(email, secret_key="secret-alpha-key")
    b = mask_email(email, secret_key="secret-beta-key")
    a_again = mask_email(email, secret_key="secret-alpha-key")
    assert a != b
    assert a == a_again
