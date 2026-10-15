"""Validate a Google callback into a verified identity without database or HTTP responses."""

from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.integrations.google_oidc import exchange_google_code, google_callback_metadata, google_signing_keys
from app.security.google_identity import GoogleIdentityError, VerifiedGoogleIdentity, verify_google_id_token
from app.security.google_transactions import GoogleTransactionError, verify_google_transaction
from app.security.settings import GoogleSettings


@dataclass(frozen=True, slots=True)
class VerifiedGoogleLogin:
    """신원 검증 이후 단계에 전달할 최소 신원과 허용된 복귀 경로."""

    identity: VerifiedGoogleIdentity = field(repr=False)
    return_to: str


async def verify_google_login(
    code: str | None,
    state: str | None,
    cookie_value: str | None,
    settings: GoogleSettings,
    client: httpx.AsyncClient,
    *,
    now: datetime | None = None,
) -> VerifiedGoogleLogin:
    """쿠키·state를 먼저 검사하고 Google 교환과 신원 검증을 마친 뒤에만 결과를 반환한다."""
    try:
        transaction = verify_google_transaction(cookie_value, state, settings, now=now)
    except GoogleTransactionError:
        raise GoogleIdentityError() from None
    if (
        not isinstance(code, str)
        or not 1 <= len(code) <= 4096
        or any(not 33 <= ord(character) <= 126 for character in code)
    ):
        raise GoogleIdentityError()
    metadata = await google_callback_metadata(settings, client)
    tokens = await exchange_google_code(code, transaction, metadata, settings, client)
    keys = await google_signing_keys(metadata, client)
    identity = verify_google_id_token(tokens.id_token, tokens.access_token, keys, transaction, settings, now=now)
    try:
        # 네트워크 조회 중 트랜잭션이 만료됐을 때도 신원을 후속 단계에 넘기지 않는다.
        verify_google_transaction(cookie_value, state, settings, now=now)
    except GoogleTransactionError:
        raise GoogleIdentityError() from None
    return VerifiedGoogleLogin(identity=identity, return_to=transaction.return_to)
