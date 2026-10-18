"""Short-lived Google login state, PKCE and authenticated transaction encryption."""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.security.settings import GoogleSettings

GOOGLE_TRANSACTION_TTL_SECONDS = 300
# 현재 화면의 루트 경로만 허용한다. 새 화면이 생기면 해당 경로를 명시적으로 추가한다.
GOOGLE_RETURN_TO_PATHS = frozenset({"/"})
_VERSION = "v1"
_RANDOM_VALUE = re.compile(r"[A-Za-z0-9_-]{43}")
_COOKIE_VALUE = re.compile(r"v1\.([A-Za-z0-9_-]+)")
_MAX_COOKIE_LENGTH = 2048
_PAYLOAD_FIELDS = {"state", "nonce", "code_verifier", "return_to", "issued_at", "expires_at"}


class GoogleTransactionError(ValueError):
    """쿠키 내용과 로그인 비밀값을 노출하지 않는 공통 검증 오류."""

    def __init__(self) -> None:
        super().__init__("Google 로그인 요청을 확인할 수 없습니다. 다시 시작해 주세요.")


class InvalidGoogleReturnTo(ValueError):
    """시작 요청의 복귀 경로 오류. 후속 API에서 422로 처리할 수 있도록 구분한다."""

    def __init__(self) -> None:
        super().__init__("허용된 Google 로그인 복귀 경로가 필요합니다.")


@dataclass(frozen=True, slots=True)
class GoogleTransaction:
    """한 번의 로그인 시도에 필요한 값. 비밀값은 객체 표현과 로그에서 제외한다."""

    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    return_to: str
    issued_at: int
    expires_at: int

    @property
    def code_challenge(self) -> str:
        """RFC 7636의 S256 규칙에 따라 verifier를 해시하고 패딩 없는 Base64url로 변환한다."""
        return _encode(hashlib.sha256(self.code_verifier.encode("ascii")).digest())


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _current_time(now: datetime | None) -> float:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.utcoffset() is None:
        raise GoogleTransactionError()
    return current.timestamp()


def validate_google_return_to(value: str | None) -> str:
    """입력값을 디코딩하거나 정규화하지 않고 허용된 경로와 정확히 비교한다."""
    if value is None:
        return "/"
    if not isinstance(value, str) or value not in GOOGLE_RETURN_TO_PATHS:
        raise InvalidGoogleReturnTo()
    return value


def create_google_transaction(return_to: str | None = None, *, now: datetime | None = None) -> GoogleTransaction:
    """state, OIDC nonce, PKCE verifier를 각각 독립적인 256비트 난수로 만든다."""
    destination = validate_google_return_to(return_to)
    issued_at = int(_current_time(now))
    return GoogleTransaction(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=secrets.token_urlsafe(32),
        return_to=destination,
        issued_at=issued_at,
        expires_at=issued_at + GOOGLE_TRANSACTION_TTL_SECONDS,
    )


def _validate(transaction: GoogleTransaction, now: datetime | None) -> None:
    for value in (transaction.state, transaction.nonce, transaction.code_verifier):
        if not isinstance(value, str) or _RANDOM_VALUE.fullmatch(value) is None:
            raise GoogleTransactionError()
    if (
        not isinstance(transaction.return_to, str)
        or transaction.return_to not in GOOGLE_RETURN_TO_PATHS
        or type(transaction.issued_at) is not int
        or type(transaction.expires_at) is not int
        or transaction.issued_at < 0
        or transaction.expires_at - transaction.issued_at != GOOGLE_TRANSACTION_TTL_SECONDS
        or not transaction.issued_at <= _current_time(now) < transaction.expires_at
    ):
        raise GoogleTransactionError()


def _cipher(settings: GoogleSettings) -> AESGCM:
    # from_env를 거치지 않고 설정 객체를 직접 만든 경우에도 키 길이를 확인한다.
    if not isinstance(settings.transaction_key, bytes) or len(settings.transaction_key) != 32:
        raise GoogleTransactionError()
    return AESGCM(settings.transaction_key)


def _context(settings: GoogleSettings) -> bytes:
    """암호문을 용도, 버전, OAuth client와 callback에 묶어 다른 맥락에서 사용하지 못하게 한다."""
    return json.dumps(
        ["google-login-transaction", _VERSION, settings.client_id, settings.redirect_uri], separators=(",", ":")
    ).encode("utf-8")


def encode_google_transaction(
    transaction: GoogleTransaction, settings: GoogleSettings, *, now: datetime | None = None
) -> str:
    """state·nonce·verifier를 암호화하며 매번 새로운 96비트 AES-GCM nonce를 만든다."""
    _validate(transaction, now)
    payload = json.dumps(
        {
            "state": transaction.state,
            "nonce": transaction.nonce,
            "code_verifier": transaction.code_verifier,
            "return_to": transaction.return_to,
            "issued_at": transaction.issued_at,
            "expires_at": transaction.expires_at,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    encryption_nonce = secrets.token_bytes(12)
    ciphertext = _cipher(settings).encrypt(encryption_nonce, payload, _context(settings))
    return _VERSION + "." + _encode(encryption_nonce + ciphertext)


def verify_google_transaction(
    cookie_value: str | None, state: str | None, settings: GoogleSettings, *, now: datetime | None = None
) -> GoogleTransaction:
    """code를 교환하기 전에 쿠키를 복호화하고 callback의 state를 검증한다.

    이 함수는 로그인 시도의 유효성만 확인한다. Google 신원은 후속 단계에서 검증한다.
    쿠키 삭제만으로 일회 사용을 보장할 수 없으므로 callback에서 code의 일회 교환도 확인해야 한다.
    """
    if not isinstance(cookie_value, str) or len(cookie_value) > _MAX_COOKIE_LENGTH:
        raise GoogleTransactionError()
    match = _COOKIE_VALUE.fullmatch(cookie_value)
    if match is None or not isinstance(state, str) or _RANDOM_VALUE.fullmatch(state) is None:
        raise GoogleTransactionError()
    encoded = match.group(1)
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(raw) < 12 + 16 or _encode(raw) != encoded:
            raise GoogleTransactionError()
        plaintext = _cipher(settings).decrypt(raw[:12], raw[12:], _context(settings))
        payload = json.loads(plaintext)
        if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS:
            raise GoogleTransactionError()
        transaction = GoogleTransaction(**payload)
    except (ValueError, TypeError, InvalidTag, UnicodeError):
        raise GoogleTransactionError() from None
    _validate(transaction, now)
    if not hmac.compare_digest(transaction.state, state):
        raise GoogleTransactionError()
    return transaction
