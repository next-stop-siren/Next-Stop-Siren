"""Digest and CSRF values for a server-issued refresh session."""

import base64
import hmac

from app.security.settings import AuthSettings


def create_refresh_digest(refresh_token: str, settings: AuthSettings) -> bytes:
    """Return only the 32-byte HMAC to persist in refresh_sessions."""
    return hmac.digest(settings.refresh_digest_key, refresh_token.encode("utf-8"), "sha256")


def create_csrf_token(refresh_jti: str, settings: AuthSettings) -> str:
    """Bind a cookie-safe MAC to the current refresh jti with a separate key."""
    message = b"csrf:v1:" + refresh_jti.encode("utf-8")
    mac = hmac.digest(settings.csrf_key, message, "sha256")
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def verify_csrf_token(refresh_jti: str, cookie: str | None, header: str | None, settings: AuthSettings) -> bool:
    """검증된 refresh jti와 쿠키·헤더의 MAC을 비교한다. 실패는 상태를 변경하지 않는다."""
    # compare_digest(str, str)는 ASCII만 허용한다. 누락·비정상 입력도 안전하게 거부한다.
    if any(type(value) is not str or len(value) != 43 or not value.isascii() for value in (cookie, header)):
        return False
    assert cookie is not None and header is not None
    expected = create_csrf_token(refresh_jti, settings)
    same_pair = hmac.compare_digest(cookie, header)
    bound_to_refresh = hmac.compare_digest(cookie, expected)
    return same_pair and bound_to_refresh
