"""Verify Google ID token signatures and OIDC claims before exposing an identity."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import re

from authlib.oidc.core import CodeIDToken  # type: ignore[import-untyped]  # Authlib은 타입 정보를 제공하지 않는다.
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import Key, KeySet, RSAKey

from app.security.google_transactions import GoogleTransaction
from app.security.settings import GoogleSettings

_COMPACT_JWT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_EMAIL = re.compile(r"[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+\.[^\s@\x00-\x1f\x7f]+")


class GoogleIdentityError(ValueError):
    """검증 실패 원인이나 code·토큰 원문을 노출하지 않는 공통 인증 오류."""

    def __init__(self) -> None:
        super().__init__("Google 로그인 정보를 확인할 수 없습니다. 다시 로그인해 주세요.")


@dataclass(frozen=True, slots=True)
class VerifiedGoogleIdentity:
    """모든 검증을 통과한 신원. 이메일은 계정 식별 키로 사용하지 않는다."""

    issuer: str = field(repr=False)
    subject: str = field(repr=False)
    email: str = field(repr=False)


def verify_google_id_token(
    id_token: str,
    access_token: str,
    keys: KeySet,
    transaction: GoogleTransaction,
    settings: GoogleSettings,
    *,
    now: datetime | None = None,
) -> VerifiedGoogleIdentity:
    """서명·OIDC claim·검증된 이메일을 모두 확인한 뒤 최소 신원만 반환한다."""
    if not isinstance(id_token, str) or len(id_token) > 16384 or _COMPACT_JWT.fullmatch(id_token) is None:
        raise GoogleIdentityError()
    if (
        not isinstance(access_token, str)
        or not 1 <= len(access_token) <= 16384
        or any(not 33 <= ord(character) <= 126 for character in access_token)
    ):
        raise GoogleIdentityError()
    current = now if now is not None else datetime.now(timezone.utc)
    if current.utcoffset() is None:
        raise GoogleIdentityError()
    try:
        # 공개 RSA 서명 검증 키만 선택한다. 토큰 헤더가 지정한 외부 키 주소는 조회하지 않는다.
        eligible: list[Key] = [
            key
            for key in keys.keys
            if isinstance(key, RSAKey)
            and not key.is_private
            and key.public_key.key_size >= 2048
            and key.get("use") in (None, "sig")
            and key.get("alg") in (None, "RS256")
            and key.get("key_ops") in (None, ["verify"])
        ]
        identifiers = [key.kid for key in eligible]
        if (
            not eligible
            or any(not isinstance(kid, str) or not kid for kid in identifiers)
            or len(set(identifiers)) != len(identifiers)
        ):
            raise GoogleIdentityError()
        decoded = jwt.decode(id_token, KeySet(eligible), algorithms=settings.id_token_algorithms)
        header, claims = decoded.header, decoded.claims
        if (
            set(header) - {"alg", "kid", "typ"}
            or header.get("alg") != "RS256"
            or not isinstance(header.get("kid"), str)
            or not header["kid"]
            or header.get("typ") not in (None, "JWT")
            or not isinstance(claims, dict)
        ):
            raise GoogleIdentityError()
        issued_at, expires_at = claims.get("iat"), claims.get("exp")
        if (
            type(issued_at) is not int
            or type(expires_at) is not int
            or not 0 <= issued_at <= current.timestamp() < expires_at
            or issued_at >= expires_at
            or claims.get("iss") not in settings.accepted_issuers
        ):
            raise GoogleIdentityError()
        audience = claims.get("aud")
        if isinstance(audience, str):
            valid_audience = audience == settings.client_id
        elif isinstance(audience, list) and audience and all(isinstance(value, str) and value for value in audience):
            valid_audience = settings.client_id in audience and len(set(audience)) == len(audience)
        else:
            valid_audience = False
        subject, email, nonce = claims.get("sub"), claims.get("email"), claims.get("nonce")
        if (
            not valid_audience
            or not isinstance(subject, str)
            or not 1 <= len(subject) <= 255
            or not subject.isascii()
            or any(not 33 <= ord(character) <= 126 for character in subject)
            or not isinstance(email, str)
            or not 3 <= len(email) <= 320
            or _EMAIL.fullmatch(email) is None
            or claims.get("email_verified") is not True
            or not isinstance(nonce, str)
            or not hmac.compare_digest(nonce.encode("utf-8"), transaction.nonce.encode("ascii"))
        ):
            raise GoogleIdentityError()
        if "nbf" in claims and (type(claims["nbf"]) is not int or not 0 <= claims["nbf"] <= current.timestamp()):
            raise GoogleIdentityError()
        # Authlib에도 요청 nonce·client ID·access를 전달해 azp와 선택적 at_hash까지 검사한다.
        oidc = CodeIDToken(
            claims,
            header,
            options={"iss": {"values": list(settings.accepted_issuers)}, "aud": {"value": settings.client_id}},
            params={"nonce": transaction.nonce, "client_id": settings.client_id, "access_token": access_token},
        )
        oidc.validate(now=current.timestamp(), leeway=0)
    except (JoseError, ValueError, TypeError, KeyError, OverflowError):
        raise GoogleIdentityError() from None
    # Google이 허용하는 두 issuer 표기를 하나로 저장해 같은 신원이 중복 생성되지 않게 한다.
    return VerifiedGoogleIdentity(issuer=settings.issuer, subject=subject, email=email)
