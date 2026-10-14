"""Access/refresh issuance and access verification, independent of HTTP and persistence."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Literal
from uuid import UUID, uuid4

import jwt

from app.security.settings import AuthSettings

TokenType = Literal["access", "refresh"]
_REQUIRED_CLAIMS = ["iss", "aud", "sub", "iat", "nbf", "exp", "jti", "token_type"]
_USER_ID = re.compile(r"[1-9][0-9]{0,18}")


class TokenIssueError(Exception):
    """토큰·키·서명 오류 원문을 노출하지 않는 발급 오류."""


class TokenVerifyError(Exception):
    """검증 실패 이유나 인증 정보를 노출하지 않는 공통 토큰 오류."""

    def __init__(self) -> None:
        super().__init__("유효한 access 토큰이 필요합니다.")


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """쿠키 설정과 세션 저장에 사용할 발급 정보를 함께 보관한다."""

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
    # DB에 저장할 시각을 JWT의 정수 초 단위 시각과 맞춘다.
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
    """서버가 확인한 사용자 ID로 15분짜리 access JWT를 발급한다."""
    return _issue_token(user_id, settings, "access", now)


def issue_refresh_token(user_id: int, settings: AuthSettings, *, now: datetime | None = None) -> IssuedToken:
    """최초 14일짜리 refresh JWT를 발급한다. 회전 시 최초 만료 시각을 유지해야 한다."""
    return _issue_token(user_id, settings, "refresh", now)


def verify_access_token(token: str, settings: AuthSettings) -> int:
    """서명과 필수 정보를 모두 검사한 뒤에만 확인된 사용자 ID를 반환한다."""
    if type(token) is not str or not token:
        raise TokenVerifyError()
    try:
        # 허용 알고리즘과 키는 서버 설정으로 고정한다. 토큰의 헤더로 선택하지 않는다.
        decoded = jwt.decode_complete(
            token,
            settings.access_key,
            algorithms=["HS256"],
            issuer=settings.issuer,
            audience=settings.audience,
            options={"require": _REQUIRED_CLAIMS, "strict_aud": True},
        )
    except (jwt.PyJWTError, TypeError, ValueError, OverflowError):
        # 잘못된 claim 타입의 변환 오류도 내부 내용 없이 동일하게 거부한다.
        raise TokenVerifyError() from None
    header, claims = decoded["header"], decoded["payload"]
    if (
        header.get("kid") != settings.access_kid
        or header.get("typ") != "JWT"
        or "crit" in header
        or "b64" in header
        or claims["token_type"] != "access"
    ):
        raise TokenVerifyError()
    subject = claims["sub"]
    if type(subject) is not str or _USER_ID.fullmatch(subject) is None:
        raise TokenVerifyError()
    user_id = int(subject)
    if user_id > 9223372036854775807:
        raise TokenVerifyError()
    # PyJWT가 숫자로 변환할 수 있는 값이라도 문자열·실수·bool 시각은 허용하지 않는다.
    issued_at, not_before, expires_at = claims["iat"], claims["nbf"], claims["exp"]
    if any(type(value) is not int or value < 0 for value in (issued_at, not_before, expires_at)):
        raise TokenVerifyError()
    if not issued_at <= not_before < expires_at or not 0 < expires_at - issued_at <= settings.access_ttl_seconds:
        raise TokenVerifyError()
    # 발급 함수가 사용하는 표준 UUID v4 표기만 토큰 식별자로 허용한다.
    jti = claims["jti"]
    try:
        identifier = UUID(jti) if type(jti) is str else None
    except ValueError:
        raise TokenVerifyError() from None
    if identifier is None or identifier.version != 4 or str(identifier) != jti:
        raise TokenVerifyError()
    return user_id
