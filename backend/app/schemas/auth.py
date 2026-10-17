"""Local registration/login inputs and public authentication responses."""

from datetime import datetime, timezone
import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_serializer, field_validator


# 따옴표 없는 이메일 주소와 DNS 형식 도메인을 지원하며, 한글 등 Unicode 문자도 허용한다.
# 입력 표기를 유지하고, 로컬 이메일 중복은 DB의 lower(email)로 판정한다.
_EMAIL_LOCAL = re.compile(r"[\w!#$%&'*+/=?^`{|}~-]+(?:\.[\w!#$%&'*+/=?^`{|}~-]+)*")
_DOMAIN_LABEL = re.compile(r"[^\W_](?:[^\W_]|-){0,61}[^\W_]|[^\W_]")


class _LocalCredentialsRequest(BaseModel):
    """가입과 로그인에 같은 이메일·비밀번호 검증 규칙을 적용한다."""

    model_config = ConfigDict(extra="forbid")

    email: StrictStr
    password: StrictStr = Field(repr=False)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        """앞뒤 공백을 제거하고 이메일 형식을 검사한다. DNS 조회나 소유 확인은 하지 않는다."""
        email = value.strip()
        if len(email) > 320:
            raise ValueError("이메일은 최대 320자까지 입력할 수 있습니다.")
        local, separator, domain = email.partition("@")
        labels = domain.split(".")
        if (
            not separator
            or not _EMAIL_LOCAL.fullmatch(local)
            or len(local) > 64
            or len(domain) > 255
            or len(labels) < 2
            or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
        ):
            raise ValueError("올바른 이메일 형식을 입력해 주세요.")
        return email

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        """입력 원문을 유지하면서 Unicode 문자 수와 UTF-8 바이트 수를 검사한다."""
        if not 12 <= len(value) <= 128:
            raise ValueError("비밀번호는 12~128자로 입력해 주세요.")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("비밀번호는 UTF-8로 표현할 수 있어야 합니다.") from None
        if len(encoded) > 1024:
            raise ValueError("비밀번호는 UTF-8 기준 1024바이트 이하여야 합니다.")
        return value


class RegisterRequest(_LocalCredentialsRequest):
    """로컬 가입 입력. 비밀번호는 입력한 그대로 유지한다."""


class LoginRequest(_LocalCredentialsRequest):
    """가입과 같은 입력 규칙을 사용하는 로컬 로그인 요청."""


class PublicUser(BaseModel):
    """공개할 사용자 속성만 반환하고 DB의 인증 정보는 숨긴다."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[StrictStr, Field(pattern=r"^[1-9][0-9]*$")]
    email: StrictStr
    created_at: AwareDatetime

    @field_validator("id", mode="before")
    @classmethod
    def stringify_database_id(cls, value: object) -> object:
        """ORM 정수 ID를 JSON 문자열로 바꿔 bigint의 정밀도를 유지한다."""
        if type(value) is int:
            return str(value)
        return value

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        """저장된 시간대와 관계없이 UTC ISO 8601 문자열로 반환한다."""
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _AuthResponse(BaseModel):
    """가입·로그인의 공통 성공 본문. refresh 토큰은 쿠키로만 전달한다."""

    model_config = ConfigDict(extra="forbid")

    user: PublicUser
    access_token: StrictStr = Field(repr=False)
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: Literal[900] = 900
    csrf_token: StrictStr = Field(repr=False)


class RegisterResponse(_AuthResponse):
    """가입 성공 본문. API에서는 HTTP 201로 반환한다."""


class LoginResponse(_AuthResponse):
    """로그인 성공 본문. API에서는 HTTP 200으로 반환한다."""


class MeResponse(BaseModel):
    """현재 로그인한 사용자의 공개 정보만 반환한다."""

    model_config = ConfigDict(extra="forbid")

    user: PublicUser


class CsrfResponse(BaseModel):
    """현재 refresh에 연결된 메모리용 CSRF 값. 토큰과 사용자 정보는 포함하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    csrf_token: StrictStr = Field(repr=False)


class RefreshResponse(CsrfResponse):
    """회전 성공 본문. refresh 원문은 쿠키로만 전달한다."""

    access_token: StrictStr = Field(repr=False)
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: Literal[900] = 900
