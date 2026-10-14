"""Registration and login schema boundaries without a database or real credentials."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse, PublicUser, RegisterRequest, RegisterResponse


@pytest.fixture(params=[RegisterRequest, LoginRequest], ids=["register", "login"])
def request_model(request: pytest.FixtureRequest) -> type[RegisterRequest] | type[LoginRequest]:
    return request.param


@pytest.fixture(params=[RegisterResponse, LoginResponse], ids=["register", "login"])
def response_model(request: pytest.FixtureRequest) -> type[RegisterResponse] | type[LoginResponse]:
    return request.param


def test_credentials_request_preserves_fixture_input(
    request_model: type[RegisterRequest] | type[LoginRequest], auth_user: dict[str, Any]
) -> None:
    input_key = "login_input" if request_model is LoginRequest else "register_input"
    request = request_model.model_validate(auth_user[input_key])
    assert request.model_dump() == auth_user[input_key]
    assert request.password not in repr(request)


@pytest.mark.parametrize("field", ["email", "password"])
@pytest.mark.parametrize("value", [None, 123, True, [], {}])
def test_credentials_request_rejects_non_string_fields(
    request_model: type[RegisterRequest] | type[LoginRequest], field: str, value: object
) -> None:
    body = {"email": "a@example.test", "password": "fixture-only-password"}
    with pytest.raises(ValidationError) as caught:
        request_model.model_validate({**body, field: value})
    assert caught.value.errors()[0]["loc"] == (field,)
    assert caught.value.errors()[0]["type"] == "string_type"


@pytest.mark.parametrize("field", ["email", "password"])
def test_credentials_request_requires_both_fields(
    request_model: type[RegisterRequest] | type[LoginRequest], field: str
) -> None:
    body = {"email": "a@example.test", "password": "fixture-only-password"}
    del body[field]
    with pytest.raises(ValidationError) as caught:
        request_model.model_validate(body)
    assert caught.value.errors()[0]["loc"] == (field,)
    assert caught.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("field", ["user_id", "password_hash", "unexpected"])
def test_credentials_request_rejects_extra_fields(
    request_model: type[RegisterRequest] | type[LoginRequest], field: str, auth_user_a: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError) as caught:
        request_model.model_validate({**auth_user_a["register_input"], field: "forged"})
    assert caught.value.errors()[0]["loc"] == (field,)
    assert caught.value.errors()[0]["type"] == "extra_forbidden"


def test_credentials_request_preserves_unicode_and_whitespace(
    request_model: type[RegisterRequest] | type[LoginRequest],
) -> None:
    body = {"email": " a@example.test ", "password": "  e\u0301한글🔑 password  "}
    assert request_model.model_validate(body).model_dump() == {**body, "email": "a@example.test"}


@pytest.mark.parametrize(
    "email",
    ["\t A.Name+tag@Example.TEST \n", "사용자@예시.한국", "a@sub.example.test"],
)
def test_credentials_email_trims_without_changing_case_or_unicode(
    request_model: type[RegisterRequest] | type[LoginRequest], email: str
) -> None:
    request = request_model(email=email, password="fixture-only-password")
    assert request.email == email.strip()


@pytest.mark.parametrize(
    "email",
    [
        "",
        "   ",
        "missing-at.test",
        "@example.test",
        "a@",
        "a@@example.test",
        "a b@example.test",
        "a@example .test",
        "a@example",
        ".a@example.test",
        "a.@example.test",
        "a..b@example.test",
        "a@-example.test",
        "a@example-.test",
        "a@exam_ple.test",
        "a@example..test",
        "a@example.test.",
        "Name <a@example.test>",
        '"a b"@example.test',
        "a@example.test\x00",
        "a" * 65 + "@example.test",
        "a@" + "b" * 64 + ".test",
    ],
)
def test_credentials_email_rejects_invalid_mailboxes(
    request_model: type[RegisterRequest] | type[LoginRequest], email: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        request_model(email=email, password="fixture-only-password")
    assert caught.value.errors()[0]["loc"] == ("email",)


def test_credentials_email_length_is_checked_after_trim(
    request_model: type[RegisterRequest] | type[LoginRequest],
) -> None:
    email = "a" * 64 + "@" + ".".join(["b" * 63] * 4)
    assert len(email) == 320
    assert request_model(email=" " + email + " ", password="fixture-only-password").email == email
    with pytest.raises(ValidationError, match="320"):
        request_model(email=email + "b", password="fixture-only-password")


@pytest.mark.parametrize("password", ["a" * 12, "a" * 128, "🔑" * 12, "🔑" * 128, " " * 12])
def test_credentials_password_accepts_boundaries_without_modification(
    request_model: type[RegisterRequest] | type[LoginRequest], password: str
) -> None:
    assert request_model(email="a@example.test", password=password).password == password


@pytest.mark.parametrize("password", ["", "a" * 11, "a" * 129, "🔑" * 11, "🔑" * 129])
def test_credentials_password_rejects_code_point_length_violations(
    request_model: type[RegisterRequest] | type[LoginRequest], password: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        request_model(email="a@example.test", password=password)
    assert caught.value.errors()[0]["loc"] == ("password",)


def test_credentials_password_does_not_normalize_combining_characters(
    request_model: type[RegisterRequest] | type[LoginRequest],
) -> None:
    decomposed = "e\u0301" * 6
    assert request_model(email="a@example.test", password=decomposed).password == decomposed
    with pytest.raises(ValidationError):
        request_model(email="a@example.test", password="é" * 6)


def test_credentials_password_rejects_unencodable_unicode(
    request_model: type[RegisterRequest] | type[LoginRequest],
) -> None:
    with pytest.raises(ValidationError) as caught:
        request_model(email="a@example.test", password="a" * 11 + "\ud800")
    assert caught.value.errors()[0]["loc"] == ("password",)
    assert "UTF-8" in caught.value.errors()[0]["msg"]


def test_public_user_maps_orm_bigint_and_utc_without_password_hash() -> None:
    user = User(
        id=9007199254740993,
        email="a@example.test",
        password_hash="fixture-only-hash",
        created_at=datetime(2026, 1, 2, 12, 4, 5, tzinfo=timezone(timedelta(hours=9))),
    )
    public = PublicUser.model_validate(user)
    assert public.model_dump(mode="json") == {
        "id": "9007199254740993",
        "email": "a@example.test",
        "created_at": "2026-01-02T03:04:05Z",
    }
    assert user.password_hash not in public.model_dump_json()


@pytest.mark.parametrize("value", [True, 1.5, "1.5", "01", "-1", 0])
def test_public_user_rejects_invalid_decimal_ids(value: object, auth_user_a: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PublicUser.model_validate({**auth_user_a["public_user"], "id": value})


def test_public_user_rejects_timestamp_without_timezone(auth_user_a: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        PublicUser.model_validate({**auth_user_a["public_user"], "created_at": "2026-01-02T03:04:05"})
    assert caught.value.errors()[0]["type"] == "timezone_aware"


def test_auth_response_matches_existing_contract(
    response_model: type[RegisterResponse] | type[LoginResponse], auth_user: dict[str, Any]
) -> None:
    response = response_model.model_validate(auth_user["expected_auth_response"])
    assert response.model_dump(mode="json") == auth_user["expected_auth_response"]
    assert response.access_token not in repr(response)
    assert response.csrf_token not in repr(response)


@pytest.mark.parametrize("field,value", [("token_type", "Refresh"), ("expires_in", 3600)])
def test_auth_response_rejects_wrong_token_contract(
    response_model: type[RegisterResponse] | type[LoginResponse], field: str, value: object, auth_user_a: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        response_model.model_validate({**auth_user_a["expected_auth_response"], field: value})


def test_auth_response_cannot_include_refresh_token(
    response_model: type[RegisterResponse] | type[LoginResponse], auth_user_a: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError) as caught:
        response_model.model_validate({**auth_user_a["expected_auth_response"], "refresh_token": "fixture-refresh"})
    assert caught.value.errors()[0]["type"] == "extra_forbidden"


def test_auth_response_supplies_bearer_and_expiry_defaults(
    response_model: type[RegisterResponse] | type[LoginResponse], auth_user_a: dict[str, Any]
) -> None:
    body = auth_user_a["expected_auth_response"].copy()
    del body["token_type"]
    del body["expires_in"]
    assert response_model.model_validate(body).model_dump(mode="json") == auth_user_a["expected_auth_response"]


@pytest.mark.parametrize("field", ["user", "access_token", "csrf_token"])
def test_auth_response_requires_public_user_and_tokens(
    response_model: type[RegisterResponse] | type[LoginResponse], field: str, auth_user_a: dict[str, Any]
) -> None:
    body = auth_user_a["expected_auth_response"].copy()
    del body[field]
    with pytest.raises(ValidationError) as caught:
        response_model.model_validate(body)
    assert caught.value.errors()[0]["loc"] == (field,)
    assert caught.value.errors()[0]["type"] == "missing"
