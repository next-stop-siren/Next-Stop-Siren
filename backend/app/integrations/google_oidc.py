"""Google OIDC discovery and authorization URL construction with Authlib."""

import asyncio

from authlib.oauth2.client import OAuth2Client  # type: ignore[import-untyped]  # Authlib은 타입 정보를 제공하지 않는다.
import httpx

from app.observability import FailureDetails
from app.security.google_transactions import GoogleTransaction
from app.security.settings import GoogleSettings

GOOGLE_DISCOVERY_TIMEOUT_SECONDS = 5
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_ISSUER = "https://accounts.google.com"


class GoogleProviderUnavailable(Exception):
    """Google 응답 원문과 자격 증명을 숨기고 안전한 실패 정보만 전달한다."""

    def __init__(self, failure: FailureDetails) -> None:
        super().__init__("Google 로그인을 시작할 수 없습니다. 잠시 후 다시 시도해 주세요.")
        self.failure = failure


async def google_authorization_url(
    transaction: GoogleTransaction, settings: GoogleSettings, client: httpx.AsyncClient
) -> str:
    """고정된 Discovery 주소를 조회하고 검증한 Google 주소로만 로그인 요청을 만든다."""
    try:
        # 연결별 제한 외에 전체 조회 시간도 제한해 응답이 조금씩 이어지는 경우를 막는다.
        async with asyncio.timeout(GOOGLE_DISCOVERY_TIMEOUT_SECONDS):
            response = await client.get(settings.discovery_url, follow_redirects=False)
            response.raise_for_status()
            metadata = response.json()
        if (
            not isinstance(metadata, dict)
            or metadata.get("issuer") != GOOGLE_ISSUER
            or metadata.get("authorization_endpoint") != GOOGLE_AUTHORIZATION_ENDPOINT
        ):
            raise ValueError("Google Discovery 응답을 확인할 수 없습니다.")
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        raise GoogleProviderUnavailable(FailureDetails.from_exception("google_discovery", exc)) from None

    # URL 구성만 Authlib에 맡긴다. 아직 code 교환을 하지 않으므로 secret과 HTTP 세션은 전달하지 않는다.
    oauth = OAuth2Client(
        session=None,
        client_id=settings.client_id,
        redirect_uri=settings.redirect_uri,
        scope=" ".join(settings.scopes),
        code_challenge_method="S256",
    )
    url, _ = oauth.create_authorization_url(
        metadata["authorization_endpoint"],
        state=transaction.state,
        nonce=transaction.nonce,
        code_verifier=transaction.code_verifier,
    )
    return str(url)
