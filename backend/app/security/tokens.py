"""Initial access and refresh issuance, independent of HTTP and persistence."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

import jwt

from app.security.settings import AuthSettings

TokenType = Literal["access", "refresh"]


class TokenIssueError(Exception):
    """Safe issuance failure without raw tokens, keys or signing error details."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """Keep token metadata available for later cookie and session creation."""

    token: str = field(repr=False)
    token_type: TokenType
    jti: str
    issued_at: datetime
    expires_at: datetime


def _issue_token(user_id: int, settings: AuthSettings, token_type: TokenType, now: datetime | None) -> IssuedToken:
    if type(user_id) is not int or not 1 <= user_id <= 9223372036854775807:
        raise TokenIssueError("유효한 사용자 ID가 필요합니다.")
    issued_at = now if now is not None else datetime.now(timezone.utc)
    if issued_at.utcoffset() is None:
        raise TokenIssueError("시간대가 있는 발급 시각이 필요합니다.")
    # Match database metadata to the whole-second NumericDate stored in JWT.
    issued_at = issued_at.astimezone(timezone.utc).replace(microsecond=0)
    if token_type == "access":
        key, kid, ttl = settings.access_key, settings.access_kid, settings.access_ttl_seconds
    else:
        key, kid, ttl = settings.refresh_key, settings.refresh_kid, settings.refresh_ttl_seconds
    expires_at = issued_at + timedelta(seconds=ttl)
    jti = str(uuid4())
    payload = {
        "iss": settings.issuer,
        "aud": settings.audience,
        "sub": str(user_id),
        "iat": int(issued_at.timestamp()),
        "nbf": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": jti,
        "token_type": token_type,
    }
    try:
        token = jwt.encode(payload, key, algorithm="HS256", headers={"kid": kid})
    except jwt.PyJWTError:
        raise TokenIssueError("인증 토큰을 발급할 수 없습니다.") from None
    return IssuedToken(token=token, token_type=token_type, jti=jti, issued_at=issued_at, expires_at=expires_at)


def issue_access_token(user_id: int, settings: AuthSettings, *, now: datetime | None = None) -> IssuedToken:
    """Create a 15-minute access JWT for a server-selected user ID."""
    return _issue_token(user_id, settings, "access", now)


def issue_refresh_token(user_id: int, settings: AuthSettings, *, now: datetime | None = None) -> IssuedToken:
    """Create an initial 14-day refresh JWT; rotation must retain root expiry."""
    return _issue_token(user_id, settings, "refresh", now)
