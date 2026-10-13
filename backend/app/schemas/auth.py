"""Registration input and public authentication response contracts."""

from datetime import datetime, timezone
import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_serializer, field_validator


# Support unquoted dot-atom mailboxes and DNS-style domains, including Unicode
# letters. Keep the original spelling; DB lower(email) controls local duplicates.
_EMAIL_LOCAL = re.compile(r"[\w!#$%&'*+/=?^`{|}~-]+(?:\.[\w!#$%&'*+/=?^`{|}~-]+)*")
_DOMAIN_LABEL = re.compile(r"[^\W_](?:[^\W_]|-){0,61}[^\W_]|[^\W_]")


class RegisterRequest(BaseModel):
    """Validate local registration inputs without normalizing passwords."""

    model_config = ConfigDict(extra="forbid")

    email: StrictStr
    password: StrictStr = Field(repr=False)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        """Trim and check mailbox syntax without DNS or ownership checks."""
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
        """Count code points and UTF-8 bytes while preserving the input."""
        if not 12 <= len(value) <= 128:
            raise ValueError("비밀번호는 12~128자로 입력해 주세요.")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("비밀번호는 UTF-8로 표현할 수 있어야 합니다.") from None
        if len(encoded) > 1024:
            raise ValueError("비밀번호는 UTF-8 기준 1024바이트 이하여야 합니다.")
        return value


class PublicUser(BaseModel):
    """Expose an allowlist of user attributes without database-only secrets."""

    model_config = ConfigDict(from_attributes=True)

    id: Annotated[StrictStr, Field(pattern=r"^[1-9][0-9]*$")]
    email: StrictStr
    created_at: AwareDatetime

    @field_validator("id", mode="before")
    @classmethod
    def stringify_database_id(cls, value: object) -> object:
        """Preserve bigint precision when mapping an ORM integer to JSON."""
        if type(value) is int:
            return str(value)
        return value

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        """Emit a UTC ISO 8601 timestamp regardless of the source offset."""
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RegisterResponse(BaseModel):
    """Registration success body; refresh tokens travel only in cookies."""

    model_config = ConfigDict(extra="forbid")

    user: PublicUser
    access_token: StrictStr = Field(repr=False)
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: Literal[900] = 900
    csrf_token: StrictStr = Field(repr=False)
