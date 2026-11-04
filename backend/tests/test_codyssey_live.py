"""The isolated real smoke test of S11 and #68: one synthetic question to Codyssey's gpt-5.5.

Runs only with `--run-live`, never in CI, and only after the separate approval for a
real call. It uses the developer's own key from the server .env, the guarded test
database and a fake user. The question goes through the queue as in the service: it
is admitted with the member quota, the runner streams the answer from the real
adapter once, and the history is read back. Check the remaining quota on the Codyssey
dashboard before and after.

The printed evidence holds states, the approved URL and model, piece counts and
elapsed time; never the key, the question or the answer.
"""

import json
import logging

import pytest
from sqlalchemy.orm import Session

from app.db import get_session
from app.integrations.codyssey import CodysseyGptProvider, GptSettings
from app.main import app
from app.models.chat_job import ChatJob
from app.models.conversation import Conversation
from app.models.user import User
from app.services import chat_queue, chat_runner, quota
from test_conversations_api import call, sign_in

pytestmark = [pytest.mark.db, pytest.mark.live]


@pytest.fixture
def sessions(orm_test_connection):
    """New sessions on the rolled-back test connection; each commit ends only its savepoint."""

    def factory():
        return Session(orm_test_connection, join_transaction_mode="create_savepoint")

    with factory() as route_session:
        app.dependency_overrides[get_session] = lambda: route_session
        yield factory
        app.dependency_overrides.clear()


def test_a_real_streamed_answer_is_stored_and_read_back(sessions, caplog):
    settings = GptSettings.from_env()
    ledger = quota.MemberQuota()
    with sessions() as session:
        user = User(email="s11-smoke@example.test", password_hash="fake-hash")
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        session.flush()
        admission = chat_queue.admit_question(
            session, ledger, user.id, conversation.id, "한 문장으로 인사해 주세요.", "s11-smoke-1"
        )
        assert admission.job is not None
        user_id, conversation_id, job_id = user.id, conversation.id, admission.job.id
        session.commit()

    pieces = []
    with caplog.at_level(logging.INFO, logger="app"):
        ran = chat_runner.run_next(
            sessions, CodysseyGptProvider(settings), ledger, user_id, on_delta=lambda _job, text: pieces.append(text)
        )
    with sessions() as session:
        job = session.get(ChatJob, job_id)
        assert job is not None
        state = job.state
    sign_in(user_id)
    history = call("GET", f"/api/conversations/{conversation_id}/messages")
    items = history.json().get("items", [])

    logged = [record.getMessage() for record in caplog.records]
    ended = next(message for message in logged if "gpt_call_ended" in message)
    print(
        json.dumps(
            {
                "base_url": settings.base_url,
                "model": settings.model,
                "ran": ran,
                "job_state": state,
                "pieces": len(pieces),
                "history_status": history.status_code,
                "history_roles": [(item["role"], item["status"]) for item in items],
                "provider_log": ended,
            },
            ensure_ascii=False,
        )
    )
    assert ran and state == "completed"
    assert [(item["role"], item["status"]) for item in items] == [("user", "completed"), ("assistant", "completed")]
    assert items[1]["content"] == "".join(pieces) and items[1]["content"].strip()
