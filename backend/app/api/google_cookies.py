"""Host-only transaction cookie helpers for the later Google start/callback API."""

from datetime import datetime, timezone

from fastapi import Response

from app.security.google_transactions import GoogleTransaction, GoogleTransactionError, encode_google_transaction
from app.security.settings import AuthSettings, GoogleSettings

GOOGLE_TRANSACTION_COOKIE_NAME = "google_transaction"


def set_google_transaction_cookie(
    response: Response,
    transaction: GoogleTransaction,
    google_settings: GoogleSettings,
    auth_settings: AuthSettings,
    *,
    now: datetime | None = None,
) -> None:
    """헤더를 설정하기 전에 암호화하며 트랜잭션에 담긴 절대 만료 시각을 늘리지 않는다."""
    current = now if now is not None else datetime.now(timezone.utc)
    value = encode_google_transaction(transaction, google_settings, now=current)
    remaining = int(transaction.expires_at - current.timestamp())
    if remaining <= 0:
        raise GoogleTransactionError()
    response.set_cookie(
        key=GOOGLE_TRANSACTION_COOKIE_NAME,
        value=value,
        max_age=remaining,
        expires=datetime.fromtimestamp(transaction.expires_at, timezone.utc),
        path=auth_settings.cookie_path,
        secure=auth_settings.cookie_secure,
        httponly=auth_settings.cookie_httponly,
        samesite=auth_settings.cookie_samesite,
    )


def clear_google_transaction_cookie(response: Response, auth_settings: AuthSettings) -> None:
    """같은 호스트·경로의 트랜잭션 쿠키만 만료시키고 다른 인증 쿠키는 유지한다."""
    response.delete_cookie(
        key=GOOGLE_TRANSACTION_COOKIE_NAME,
        path=auth_settings.cookie_path,
        secure=auth_settings.cookie_secure,
        httponly=auth_settings.cookie_httponly,
        samesite=auth_settings.cookie_samesite,
    )
