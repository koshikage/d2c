"""
Token Encryption at Rest — Fernet symmetric encryption
========================================================
OAuth access tokens stored in the DB are encrypted using Fernet
(AES-128-CBC + HMAC-SHA256).

WHY LAZY INITIALISATION (_fernet is None until first use):
  The old code built _fernet at import time:
      _fernet = _build_fernet()   ← ran when Python loaded the module

  That crashed the moment the module was imported because:
    1. settings.ENCRYPTION_KEY is a SecretStr — calling .split() on it
       raises AttributeError: 'SecretStr' object has no attribute 'split'
    2. In production, the key is a placeholder at import time anyway —
       the real value only arrives after build_settings() runs in the
       FastAPI lifespan.

  Fix: build _fernet lazily on the first actual encrypt/decrypt call.
  By that time, the lifespan has run, secrets are loaded, and
  settings.ENCRYPTION_KEY.get_secret_value() returns the real key.

KEY ROTATION:
  Set ENCRYPTION_KEY=new_key,old_key (comma-separated).
  MultiFernet encrypts with new_key, decrypts with either.
  After all old tokens are re-encrypted, drop old_key from the value.
"""
import base64

from cryptography.fernet import Fernet, MultiFernet

from app.core.logging import get_logger

logger = get_logger(__name__)

# Module-level sentinel. Built on first use, not at import time.
_fernet: MultiFernet | None = None


def _build_fernet() -> MultiFernet:
    """
    Build MultiFernet from settings.ENCRYPTION_KEY.

    Called lazily on the first encrypt/decrypt call — by which point
    build_settings() has already populated the real key value.

    Handles:
      - SecretStr: calls .get_secret_value() to unwrap
      - Comma-separated keys for rotation: "new_key,old_key"
      - Dev placeholder strings (not valid Fernet base64): derives a
        deterministic key so local dev works without generating a real key
    """
    # Import here (not at module top) to avoid circular import at load time
    from app.core.config import settings

    # Always call .get_secret_value() — ENCRYPTION_KEY is SecretStr
    raw: str = settings.ENCRYPTION_KEY.get_secret_value()

    key_strings = [k.strip() for k in raw.split(",") if k.strip()]
    if not key_strings:
        raise RuntimeError(
            "ENCRYPTION_KEY is empty. "
            "Set it in .env (dev) or GCP Secret Manager (production).\n"
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    fernets: list[Fernet] = []
    for key_str in key_strings:
        key_bytes = key_str.encode()

        if len(key_bytes) == 44:
            # Looks like a real Fernet key (44 url-safe base64 chars = 32 bytes)
            try:
                fernets.append(Fernet(key_bytes))
                continue
            except Exception:
                pass  # fall through to the derivation path

        # Not a valid Fernet key — derive one deterministically from the string.
        # This only happens in local dev with placeholder values like
        # "dev_encryption_key_NOT_FOR_PRODUCTION".
        # In production build_settings() replaces this with the real Secret Manager value.
        logger.warning(
            "encryption_key_not_valid_fernet",
            hint="Using derived key from plaintext string. Only acceptable in development.",
            key_prefix=key_str[:8] + "...",
        )
        padded = key_str.encode()[:32].ljust(32, b"\x00")
        fernets.append(Fernet(base64.urlsafe_b64encode(padded)))

    return MultiFernet(fernets)


def _get_fernet() -> MultiFernet:
    """
    Return the module-level MultiFernet, building it on first call.
    Thread-safe: worst case two threads build it simultaneously and one
    result is discarded — both are equivalent because settings is immutable
    after lifespan startup.
    """
    global _fernet
    if _fernet is None:
        _fernet = _build_fernet()
    return _fernet


def reset_fernet() -> None:
    """
    Force _fernet to be rebuilt on the next call to encrypt_token/decrypt_token.
    Call this in tests after changing ENCRYPTION_KEY, or after key rotation.
    """
    global _fernet
    _fernet = None


def encrypt_token(plaintext: str) -> str:
    """Encrypt an OAuth token. Returns a Fernet token (base64 ciphertext)."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """
    Decrypt a Fernet token. Raises InvalidToken if the ciphertext was
    tampered with or was encrypted with an unknown key.
    """
    return _get_fernet().decrypt(ciphertext.encode()).decode()