"""Exercise safe request validation and expected API error handling."""

from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator

from app.api.errors import ApiError, api_error_handler, register_error_handlers, validation_error_handler
from app.main import app as production_app
from app.schemas.auth import RegisterRequest


class UnsafeValidatorInput(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def reject_content(cls, value: str) -> str:
        raise ValueError(f"private validator details: {value}")


@pytest.fixture
def error_client():
    app = FastAPI()
    register_error_handlers(app)

    @app.post("/test/register")
    def register(body: RegisterRequest) -> dict[str, str]:
        return {"email": body.email}

    @app.get("/test/expected/{code}")
    def expected(code: str) -> None:
        if code == "duplicate":
            raise ApiError("local_email_exists")
        if code == "origin":
            raise ApiError("csrf_failed")
        raise ApiError("temporarily_unavailable")

    @app.post("/test/unsafe-validator")
    def unsafe_validator(body: UnsafeValidatorInput) -> None:
        pass

    @app.get("/test/path/{item_id}")
    def path_input(item_id: int, limit: int) -> dict[str, int]:
        return {"id": item_id, "limit": limit}

    @app.get("/test/unexpected")
    def unexpected() -> None:
        raise RuntimeError("internal programming failure")

    @app.get("/test/bad-response", response_model=RegisterRequest)
    def bad_response() -> dict[str, str]:
        return {}

    with TestClient(app) as client:
        yield client


def assert_error(response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    assert "set-cookie" not in response.headers
    payload = response.json()
    assert set(payload) == {"error"}
    error = payload["error"]
    assert set(error) == {"code", "message", "fields", "trace_id"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["fields"], dict)
    assert all(isinstance(value, str) for value in error["fields"].values())
    UUID(error["trace_id"])
    return error


def test_handlers_are_installed_on_production_app() -> None:
    assert production_app.exception_handlers[ApiError] is api_error_handler
    assert production_app.exception_handlers[RequestValidationError] is validation_error_handler
    assert "/api/auth/register" in production_app.openapi()["paths"]


def test_valid_input_passes_without_returning_password(error_client, auth_user_a) -> None:
    response = error_client.post("/test/register", json=auth_user_a["register_input"])
    assert response.status_code == 200
    assert response.json() == {"email": auth_user_a["register_input"]["email"]}


def test_missing_and_wrong_type_fields_have_safe_messages(error_client) -> None:
    response = error_client.post("/test/register", json={"password": 123})
    error = assert_error(response, 422, "validation_error")
    assert error["fields"] == {"email": "필수 값입니다.", "password": "문자열로 입력해 주세요."}


def test_validation_does_not_echo_password_email_or_extra_names(error_client) -> None:
    secret = "secret"
    response = error_client.post(
        "/test/register", json={"email": "private-email", "password": secret, "secret-field-name": "private-value"}
    )
    error = assert_error(response, 422, "validation_error")
    assert set(error["fields"]) == {"email", "password", "body"}
    for private in (secret, "private-email", "secret-field-name", "private-value"):
        assert private not in response.text


def test_arbitrary_validator_message_is_not_exposed(error_client) -> None:
    response = error_client.post("/test/unsafe-validator", json={"content": "private-value"})
    error = assert_error(response, 422, "validation_error")
    assert error["fields"] == {"body": "입력값을 확인해 주세요."}
    assert "private" not in response.text


@pytest.mark.parametrize("body", ['{"password":"private-value",', "null", "[]"])
def test_malformed_json_and_wrong_body_shapes_are_safe(error_client, body: str) -> None:
    response = error_client.post("/test/register", content=body, headers={"Content-Type": "application/json"})
    error = assert_error(response, 422, "validation_error")
    assert set(error["fields"]) == {"body"}
    assert "private-value" not in response.text


def test_path_and_query_validation_use_same_envelope(error_client) -> None:
    response = error_client.get("/test/path/private-id?limit=private-limit")
    error = assert_error(response, 422, "validation_error")
    assert set(error["fields"]) == {"path", "limit"}
    assert "private" not in response.text


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("duplicate", 409, "local_email_exists"),
        ("origin", 403, "csrf_failed"),
        ("database", 503, "temporarily_unavailable"),
    ],
)
def test_expected_failures_have_stable_status_and_safe_body(error_client, path: str, status: int, code: str) -> None:
    error = assert_error(error_client.get(f"/test/expected/{path}"), status, code)
    assert error["fields"] == {}


def test_each_failure_has_a_fresh_server_trace_id(error_client) -> None:
    responses = [error_client.post("/test/register", json={}, headers={"X-Request-ID": "client-id"}) for _ in range(2)]
    ids = [assert_error(response, 422, "validation_error")["trace_id"] for response in responses]
    assert len(set(ids)) == 2
    assert "client-id" not in ids


def test_unexpected_failures_are_not_converted_to_validation_errors(error_client) -> None:
    with pytest.raises(RuntimeError, match="internal programming failure"):
        error_client.get("/test/unexpected")
    with pytest.raises(ResponseValidationError):
        error_client.get("/test/bad-response")
