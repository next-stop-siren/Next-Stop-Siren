"""Validated authentication configuration supplied by environment variables."""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
import os
import re
from typing import ClassVar, Literal
from urllib.parse import urlsplit


class AuthSettingsError(ValueError):
    """설정값을 노출하지 않고 문제가 있는 항목만 알려 주는 오류."""


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value or value != value.strip():
        raise AuthSettingsError(f"{name}: 필수 설정이 없거나 형식이 올바르지 않습니다.")
    return value


def _key(environ: Mapping[str, str], name: str) -> bytes:
    value = _required(environ, name)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise AuthSettingsError(f"{name}: 올바른 Base64 키가 필요합니다.") from None
    if len(decoded) < 32:
        raise AuthSettingsError(f"{name}: 최소 32바이트의 키가 필요합니다.")
    return decoded


def _kid(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise AuthSettingsError(f"{name}: 1~64자의 영문·숫자·밑줄·하이픈을 사용해 주세요.")
    return value


def _claim(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise AuthSettingsError(f"{name}: 공백·제어 문자를 사용할 수 없습니다.")
    return value


def _origin(value: str, name: str) -> tuple[str, str]:
    """사용자 정보, 경로, 와일드카드가 없는 정확한 HTTP Origin만 허용한다."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not value.startswith(parsed.scheme + "://")
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or any(character.isspace() or ord(character) < 32 for character in value)
            or any(character in value for character in "?#*")
            or parsed.netloc.endswith(":")
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
        try:
            ip_address(host)
        except ValueError:
            if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                raise ValueError
        # 브라우저는 호스트 이름을 소문자로 표현하므로 설정도 같은 표기를 요구한다.
        if parsed.netloc != parsed.netloc.lower():
            raise ValueError
    except ValueError:
        raise AuthSettingsError(f"{name}: 정확한 http(s) Origin을 지정해 주세요.") from None
    return parsed.scheme, host


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """토큰 발급과 쿠키 정책에 사용할 검증된 설정. 비밀키는 기본값 없이 명시적으로 받는다."""

    environment: Literal["development", "production"]
    access_key: bytes = field(repr=False)
    refresh_key: bytes = field(repr=False)
    refresh_digest_key: bytes = field(repr=False)
    csrf_key: bytes = field(repr=False)
    access_kid: str
    refresh_kid: str
    issuer: str
    audience: str
    api_origin: str
    allowed_origins: tuple[str, ...]
    cookie_secure: bool

    access_ttl_seconds: ClassVar[int] = 900
    refresh_ttl_seconds: ClassVar[int] = 14 * 24 * 60 * 60
    cookie_path: ClassVar[str] = "/api/auth"
    cookie_samesite: ClassVar[Literal["lax"]] = "lax"
    cookie_httponly: ClassVar[bool] = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AuthSettings":
        """환경 변수를 읽거나 테스트 격리를 위해 전달받은 설정을 사용한다."""
        values = os.environ if environ is None else environ
        environment = _required(values, "AUTH_ENV")
        if environment not in {"development", "production"}:
            raise AuthSettingsError("AUTH_ENV: development 또는 production을 지정해 주세요.")
        keys = {
            name: _key(values, name)
            for name in ("AUTH_ACCESS_KEY", "AUTH_REFRESH_KEY", "AUTH_REFRESH_DIGEST_KEY", "AUTH_CSRF_KEY")
        }
        if len(set(keys.values())) != len(keys):
            raise AuthSettingsError("인증 용도별 비밀키는 서로 달라야 합니다.")
        api_origin = _required(values, "AUTH_API_ORIGIN")
        api_scheme, api_host = _origin(api_origin, "AUTH_API_ORIGIN")
        origins = tuple(origin.strip() for origin in _required(values, "AUTH_ALLOWED_ORIGINS").split(","))
        parsed_origins = [_origin(origin, "AUTH_ALLOWED_ORIGINS") for origin in origins]
        if len(set(origins)) != len(origins):
            raise AuthSettingsError("AUTH_ALLOWED_ORIGINS: 중복 Origin을 제거해 주세요.")
        secure = _required(values, "AUTH_COOKIE_SECURE")
        if secure not in {"true", "false"}:
            raise AuthSettingsError("AUTH_COOKIE_SECURE: true 또는 false를 지정해 주세요.")
        if environment == "production" and (
            secure != "true" or api_scheme != "https" or any(scheme != "https" for scheme, _ in parsed_origins)
        ):
            raise AuthSettingsError("운영 인증은 HTTPS Origin과 Secure 쿠키가 필요합니다.")
        if secure == "false" and (
            environment != "development"
            or api_scheme != "http"
            or api_host != "127.0.0.1"
            or any(scheme != "http" or host != "127.0.0.1" for scheme, host in parsed_origins)
        ):
            raise AuthSettingsError("Secure=false는 개발용 HTTP 127.0.0.1 Origin에서만 허용됩니다.")
        return cls(
            environment="development" if environment == "development" else "production",
            access_key=keys["AUTH_ACCESS_KEY"],
            refresh_key=keys["AUTH_REFRESH_KEY"],
            refresh_digest_key=keys["AUTH_REFRESH_DIGEST_KEY"],
            csrf_key=keys["AUTH_CSRF_KEY"],
            access_kid=_kid(values, "AUTH_ACCESS_KID"),
            refresh_kid=_kid(values, "AUTH_REFRESH_KID"),
            issuer=_claim(values, "AUTH_JWT_ISSUER"),
            audience=_claim(values, "AUTH_JWT_AUDIENCE"),
            api_origin=api_origin,
            allowed_origins=origins,
            cookie_secure=secure == "true",
        )


def get_auth_settings() -> AuthSettings:
    """인증 경로에서 사용할 설정을 읽는다. 모듈을 불러올 때는 설정을 읽지 않는다."""
    return AuthSettings.from_env()


@dataclass(frozen=True, slots=True)
class GoogleSettings:
    """Google 로그인 전용 설정. 로컬 인증 설정을 읽을 때는 필요하지 않다.

    트랜잭션 키는 AES-256-GCM으로 로그인 쿠키를 암호화하고 무결성을 보호한다.
    설정을 읽는 과정에서는 Google에 접속하거나 자격 증명의 실제 유효성을 확인하지 않는다.
    """

    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    transaction_key: bytes = field(repr=False)

    callback_path: ClassVar[str] = "/api/auth/google/callback"
    discovery_url: ClassVar[str] = "https://accounts.google.com/.well-known/openid-configuration"
    scopes: ClassVar[tuple[str, ...]] = ("openid", "email")

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, *, auth_settings: AuthSettings | None = None
    ) -> "GoogleSettings":
        """기존 인증 설정의 API Origin과 비밀키를 기준으로 Google 설정을 검증한다."""
        values = os.environ if environ is None else environ
        auth = auth_settings if auth_settings is not None else AuthSettings.from_env(values)
        client_id = _required(values, "GOOGLE_CLIENT_ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id):
            raise AuthSettingsError("GOOGLE_CLIENT_ID: Google OAuth client ID 형식이 필요합니다.")
        client_secret = _claim(values, "GOOGLE_CLIENT_SECRET")
        redirect_uri = _required(values, "GOOGLE_REDIRECT_URI")
        # 정확히 일치해야 하므로 사용자 정보, 쿼리, 프래그먼트나 다른 호스트가 포함된 주소도 거부한다.
        if redirect_uri != auth.api_origin + cls.callback_path:
            raise AuthSettingsError("GOOGLE_REDIRECT_URI: API Origin과 Google callback 경로가 일치해야 합니다.")
        transaction_key = _key(values, "GOOGLE_TRANSACTION_KEY")
        if len(transaction_key) != 32:
            raise AuthSettingsError("GOOGLE_TRANSACTION_KEY: 정확히 32바이트의 키가 필요합니다.")
        if transaction_key in (auth.access_key, auth.refresh_key, auth.refresh_digest_key, auth.csrf_key):
            raise AuthSettingsError("GOOGLE_TRANSACTION_KEY: 기존 인증 키와 다른 키가 필요합니다.")
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            transaction_key=transaction_key,
        )


def get_google_settings() -> GoogleSettings:
    """Google 인증이 필요할 때 설정을 읽는다. 모듈을 불러올 때는 외부 입출력을 수행하지 않는다."""
    return GoogleSettings.from_env()
