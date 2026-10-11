"""Registration input and public authentication response contracts."""

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_serializer, field_validator


class RegisterRequest(BaseModel):
    """Accept only the two required strings; field policies follow separately."""

    model_config = ConfigDict(extra="forbid")

    email: StrictStr
    password: StrictStr = Field(repr=False)


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
