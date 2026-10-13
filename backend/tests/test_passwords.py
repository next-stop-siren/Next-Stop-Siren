"""Real Argon2 hashing and verification using only fake passwords."""

from unittest.mock import patch

from argon2 import PasswordHasher, extract_parameters
from argon2.exceptions import HashingError, VerificationError
from argon2.low_level import Type
import pytest

from app.security.passwords import PasswordHashError, hash_password, verify_password


@pytest.fixture(scope="module")
def encoded_password() -> str:
    return hash_password("fixture-only-password")


def test_generated_hash_has_approved_parameters(encoded_password: str) -> None:
    parameters = extract_parameters(encoded_password)
    assert parameters.type == Type.ID
    assert parameters.version == 19
    assert parameters.memory_cost == 65536
    assert parameters.time_cost == 3
    assert parameters.parallelism == 1
    assert parameters.salt_len == 16
    assert parameters.hash_len == 32
    assert "fixture-only-password" not in encoded_password
    assert PasswordHasher().verify(encoded_password, "fixture-only-password")


def test_same_password_gets_a_fresh_salt(encoded_password: str) -> None:
    other = hash_password("fixture-only-password")
    assert other != encoded_password
    assert other.split("$")[4] != encoded_password.split("$")[4]
    assert verify_password("fixture-only-password", other)


def test_correct_and_incorrect_passwords(encoded_password: str) -> None:
    assert verify_password("fixture-only-password", encoded_password) is True
    assert verify_password("fixture-only-password-wrong", encoded_password) is False


@pytest.mark.parametrize(
    "password,changed",
    [
        ("  fixture-password  ", "fixture-password"),
        ("e\u0301" * 6, "é" * 6),
        ("🔑" * 128, "🔑" * 127),
    ],
)
def test_unicode_and_whitespace_are_not_normalized(password: str, changed: str) -> None:
    encoded = hash_password(password)
    assert verify_password(password, encoded) is True
    assert verify_password(changed, encoded) is False


@pytest.mark.parametrize("encoded", ["", "fixture-only-hash", "$argon2id$v=19$m=65536,t=3,p=1$broken$broken"])
def test_corrupt_stored_hash_raises_a_safe_error(encoded: str) -> None:
    with pytest.raises(PasswordHashError) as caught:
        verify_password("fixture-only-password", encoded)
    assert str(caught.value) == "저장된 비밀번호 해시를 검증할 수 없습니다."
    assert "fixture" not in str(caught.value)


def test_hashing_failure_is_distinct_from_password_mismatch() -> None:
    with patch("app.security.passwords._hasher", spec=PasswordHasher) as hasher:
        hasher.hash.side_effect = HashingError("private internal details")
        with pytest.raises(PasswordHashError) as caught:
            hash_password("fixture-only-password")
    assert str(caught.value) == "비밀번호 해시를 생성할 수 없습니다."
    assert "private" not in str(caught.value)


def test_verification_failure_is_distinct_from_password_mismatch(encoded_password: str) -> None:
    with patch("app.security.passwords._hasher", spec=PasswordHasher) as hasher:
        hasher.verify.side_effect = VerificationError("private internal details")
        with pytest.raises(PasswordHashError) as caught:
            verify_password("fixture-only-password", encoded_password)
    assert "private" not in str(caught.value)


def test_unexpected_programming_errors_are_not_hidden() -> None:
    with patch("app.security.passwords._hasher", spec=PasswordHasher) as hasher:
        hasher.hash.side_effect = RuntimeError("programming failure")
        with pytest.raises(RuntimeError, match="programming failure"):
            hash_password("fixture-only-password")
