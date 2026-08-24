"""
security.py

Privacy and security utility module using BlindLog for deterministic
pseudonymization of sensitive PII (emails, phone numbers).

Fails closed: hashing never proceeds without BLINDLOG_SECRET, and a payload
is never returned if masking did not actually change known PII fields.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, Dict, Iterator, Optional

from blindlog import BlindLogConfig, BlindLogger
from blindlog.rules import MASKED_PATTERN
from dotenv import load_dotenv

load_dotenv()

_PII_EMAIL_KEYS = ("user_email", "email")
_PII_PHONE_KEYS = ("phone", "mobile")

_secret_override: ContextVar[Optional[str]] = ContextVar(
    "blindlog_secret_override", default=None
)


class MaskingError(RuntimeError):
    """Raised when PII cannot be masked or a secret is missing."""


def _looks_masked(value: str) -> bool:
    """True only for BlindLog's own mask format, not spoofable prefixes."""
    return bool(MASKED_PATTERN.match(value))


def resolve_secret(secret_key: Optional[str] = None) -> str:
    """
    Resolve the BlindLog secret from an explicit argument, a context override,
    or BLINDLOG_SECRET. Never falls back to a hardcoded default.
    """
    secret = secret_key or _secret_override.get() or os.getenv("BLINDLOG_SECRET")
    if secret is not None:
        secret = str(secret).strip()
    if not secret:
        raise MaskingError(
            "BLINDLOG_SECRET is not set. Refusing to hash with a default key."
        )
    return secret


def require_blindlog_secret() -> str:
    """
    Fail closed at process start if hashing cannot be performed safely.
    """
    if os.getenv("BLINDLOG_DEBUG", "").lower() in ("true", "1", "yes"):
        raise MaskingError(
            "BLINDLOG_DEBUG is enabled; refusing to run because masking would be a no-op."
        )
    return resolve_secret()


@contextmanager
def using_secret(secret_key: str) -> Iterator[None]:
    """Apply a secret for the current context (payload masking + ORM validators)."""
    resolved = str(secret_key).strip() if secret_key else ""
    if not resolved:
        raise MaskingError(
            "BLINDLOG_SECRET is not set. Refusing to hash with a default key."
        )
    token = _secret_override.set(resolved)
    try:
        yield
    finally:
        _secret_override.reset(token)


@lru_cache(maxsize=16)
def _logger_for_secret(secret: str) -> BlindLogger:
    return BlindLogger(
        BlindLogConfig(
            secret_key=secret,
            debug_mode=False,
        )
    )


def clear_logger_cache() -> None:
    """Drop cached BlindLogger instances (used by tests when the secret changes)."""
    _logger_for_secret.cache_clear()


def get_blind_logger(secret_key: Optional[str] = None) -> BlindLogger:
    """
    Return a BlindLogger cached by the resolved secret string.

    Cache keys are the actual secret, so two different secrets never share a
    logger, and a later call with secret A is not served a logger built for B.
    """
    if os.getenv("BLINDLOG_DEBUG", "").lower() in ("true", "1", "yes"):
        raise MaskingError(
            "BLINDLOG_DEBUG is enabled; refusing to run because masking would be a no-op."
        )
    return _logger_for_secret(resolve_secret(secret_key))


def _masked_or_raise(original: str, masked: str, field: str) -> str:
    if not masked:
        raise MaskingError(f"BlindLog returned an empty value for {field}")
    if masked == original and not _looks_masked(masked):
        raise MaskingError(f"Refusing to persist unmasked {field}")
    return masked


def mask_email(email: Optional[str], secret_key: Optional[str] = None) -> Optional[str]:
    """
    Mask an email address deterministically using BlindLog.

    Always runs BlindLog. Does not skip values based on prefixes such as
    blnd_ or @masked.com. Empty strings are rejected.
    """
    if email is None:
        return None
    if not str(email).strip():
        raise MaskingError("user_email is required and cannot be empty")
    original = str(email)
    logger = get_blind_logger(secret_key)
    result = logger.mask({"user_email": original})
    if not isinstance(result, dict) or "user_email" not in result:
        raise MaskingError("BlindLog did not mask user_email")
    return _masked_or_raise(original, str(result["user_email"]), "user_email")


def mask_phone(phone: Optional[str], secret_key: Optional[str] = None) -> Optional[str]:
    """
    Mask a phone number deterministically using BlindLog.

    Always runs BlindLog. Does not skip values based on a blind: prefix.
    Empty strings are rejected.
    """
    if phone is None:
        return None
    if not str(phone).strip():
        raise MaskingError("phone is required and cannot be empty")
    original = str(phone)
    logger = get_blind_logger(secret_key)
    result = logger.mask({"phone": original})
    if not isinstance(result, dict) or "phone" not in result:
        raise MaskingError("BlindLog did not mask phone")
    return _masked_or_raise(original, str(result["phone"]), "phone")


def mask_payload(payload: Dict[str, Any], secret_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Mask sensitive fields in a payload dict using BlindLog.

    Never returns the original payload. Known PII keys are re-masked from the
    original values and compared so a no-op cannot be persisted.
    """
    if not isinstance(payload, dict):
        raise MaskingError("payload must be a dict")

    logger = get_blind_logger(secret_key)
    masked = logger.mask(payload)
    if not isinstance(masked, dict):
        raise MaskingError(
            "BlindLog did not return a dict; refusing to store unmasked payload"
        )

    result = dict(masked)

    for key in _PII_EMAIL_KEYS:
        raw = payload.get(key)
        if raw:
            result[key] = mask_email(str(raw), secret_key)

    for key in _PII_PHONE_KEYS:
        raw = payload.get(key)
        if raw:
            result[key] = mask_phone(str(raw), secret_key)

    for key in _PII_EMAIL_KEYS + _PII_PHONE_KEYS:
        raw = payload.get(key)
        stored = result.get(key)
        if raw and stored == raw:
            raise MaskingError(f"Refusing to persist unmasked field: {key}")

    return result
