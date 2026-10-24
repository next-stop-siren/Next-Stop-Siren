"""Access/refresh issuance and verification, independent of HTTP and persistence."""

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

    def __init__(self, token_type: TokenType = "access") -> None:
        super().__init__(f"유효한 {token_type} 토큰이 필요합니다.")


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """쿠키 설정과 세션 저장에 사용할 발급 정보를 함께 보관한다."""

    token: str = field(repr=False)
    token_type: TokenType
    jti: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """서명과 claim 검증을 통과한 정보. 원문 JWT는 보관하지 않는다."""

    user_id: int
    jti: str
    issued_at: datetime
    expires_at: datetime


def _issue_token(
    user_id: int,
    settings: AuthSettings,
    token_type: TokenType,
    now: datetime | None,
    absolute_expires_at: datetime | None = None,
) -> IssuedToken:
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
    if absolute_expires_at is not None:
        if absolute_expires_at.utcoffset() is None:
            raise TokenIssueError("시간대가 있는 절대 만료 시각이 필요합니다.")
        # JWT 정수 초와 DB 메타데이터를 맞추고, 입력 만료를 연장하지 않는다.
        inherited_expiry = absolute_expires_at.astimezone(timezone.utc).replace(microsecond=0)
        if not issued_at < inherited_expiry <= expires_at:
            raise TokenIssueError("절대 만료는 발급 이후이며 최대 수명 이내여야 합니다.")
        expires_at = inherited_expiry
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


def issue_refresh_token(
    user_id: int,
    settings: AuthSettings,
    *,
    now: datetime | None = None,
    absolute_expires_at: datetime | None = None,
) -> IssuedToken:
    """최초 발급은 14일, 회전 발급은 호출자가 확인한 루트 만료를 전달한다.

    루트와의 일치 여부는 DB를 사용하는 서비스가 확인한다. 이 함수는 전달받은
    만료를 늘리지 않고, 남은 수명이 없거나 최대 14일을 넘으면 발급하지 않는다.
    """
    return _issue_token(user_id, settings, "refresh", now, absolute_expires_at)


def verify_access_token(token: str, settings: AuthSettings) -> int:
    """기존 Bearer 인증 계약대로 검증된 사용자 ID만 반환한다."""
    return verify_access(token, settings).user_id


def verify_access(token: str, settings: AuthSettings) -> VerifiedToken:
    """access를 검증하고 만료 시각까지 반환한다. 오래 열린 연결이 만료에 끊을 때 쓴다."""
    return _verify_token(token, settings, "access")


def verify_refresh_token(token: str, settings: AuthSettings) -> VerifiedToken:
    """refresh 전용 키와 종류를 검증한다. DB 세션의 활성 여부는 별도 확인한다."""
    return _verify_token(token, settings, "refresh")


def _verify_token(token: str, settings: AuthSettings, token_type: TokenType) -> VerifiedToken:
    """서버가 정한 종류별 정책으로 서명·필수 claim을 모두 검증한다."""
    if token_type == "access":
        key, kid, ttl = settings.access_key, settings.access_kid, settings.access_ttl_seconds
    else:
        key, kid, ttl = settings.refresh_key, settings.refresh_kid, settings.refresh_ttl_seconds
    if type(token) is not str or not token:
        raise TokenVerifyError(token_type)
    try:
        # 허용 알고리즘과 키는 서버 설정으로 고정한다. 토큰의 헤더로 선택하지 않는다.
        decoded = jwt.decode_complete(
            token,
            key,
            algorithms=["HS256"],
            issuer=settings.issuer,
            audience=settings.audience,
            options={"require": _REQUIRED_CLAIMS, "strict_aud": True},
        )
    except (jwt.PyJWTError, TypeError, ValueError, OverflowError):
        # 잘못된 claim 타입의 변환 오류도 내부 내용 없이 동일하게 거부한다.
        raise TokenVerifyError(token_type) from None
    header, claims = decoded["header"], decoded["payload"]
    if (
        header.get("kid") != kid
        or header.get("typ") != "JWT"
        or "crit" in header
        or "b64" in header
        or claims["token_type"] != token_type
    ):
        raise TokenVerifyError(token_type)
    subject = claims["sub"]
    if type(subject) is not str or _USER_ID.fullmatch(subject) is None:
        raise TokenVerifyError(token_type)
    user_id = int(subject)
    if user_id > 9223372036854775807:
        raise TokenVerifyError(token_type)
    # PyJWT가 숫자로 변환할 수 있는 값이라도 문자열·실수·bool 시각은 허용하지 않는다.
    issued_at, not_before, expires_at = claims["iat"], claims["nbf"], claims["exp"]
    if any(type(value) is not int or value < 0 for value in (issued_at, not_before, expires_at)):
        raise TokenVerifyError(token_type)
    if not issued_at <= not_before < expires_at or not 0 < expires_at - issued_at <= ttl:
        raise TokenVerifyError(token_type)
    # 발급 함수가 사용하는 표준 UUID v4 표기만 토큰 식별자로 허용한다.
    jti = claims["jti"]
    try:
        identifier = UUID(jti) if type(jti) is str else None
    except ValueError:
        raise TokenVerifyError(token_type) from None
    if identifier is None or identifier.version != 4 or str(identifier) != jti:
        raise TokenVerifyError(token_type)
    try:
        return VerifiedToken(
            user_id=user_id,
            jti=jti,
            issued_at=datetime.fromtimestamp(issued_at, timezone.utc),
            expires_at=datetime.fromtimestamp(expires_at, timezone.utc),
        )
    except (ValueError, OverflowError, OSError):
        raise TokenVerifyError(token_type) from None
