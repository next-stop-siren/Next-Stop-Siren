"""Approved Argon2id hashing for passwords already validated at the boundary."""

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

_hasher = PasswordHasher(
    type=Type.ID,
    memory_cost=64 * 1024,  # 설정 단위는 KiB이며, 메모리 64 MiB를 사용한다.
    time_cost=3,
    parallelism=1,
    salt_len=16,
    hash_len=32,
    encoding="utf-8",
)

# 실제 계정과 같은 설정으로 미리 만든 검증용 더미 해시.
# 계정이 없을 때도 이 해시를 검증해, 비밀번호가 틀린 경우와 처리 시간 차이를 줄인다.
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=1$1ejjmo0odQMz+W74JZBtwg$FuxHJjFfQ7yxfn+lA87e/S8vh7NKUMj43KIX5XQGKcw"
)


class PasswordHashError(Exception):
    """해시를 만들거나 검증하지 못했을 때 사용하는 오류. 비밀번호가 틀린 경우와 구분한다."""


def hash_password(password: str) -> str:
    """라이브러리가 매번 새로 만든 salt를 사용해 Argon2id 해시를 생성한다."""
    try:
        return _hasher.hash(password)
    except HashingError:
        raise PasswordHashError("비밀번호 해시를 생성할 수 없습니다.") from None


def verify_password(password: str, password_hash: str) -> bool:
    """입력한 비밀번호를 저장된 해시와 비교한다. 요청으로 받은 해시는 검증에 사용하지 않는다."""
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError):
        raise PasswordHashError("저장된 비밀번호 해시를 검증할 수 없습니다.") from None


def verify_dummy_password(password: str) -> None:
    """계정이 없어도 같은 비용으로 Argon2 검증을 수행한다. 결과가 일치해도 로그인은 허용하지 않는다."""
    verify_password(password, _DUMMY_PASSWORD_HASH)
