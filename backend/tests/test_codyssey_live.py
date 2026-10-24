"""The isolated real smoke test of S11: one synthetic question to Codyssey's gpt-5.5.

Runs only with `--run-live`, never in CI. It uses the developer's own key from the
server .env, the guarded test database and a fake user, and makes exactly one call.
Check the remaining quota on the Codyssey dashboard before and after.

The printed evidence holds statuses, the approved URL and model, and elapsed time;
never the key, the question or the answer.
"""

import json
import logging

import pytest
from sqlalchemy.orm import Session

from app.db import get_session
from app.integrations.codyssey import CodysseyGptProvider, GptSettings
from app.integrations.provider import get_provider
from app.main import app
from app.models.conversation import Conversation
from app.models.user import User
from test_conversations_api import call, sign_in

pytestmark = [
    pytest.mark.db,
    pytest.mark.live,
    # Since #53 a question is queued and the worker streams its answer, but the Codyssey
    # adapter has only `complete`. Rewrite this smoke when the real AI is connected.
    pytest.mark.skip(reason="#53 이후 질문은 대기열 실행기가 stream으로 답하며 Codyssey 어댑터에는 stream이 없습니다."),
]


@pytest.fixture
def session(orm_test_connection):
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as session:
        app.dependency_overrides[get_session] = lambda: session
        yield session
        app.dependency_overrides.clear()


def test_a_real_answer_is_stored_and_read_back(session, caplog):
    settings = GptSettings.from_env()
    app.dependency_overrides[get_provider] = lambda: CodysseyGptProvider(settings)
    user = User(email="s11-smoke@example.test", password_hash="fake-hash")
    session.add(user)
    session.flush()
    conversation = Conversation(user_id=user.id)
    session.add(conversation)
    session.commit()
    sign_in(user.id)

    path = f"/api/conversations/{conversation.id}/messages"
    with caplog.at_level(logging.INFO, logger="app"):
        sent = call("POST", path, json={"content": "한 문장으로 인사해 주세요.", "request_key": "s11-smoke-1"})
    history = call("GET", path)

    answer = sent.json().get("answer", {})
    logged = [record.getMessage() for record in caplog.records]
    ended = next(message for message in logged if "gpt_call_ended" in message)
    # The service's entry carries the request's trace_id, the outcome and the elapsed time.
    traced = next(message for message in logged if message.startswith("Provider call ended"))
    print(
        json.dumps(
            {
                "base_url": settings.base_url,
                "model": settings.model,
                "send_status": sent.status_code,
                "answer_status": answer.get("status"),
                "history_status": history.status_code,
                "history_matches": history.json().get("items") == [sent.json().get("question"), answer],
                "provider_log": ended,
                "service_log": traced,
            },
            ensure_ascii=False,
        )
    )
    assert sent.status_code == 201 and answer["status"] == "completed" and answer["content"].strip()
    assert history.json()["items"] == [sent.json()["question"], answer]
