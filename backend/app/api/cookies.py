"""Refresh and CSRF cookie creation after a successful session commit."""

from datetime import datetime, timezone

from fastapi import Response

from app.security.session_values import create_csrf_token
from app.security.settings import AuthSettings
from app.security.tokens import IssuedToken

REFRESH_COOKIE_NAME = "refresh_token"
CSRF_COOKIE_NAME = "csrf_token"


class AuthCookieError(Exception):
    """Invalid internal token metadata; no cookies should be issued."""


def set_auth_cookies(
    response: Response,
    refresh: IssuedToken,
    settings: AuthSettings,
    *,
    now: datetime | None = None,
) -> str:
    """Set host-only refresh/CSRF cookies and return CSRF for the response body.

    Callers must commit the session before calling this function. It uses trusted
    issuance metadata; verification of incoming tokens belongs to later routes.
    """
    if refresh.token_type != "refresh":
        raise AuthCookieError("쿠키에는 refresh 토큰만 사용할 수 있습니다.")
    current = now if now is not None else datetime.now(timezone.utc)
    if current.utcoffset() is None or refresh.expires_at.utcoffset() is None:
        raise AuthCookieError("시간대가 있는 쿠키 만료 시각이 필요합니다.")
    expires_at = refresh.expires_at.astimezone(timezone.utc)
    # Round down so time spent storing the session cannot extend its lifetime.
    remaining = int((expires_at - current).total_seconds())
    if remaining <= 0:
        raise AuthCookieError("만료된 refresh 쿠키를 발급할 수 없습니다.")
    csrf_token = create_csrf_token(refresh.jti, settings)
    for name, value in ((REFRESH_COOKIE_NAME, refresh.token), (CSRF_COOKIE_NAME, csrf_token)):
        response.set_cookie(
            key=name,
            value=value,
            max_age=remaining,
            expires=expires_at,
            path=settings.cookie_path,
            secure=settings.cookie_secure,
            httponly=settings.cookie_httponly,
            samesite=settings.cookie_samesite,
        )
    return csrf_token
