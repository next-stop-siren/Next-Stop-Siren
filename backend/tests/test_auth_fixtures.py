"""Fixture reuse and isolation only; HTTP authentication is tested separately."""

from typing import Any

from auth_helpers import load_auth_cases


def test_auth_user_provides_consistent_inputs_and_public_responses(auth_user: dict[str, Any]):
    public = auth_user["public_user"]
    assert set(public) == {"id", "email", "created_at"}
    assert public["id"] == str(auth_user["principal"]["user_id"])
    assert auth_user["register_input"]["email"] == public["email"]
    assert auth_user["login_input"]["email"] == public["email"]
    assert auth_user["expected_auth_response"]["user"] == public
    assert auth_user["expected_me_response"] == {"user": public}


def test_auth_fixture_mutations_do_not_change_source_or_other_fixtures(
    auth_cases: dict[str, Any], auth_user_a: dict[str, Any], auth_user_b: dict[str, Any]
):
    auth_user_a["public_user"]["email"] = "changed@example.test"
    assert auth_cases["users"]["a"]["public_user"]["email"] == "a@example.test"
    assert auth_user_b["public_user"]["email"] == "b@example.test"
    auth_cases["users"]["b"]["public_user"]["email"] = "changed@example.test"
    assert load_auth_cases()["users"]["b"]["public_user"]["email"] == "b@example.test"


def test_auth_helper_resolves_data_independently_of_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_auth_cases()["users"]["a"]["principal"]["user_id"] == 101


def test_error_and_mismatch_fixtures_are_available(
    auth_cases: dict[str, Any],
    unauthenticated_case: dict[str, Any],
    principal_body_mismatch_case: dict[str, Any],
):
    assert unauthenticated_case["principal"] is None
    assert unauthenticated_case["expected_status"] == 401
    assert unauthenticated_case["expected_response"]["error"]["code"] == "unauthenticated"
    case = principal_body_mismatch_case
    principal = auth_cases["users"][case["authenticated_user"]]["principal"]
    assert principal["user_id"] == case["expected_principal_user_id"]
    assert str(principal["user_id"]) != case["request_body"]["user_id"]
