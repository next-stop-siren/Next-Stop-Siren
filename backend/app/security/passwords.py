"""Approved Argon2id hashing for passwords already validated at the boundary."""

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

_hasher = PasswordHasher(
    type=Type.ID,
    memory_cost=64 * 1024,  # KiB: 64 MiB.
    time_cost=3,
    parallelism=1,
    salt_len=16,
    hash_len=32,
    encoding="utf-8",
)


class PasswordHashError(Exception):
    """Hashing or stored-hash failure, distinct from an incorrect password."""


def hash_password(password: str) -> str:
    """Return an encoded Argon2id hash with a fresh library-generated salt."""
    try:
        return _hasher.hash(password)
    except HashingError:
        raise PasswordHashError("비밀번호 해시를 생성할 수 없습니다.") from None


def verify_password(password: str, password_hash: str) -> bool:
    """Verify unchanged input against a trusted stored hash, never an HTTP hash."""
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError):
        raise PasswordHashError("저장된 비밀번호 해시를 검증할 수 없습니다.") from None
