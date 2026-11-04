"""Fail closed before serving production; no schema mutation or external calls."""

from collections.abc import Mapping
from ipaddress import ip_network
import os
import sys
from urllib.parse import unquote, urlsplit

from app.security.settings import AuthSettings, AuthSettingsError, GoogleSettings, RegistrationLimitSettings

ORIGIN = "https://next-stop-siren.duckdns.org"


def validate(values: Mapping[str, str]) -> None:
    auth = AuthSettings.from_env(values)
    if auth.environment != "production" or not auth.cookie_secure:
        raise ValueError("production_auth_required")
    if auth.api_origin != ORIGIN or auth.allowed_origins != (ORIGIN,):
        raise ValueError("same_origin_required")
    RegistrationLimitSettings.from_env(values)
    if values.get("CHAT_WORKER", "on") != "on":
        raise ValueError("worker_required")
    for entry in values.get("FORWARDED_ALLOW_IPS", "").split(","):
        try:
            if "%" in entry or ip_network(entry.strip()).prefixlen == 0:
                raise ValueError
        except ValueError:
            raise ValueError("explicit_proxy_addresses_required") from None
    override = values.get("UVICORN_FORWARDED_ALLOW_IPS")
    if override is not None and override != values["FORWARDED_ALLOW_IPS"]:
        raise ValueError("proxy_addresses_conflict")
    parsed = urlsplit(values.get("DATABASE_URL", ""))
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname != "db"
        or parsed.port not in {None, 5432}
        or not parsed.username
        or not parsed.password
        or not parsed.path.removeprefix("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("internal_database_required")
    expected = (values.get("POSTGRES_USER"), values.get("POSTGRES_PASSWORD"), values.get("POSTGRES_DB"))
    actual = (unquote(parsed.username), unquote(parsed.password), unquote(parsed.path.removeprefix("/")))
    if not all(expected) or actual != expected:
        raise ValueError("database_settings_mismatch")
    google_names = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_TRANSACTION_KEY")
    if any(values.get(name) for name in google_names):
        google = GoogleSettings.from_env(values, auth_settings=auth)
        if google.frontend_origin != ORIGIN:
            raise ValueError("google_same_origin_required")


def main() -> int:
    try:
        validate(os.environ)
    except (AuthSettingsError, ValueError):
        # Neither exception text nor environment values enter container logs.
        print("Production configuration rejected (values hidden).", file=sys.stderr)
        return 2
    if sys.argv[1:] == ["--validate-only"]:
        print("Production configuration valid; no external services checked.")
        return 0
    if len(sys.argv) == 1:
        return 2
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
