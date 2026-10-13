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
    """Configuration failure that identifies a field without echoing its value."""


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
    """Require an exact HTTP origin without credentials, paths or wildcards."""
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
        # Browsers serialize hostnames in lowercase; require the same spelling.
        if parsed.netloc != parsed.netloc.lower():
            raise ValueError
    except ValueError:
        raise AuthSettingsError(f"{name}: 정확한 http(s) Origin을 지정해 주세요.") from None
    return parsed.scheme, host


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """Validated values for token issuance and cookie policy; no implicit keys."""

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
        """Load process settings, or an explicit mapping for isolated tests."""
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
    """Dependency for later routes; configuration is not loaded at import time."""
    return AuthSettings.from_env()
