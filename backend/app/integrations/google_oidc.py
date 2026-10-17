"""Google OIDC discovery, authorization requests, code exchange and signing keys."""

import asyncio
from dataclasses import dataclass, field
from typing import cast

from authlib.oauth2.client import OAuth2Client  # type: ignore[import-untyped]  # Authlib은 타입 정보를 제공하지 않는다.
from authlib.oauth2.rfc6749.parameters import prepare_token_request  # type: ignore[import-untyped]  # Authlib의 요청 구성 함수를 사용한다.
import httpx
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, KeySetSerialization

from app.observability import FailureDetails
from app.security.google_identity import GoogleIdentityError
from app.security.google_transactions import GoogleTransaction
from app.security.settings import GoogleSettings

GOOGLE_DISCOVERY_TIMEOUT_SECONDS = 5
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_ISSUER = GoogleSettings.issuer


class GoogleProviderUnavailable(Exception):
    """Google 응답 원문과 자격 증명을 숨기고 안전한 실패 정보만 전달한다."""

    def __init__(self, failure: FailureDetails) -> None:
        super().__init__("Google 로그인 요청을 처리할 수 없습니다. 잠시 후 다시 시도해 주세요.")
        self.failure = failure


async def _google_metadata(settings: GoogleSettings, client: httpx.AsyncClient) -> dict[str, object]:
    """시작과 callback에서 같은 고정 주소와 issuer 검사 기준을 사용한다."""
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
    return metadata


async def google_authorization_url(
    transaction: GoogleTransaction, settings: GoogleSettings, client: httpx.AsyncClient
) -> str:
    """고정된 Discovery 주소를 조회하고 검증한 Google 주소로만 로그인 요청을 만든다."""
    metadata = await _google_metadata(settings, client)

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


@dataclass(frozen=True, slots=True)
class GoogleCallbackMetadata:
    """Discovery에서 신뢰할 수 있는 Google 주소인지 확인한 교환·공개키 주소."""

    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True, slots=True)
class GoogleTokenResponse:
    """신원 검증에 잠시 사용하는 제공자 토큰. 객체 표현과 로그에서는 숨긴다."""

    id_token: str = field(repr=False)
    access_token: str = field(repr=False)


async def google_callback_metadata(settings: GoogleSettings, client: httpx.AsyncClient) -> GoogleCallbackMetadata:
    """secret을 보내기 전에 교환·공개키 주소와 RS256 지원을 확인한다."""
    metadata = await _google_metadata(settings, client)
    algorithms = metadata.get("id_token_signing_alg_values_supported")
    if (
        metadata.get("token_endpoint") != settings.token_endpoint
        or metadata.get("jwks_uri") != settings.jwks_uri
        or not isinstance(algorithms, list)
        or "RS256" not in algorithms
    ):
        raise GoogleProviderUnavailable(FailureDetails(stage="google_discovery", error_type="InvalidGoogleMetadata"))
    return GoogleCallbackMetadata(token_endpoint=settings.token_endpoint, jwks_uri=settings.jwks_uri)


async def exchange_google_code(
    code: str,
    transaction: GoogleTransaction,
    metadata: GoogleCallbackMetadata,
    settings: GoogleSettings,
    client: httpx.AsyncClient,
) -> GoogleTokenResponse:
    """PKCE verifier를 포함해 code를 한 번만 교환하며 실패한 POST를 재시도하지 않는다."""
    body = prepare_token_request(
        "authorization_code",
        code=code,
        redirect_uri=settings.redirect_uri,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        code_verifier=transaction.code_verifier,
    )
    try:
        async with asyncio.timeout(GOOGLE_DISCOVERY_TIMEOUT_SECONDS):
            response = await client.post(
                metadata.token_endpoint,
                content=body,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                follow_redirects=False,
            )
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Google 토큰 응답 형식이 올바르지 않습니다.")
        # 만료·재사용·PKCE 불일치는 인증 실패다. client 설정 오류나 제공자 장애와 구분한다.
        if response.status_code in (200, 400, 401) and payload.get("error") in ("invalid_grant", "access_denied"):
            raise GoogleIdentityError()
        response.raise_for_status()
        if response.status_code != 200 or "error" in payload:
            raise ValueError("Google 토큰 교환을 완료할 수 없습니다.")
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        if isinstance(exc, GoogleIdentityError):
            raise
        raise GoogleProviderUnavailable(FailureDetails.from_exception("google_code_exchange", exc)) from None
    id_token, access_token = payload.get("id_token"), payload.get("access_token")
    if (
        not isinstance(id_token, str)
        or not 1 <= len(id_token) <= 16384
        or not isinstance(access_token, str)
        or not 1 <= len(access_token) <= 16384
        or any(not 33 <= ord(character) <= 126 for character in access_token)
        or not isinstance(payload.get("token_type"), str)
        or payload["token_type"].lower() != "bearer"
    ):
        raise GoogleIdentityError()
    return GoogleTokenResponse(id_token=id_token, access_token=access_token)


async def google_signing_keys(metadata: GoogleCallbackMetadata, client: httpx.AsyncClient) -> KeySet:
    """Google 공개키를 새로 조회한다. 토큰의 jku·x5u나 임의의 주소는 조회하지 않는다."""
    try:
        async with asyncio.timeout(GOOGLE_DISCOVERY_TIMEOUT_SECONDS):
            response = await client.get(metadata.jwks_uri, follow_redirects=False)
            response.raise_for_status()
            payload = response.json()
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("keys"), list)
            or not 1 <= len(payload["keys"]) <= 50
            or any(not isinstance(key, dict) for key in payload["keys"])
        ):
            raise ValueError("Google 공개키 응답 형식이 올바르지 않습니다.")
        keys = KeySet.import_key_set(cast(KeySetSerialization, payload))
        identifiers = [key.kid for key in keys.keys]
        if (
            any(key.is_private for key in keys.keys)
            or any(not isinstance(kid, str) or not kid for kid in identifiers)
            or len(set(identifiers)) != len(identifiers)
        ):
            raise ValueError("Google 공개키 정보를 확인할 수 없습니다.")
        return keys
    except (httpx.HTTPError, JoseError, ValueError, TypeError, KeyError, TimeoutError) as exc:
        raise GoogleProviderUnavailable(FailureDetails.from_exception("google_jwks", exc)) from None
