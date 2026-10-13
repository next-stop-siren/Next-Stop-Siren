"""Concurrent signup requests with real commits in an isolated PostgreSQL schema."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from app.main import create_app
from app.models.refresh_session import RefreshSession
from app.models.user import User
from app.repositories.users import find_local_user_by_email
from app.security.session_values import create_refresh_digest

pytestmark = pytest.mark.db


def test_concurrent_case_variant_signups_commit_one_user_and_one_session(committed_signup_sessions, auth_settings):
    application = create_app(auth_settings)
    email = f"race-{uuid4().hex}@example.test"
    precheck = Barrier(2)

    def synchronized_precheck(session, candidate):
        found = find_local_user_by_email(session, candidate)
        # 두 요청 모두 중복 조회를 마친 뒤 삽입을 시작한다.
        precheck.wait(timeout=10)
        return found

    def signup(candidate):
        with TestClient(application, base_url="http://127.0.0.1:8000") as client:
            return client.post(
                "/api/auth/register",
                json={"email": candidate, "password": "fixture-only-password"},
                headers={"Origin": auth_settings.allowed_origins[0]},
            )

    # 실제 요청용 세션 생성·종료 경로도 함께 검증한다.
    with (
        patch("app.db._session_factory", committed_signup_sessions),
        patch("app.services.registration.find_local_user_by_email", synchronized_precheck),
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        futures = [workers.submit(signup, candidate) for candidate in (email, email.upper())]
        responses = [future.result(timeout=20) for future in futures]

    assert sorted(response.status_code for response in responses) == [201, 409]
    success = next(response for response in responses if response.status_code == 201)
    duplicate = next(response for response in responses if response.status_code == 409)
    assert duplicate.json()["error"]["code"] == "local_email_exists"
    assert "set-cookie" not in duplicate.headers and "access_token" not in duplicate.text
    with committed_signup_sessions() as reader:
        # 별도 연결에서도 두 행이 보이면 커밋이 완료된 것이다.
        assert reader.scalar(select(func.count()).select_from(User)) == 1
        assert reader.scalar(select(func.count()).select_from(RefreshSession)) == 1
        user = reader.scalar(select(User))
        refresh = reader.scalar(select(RefreshSession))
        assert success.json()["user"]["id"] == str(user.id) == str(refresh.user_id)
        assert refresh.token_digest == create_refresh_digest(success.cookies["refresh_token"], auth_settings)
