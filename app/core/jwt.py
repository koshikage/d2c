"""
JWT Creation and Verification
================================
JWTs carry brand_id + user_id, which drives all tenant scoping.

SecretStr fix:
  settings.JWT_SECRET_KEY is a SecretStr (masked in logs, stored safely).
  python-jose's jwt.encode/decode expect a plain str or bytes.
  We call .get_secret_value() at the point of use — never at module load time.
  This also means the real production key is available (it arrives after
  build_settings() runs in the lifespan, before any request is served).
"""
import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class TokenPayload:
    """Typed wrapper around decoded JWT claims."""

    def __init__(self, data: dict):
        self.user_id: str = data["sub"]
        self.brand_id: str = data["brand_id"]
        self.email: str = data.get("email", "")
        self.jti: str = data.get("jti", "")


def create_access_token(user_id: str, brand_id: str, email: str) -> str:
    """Issue a signed JWT. Called after successful OAuth login."""
    now = datetime.now(tz=timezone.utc)
    expire = now + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)

    payload = {
        "sub": str(user_id),
        "brand_id": str(brand_id),
        "email": email,
        "iat": now,
        "exp": expire,
        "jti": str(uuid.uuid4()),
    }

    # .get_secret_value() unwraps SecretStr → plain str for python-jose
    token = jwt.encode(
        payload,
        settings.JWT_SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )
    logger.info("jwt_issued", user_id=user_id, brand_id=brand_id, expires_at=expire.isoformat())
    return token


def decode_access_token(token: str) -> TokenPayload:
    """
    Decode and verify a JWT.
    Raises JWTError on invalid signature, expiry, or missing claims.
    """
    try:
        data = jwt.decode(
            token,
            settings.JWT_SECRET_KEY.get_secret_value(),  # unwrap SecretStr
            algorithms=[settings.JWT_ALGORITHM],
        )
        if "sub" not in data or "brand_id" not in data:
            raise JWTError("Missing required claims: sub, brand_id")
        return TokenPayload(data)
    except JWTError as e:
        logger.warning("jwt_verification_failed", error=str(e))
        raise