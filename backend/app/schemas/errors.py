"""Public error bodies shared by API endpoints."""

from pydantic import BaseModel, ConfigDict, Field, StrictStr


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: StrictStr
    message: StrictStr
    fields: dict[str, str] = Field(default_factory=dict)
    trace_id: StrictStr


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
