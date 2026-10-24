"""The member chat queue API with the real queue and quota on PostgreSQL (#53).

Requests go through the routes with authentication mocked as in
test_conversations_api.py. Jobs are started and ended by calling the queue directly,
as the worker would, so no thread and no provider runs. Route checks share one
rolled-back connection; races commit in a temporary schema that is dropped
afterwards. Every user, question and answer is fake.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db import create_db_engine, get_session
from app.main import app
from app.models.chat_job import ChatJob, ChatQueueMember, ConversationHold
from app.models.conversation import Conversation, Message
from app.models.quota import ChatQuota, QuotaReservation
from app.models.user import User
from app.repositories import chat_jobs as job_repository
from app.services import chat, chat_queue, chat_views, quota
from app.services.chat_queue import PROVIDER_FAILED, Outcome
from db_guard import guarded_live_url
from test_conversations_api import call, error_of, sign_in

pytestmark = pytest.mark.db

LEDGER = quota.MemberQuota()


@pytest.fixture
def session(orm_test_connection):
    """One session for seeding, for the routes and for the queue calls, inside the rolled-back test."""
    with Session(orm_test_connection, join_transaction_mode="create_savepoint") as session:
        app.dependency_overrides[get_session] = lambda: session
        yield session
        app.dependency_overrides.clear()


def save(session, row):
    session.add(row)
    session.flush()
    return row.id


@pytest.fixture
def seeded(session, auth_user_a, auth_user_b):
    """Two members; the first has conversations `a` and `b` and is signed in."""
    user_a = save(session, User(email=auth_user_a["register_input"]["email"], password_hash="fake-hash"))
    user_b = save(session, User(email=auth_user_b["register_input"]["email"], password_hash="fake-hash"))
    a = save(session, Conversation(user_id=user_a))
    b = save(session, Conversation(user_id=user_a))
    theirs = save(session, Conversation(user_id=user_b))
    session.commit()
    sign_in(user_a)
    return SimpleNamespace(user_a=user_a, user_b=user_b, a=a, b=b, theirs=theirs)


def interval_passed(session, user_id):
    """Move the last admission 11 seconds back, as if the 10-second interval had passed."""
    session.execute(
        update(ChatQuota)
        .where(ChatQuota.user_id == user_id)
        .values(last_reserved_at=ChatQuota.last_reserved_at - timedelta(seconds=11))
    )
    session.commit()


def ask(session, seeded, conversation_id, key, content=None):
    response = call(
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        json={"content": content or f"질문 {key}", "request_key": key},
    )
    interval_passed(session, seeded.user_a)
    return response


def retry(session, seeded, conversation_id, question_id, key):
    response = call(
        "POST", f"/api/conversations/{conversation_id}/messages/{question_id}/retry", json={"retry_key": key}
    )
    interval_passed(session, seeded.user_a)
    return response


def job_of(job_id):
    response = call("GET", f"/api/chat/jobs/{job_id}")
    assert response.status_code == 200
    return response.json()["job"]


def run(session, user_id, outcome):
    """What the worker does for one job: start it, then store the outcome under its run token."""
    claimed, _ = chat_queue.claim_next(session, LEDGER, user_id)
    assert claimed is not None
    session.commit()
    chat_queue.finish(session, LEDGER, claimed.job_id, claimed.run_token, outcome)
    session.commit()
    return claimed


def reservation(session, job_id):
    session.expire_all()
    return session.get(QuotaReservation, session.get(ChatJob, job_id).reservation_id)


def test_admission_reserves_both_periods_and_the_same_key_never_reserves_again_even_at_zero(session, seeded):
    first = call("POST", f"/api/conversations/{seeded.a}/messages", json={"content": "질문", "request_key": "k1"})
    assert first.status_code == 202
    body = first.json()
    job_id = int(body["job"]["id"])
    assert body["answer"] is None
    assert (body["job"]["state"], body["job"]["position"], body["job"]["allowed_actions"]) == ("queued", 1, ["cancel"])
    assert body["limits"]["daily"]["reserved"] == body["limits"]["six_hour"]["reserved"] == 1
    assert body["limits"]["daily"]["remaining"] == 399 and body["limits"]["next_attempt_at"] is not None
    assert reservation(session, job_id).state == "reserved"

    # A new question within 10 seconds is refused and stores nothing.
    soon = call("POST", f"/api/conversations/{seeded.a}/messages", json={"content": "또", "request_key": "k2"})
    assert soon.status_code == 429 and error_of(soon)["code"] == "attempt_too_soon"
    assert 1 <= int(soon.headers["Retry-After"]) <= 10
    # The same key within 10 seconds is the stored job, with nothing reserved.
    again = call("POST", f"/api/conversations/{seeded.a}/messages", json={"content": "질문", "request_key": "k1"})
    assert again.status_code == 202 and again.json()["job"]["id"] == str(job_id)
    count = select(func.count()).select_from(QuotaReservation)
    assert session.scalar(count) == 1
    assert session.scalar(select(func.count()).select_from(Message)) == 1

    # Fill the six-hour period, then the limit refuses a new key but not the stored one.
    interval_passed(session, seeded.user_a)
    periods = quota.periods_at(job_repository.database_time(session))
    for _ in range(quota.SIX_HOUR_LIMIT - 1):
        session.add(
            QuotaReservation(
                user_id=seeded.user_a,
                day_start=periods.day_start,
                six_hour_start=periods.six_hour_start,
                state="settled",
                created_at=periods.six_hour_start,
                resolved_at=periods.six_hour_start,
            )
        )
    session.commit()
    full = call("POST", f"/api/conversations/{seeded.a}/messages", json={"content": "또", "request_key": "k3"})
    assert full.status_code == 429 and error_of(full)["code"] == "quota_exceeded"
    assert int(full.headers["Retry-After"]) >= 1
    assert call("GET", "/api/chat/limits").json()["six_hour"]["remaining"] == 0

    run(session, seeded.user_a, Outcome("completed", text="저장된 답변"))
    done = call("POST", f"/api/conversations/{seeded.a}/messages", json={"content": "질문", "request_key": "k1"})
    assert done.status_code == 200
    assert (done.json()["job"]["state"], done.json()["answer"]["content"]) == ("completed", "저장된 답변")
    assert session.scalar(count) == quota.SIX_HOUR_LIMIT
    assert reservation(session, job_id).state == "settled"


def test_the_context_is_read_when_the_job_starts_from_completed_pairs_only(session, seeded):
    ask(session, seeded, seeded.a, "a1")
    ask(session, seeded, seeded.a, "a2")
    ask(session, seeded, seeded.b, "b1")
    # a2 was admitted before a1 had an answer; a1 completes before a2 starts.
    run(session, seeded.user_a, Outcome("completed", text="답 a1"))
    a2 = run(session, seeded.user_a, PROVIDER_FAILED)
    assert [(item.role, item.content) for item in a2.context] == [
        ("user", "질문 a1"),
        ("assistant", "답 a1"),
        ("user", "질문 a2"),
    ]
    b1 = run(session, seeded.user_a, Outcome("completed", text="답 b1"))
    assert [(item.role, item.content) for item in b1.context] == [("user", "질문 b1")]

    # After a2 failed and was skipped, the failed pair stays out of a3's context.
    a2_question = session.get(ChatJob, a2.job_id).question_message_id
    assert call("POST", f"/api/conversations/{seeded.a}/messages/{a2_question}/skip", json={}).status_code == 200
    ask(session, seeded, seeded.a, "a3")
    a3 = run(session, seeded.user_a, Outcome("completed", text="답 a3"))
    assert [(item.role, item.content) for item in a3.context] == [
        ("user", "질문 a1"),
        ("assistant", "답 a1"),
        ("user", "질문 a3"),
    ]


def test_a_failure_holds_only_its_conversation_until_a_retry_succeeds(session, seeded):
    failed_id = int(ask(session, seeded, seeded.a, "a1").json()["job"]["id"])
    run(session, seeded.user_a, PROVIDER_FAILED)
    failed = job_of(failed_id)
    question_id = failed["question_message_id"]
    assert (failed["state"], failed["error_code"], failed["allowed_actions"]) == (
        "failed",
        "provider_failed",
        ["retry", "skip"],
    )
    assert reservation(session, failed_id).refund_reason == "failed"

    paused = ask(session, seeded, seeded.a, "a2").json()["job"]
    assert (paused["state"], paused["paused_by_message_id"], paused["position"]) == ("paused", question_id, None)
    other = ask(session, seeded, seeded.b, "b1").json()["job"]
    assert (other["state"], other["position"]) == ("queued", 1)
    assert run(session, seeded.user_a, Outcome("completed", text="답 b1")).job_id == int(other["id"])

    retried = retry(session, seeded, seeded.a, question_id, "r1")
    assert retried.status_code == 202
    retry_job = retried.json()["job"]
    assert (retry_job["kind"], retry_job["state"], retry_job["position"]) == ("retry", "queued", 1)
    assert job_of(failed_id)["allowed_actions"] == []
    blocked = call("POST", f"/api/conversations/{seeded.a}/messages/{question_id}/skip", json={})
    assert blocked.status_code == 409 and error_of(blocked)["code"] == "skip_not_allowed"

    canceled = call("POST", f"/api/chat/jobs/{retry_job['id']}/cancel", json={})
    assert canceled.status_code == 200 and canceled.json()["job"]["state"] == "canceled"
    assert call("POST", f"/api/chat/jobs/{retry_job['id']}/cancel", json={}).status_code == 200
    assert reservation(session, int(retry_job["id"])).refund_reason == "canceled"
    # A canceled retry does not release the conversation.
    assert job_of(failed_id)["allowed_actions"] == ["retry", "skip"]
    assert job_of(int(paused["id"]))["state"] == "paused"

    second = retry(session, seeded, seeded.a, question_id, "r2").json()["job"]
    claimed = run(session, seeded.user_a, Outcome("completed", text="다시 만든 답"))
    assert claimed.job_id == int(second["id"])
    assert job_of(int(second["id"]))["state"] == "completed"
    assert job_of(failed_id)["allowed_actions"] == []
    resumed = job_of(int(paused["id"]))
    assert (resumed["state"], resumed["paused_by_message_id"], resumed["position"]) == ("queued", None, 1)
    retried_again = retry(session, seeded, seeded.a, question_id, "r3")
    assert retried_again.status_code == 409 and error_of(retried_again)["code"] == "retry_not_allowed"


def test_skip_resumes_and_a_retry_is_allowed_only_until_a_later_question_starts(session, seeded):
    failed_id = int(ask(session, seeded, seeded.a, "a1").json()["job"]["id"])
    run(session, seeded.user_a, Outcome("interrupted", error_code="outcome_unknown"))
    question_id = job_of(failed_id)["question_message_id"]
    later = ask(session, seeded, seeded.a, "a2").json()["job"]

    skipped = call("POST", f"/api/conversations/{seeded.a}/messages/{question_id}/skip", json={})
    assert skipped.status_code == 200
    assert [(job["id"], job["state"]) for job in skipped.json()["resumed_jobs"]] == [(later["id"], "queued")]
    assert job_of(failed_id)["allowed_actions"] == ["retry"]
    twice = call("POST", f"/api/conversations/{seeded.a}/messages/{question_id}/skip", json={})
    assert twice.status_code == 409 and error_of(twice)["code"] == "skip_not_allowed"

    # A later question that only waits does not prevent the retry, which then runs first.
    waiting_retry = retry(session, seeded, seeded.a, question_id, "r1")
    assert waiting_retry.status_code == 202
    assert job_of(int(later["id"]))["position"] == 2
    call("POST", f"/api/chat/jobs/{waiting_retry.json()['job']['id']}/cancel", json={})

    # Once the later question starts, the failed one can no longer be retried.
    chat_queue.claim_next(session, LEDGER, seeded.user_a)
    session.commit()
    assert job_of(failed_id)["allowed_actions"] == []
    refused = retry(session, seeded, seeded.a, question_id, "r2")
    assert refused.status_code == 409 and error_of(refused)["code"] == "retry_not_latest"


def test_outcomes_settle_or_refund_once_and_a_late_result_changes_nothing(session, seeded):
    job_id = int(ask(session, seeded, seeded.a, "a1").json()["job"]["id"])
    claimed, _ = chat_queue.claim_next(session, LEDGER, seeded.user_a)
    session.commit()
    assert call("GET", "/api/chat/limits").json()["daily"]["reserved"] == 1
    assert chat_queue.finish(session, LEDGER, job_id, claimed.run_token, Outcome("completed", text="답"))
    session.commit()
    # The same result again, or a failure arriving late, is ignored.
    assert chat_queue.finish(session, LEDGER, job_id, claimed.run_token, Outcome("completed", text="답")) is None
    assert chat_queue.finish(session, LEDGER, job_id, claimed.run_token, PROVIDER_FAILED) is None
    session.commit()
    stored = reservation(session, job_id)
    assert (stored.state, stored.refund_reason) == ("settled", None)
    limits = call("GET", "/api/chat/limits").json()
    assert (limits["daily"]["used"], limits["daily"]["reserved"], limits["six_hour"]["used"]) == (1, 0, 1)
    answer = session.get(Message, claimed.answer_message_id)
    assert (answer.status, answer.content) == ("completed", "답")

    # Running jobs cannot be canceled.
    ask(session, seeded, seeded.a, "a2")
    started, _ = chat_queue.claim_next(session, LEDGER, seeded.user_a)
    session.commit()
    refused = call("POST", f"/api/chat/jobs/{started.job_id}/cancel", json={})
    assert refused.status_code == 409 and error_of(refused)["code"] == "job_not_cancelable"


def test_another_members_conversations_jobs_and_events_are_not_found(session, seeded):
    job_id = ask(session, seeded, seeded.a, "a1").json()["job"]["id"]
    run(session, seeded.user_a, PROVIDER_FAILED)
    question_id = job_of(int(job_id))["question_message_id"]
    sign_in(seeded.user_b)
    responses = [
        call("GET", f"/api/chat/jobs/{job_id}"),
        call("POST", f"/api/chat/jobs/{job_id}/cancel", json={}),
        call("GET", f"/api/chat/jobs?conversation_id={seeded.a}"),
        call("POST", f"/api/conversations/{seeded.a}/messages", json={"content": "남의 대화", "request_key": "x"}),
        call("POST", f"/api/conversations/{seeded.a}/messages/{question_id}/retry", json={"retry_key": "x"}),
        call("POST", f"/api/conversations/{seeded.a}/messages/{question_id}/skip", json={}),
    ]
    for response in responses:
        assert response.status_code == 404 and error_of(response)["code"] == "not_found"
    assert call("GET", "/api/chat/jobs").json()["items"] == []
    assert chat_views.snapshot(session, seeded.user_b, None).items == []
    # A message ID that is not a question of the member's own conversation is the same 404.
    sign_in(seeded.user_a)
    answer_id = job_of(int(job_id))["answer_message_id"]
    for path in ("retry", "skip"):
        body = {"retry_key": "r"} if path == "retry" else {}
        missing = call("POST", f"/api/conversations/{seeded.a}/messages/{answer_id}/{path}", json=body)
        assert missing.status_code == 404


def test_listing_and_the_since_snapshot_return_stored_results_after_a_reconnect(session, seeded):
    first = int(ask(session, seeded, seeded.a, "a1").json()["job"]["id"])
    listed = call("GET", "/api/chat/jobs").json()
    assert [item["id"] for item in listed["items"]] == [str(first)]
    since = job_repository.database_time(session)
    run(session, seeded.user_a, PROVIDER_FAILED)
    question_id = session.get(ChatJob, first).question_message_id
    retried = int(retry(session, seeded, seeded.a, question_id, "r1").json()["job"]["id"])
    run(session, seeded.user_a, Outcome("completed", text="다시"))

    # Without `since`, only jobs that have not ended.
    assert chat_views.snapshot(session, seeded.user_a, None).items == []
    found = {item.view.job.id: item for item in chat_views.snapshot(session, seeded.user_a, since).items}
    assert found[retried].view.job.state == "completed" and found[retried].answer.content == "다시"
    # The first attempt ended earlier and lost its buttons; it comes with them gone.
    assert found[first].view.allowed_actions == ()

    page = call("GET", f"/api/chat/jobs?conversation_id={seeded.a}&limit=1").json()
    assert [item["id"] for item in page["items"]] == [str(retried)] and page["next_cursor"] == str(retried)
    rest = call("GET", f"/api/chat/jobs?conversation_id={seeded.a}&limit=1&before_id={page['next_cursor']}").json()
    assert [item["id"] for item in rest["items"]] == [str(first)] and rest["next_cursor"] is None
    assert page["server_time"].endswith("Z")


def test_a_reconnect_gets_the_earlier_failure_without_buttons_after_a_retry_completed_elsewhere(session, seeded):
    """The failure ended before the disconnect; only the retry ran while this tab was away."""
    first = int(ask(session, seeded, seeded.a, "a1").json()["job"]["id"])
    run(session, seeded.user_a, PROVIDER_FAILED)
    assert job_of(first)["allowed_actions"] == ["retry", "skip"]
    # The failure ended two minutes ago, beyond the snapshot's 30-second margin.
    earlier = timedelta(minutes=2)
    session.execute(
        update(ChatJob)
        .where(ChatJob.id == first)
        .values(
            accepted_at=ChatJob.accepted_at - earlier,
            expires_at=ChatJob.expires_at - earlier,
            started_at=ChatJob.started_at - earlier,
            deadline_at=ChatJob.deadline_at - earlier,
            finished_at=ChatJob.finished_at - earlier,
        )
    )
    session.commit()
    # This tab disconnects here, with the buttons shown.
    since = job_repository.database_time(session)
    question_id = session.get(ChatJob, first).question_message_id
    retried = int(retry(session, seeded, seeded.a, question_id, "r1").json()["job"]["id"])
    run(session, seeded.user_a, Outcome("completed", text="다른 탭의 답"))

    found = {item.view.job.id: item for item in chat_views.snapshot(session, seeded.user_a, since).items}
    assert found[retried].view.job.state == "completed" and found[retried].answer.content == "다른 탭의 답"
    # The earlier failure did not change state, but its buttons are gone, so it comes again.
    assert first in found and found[first].view.allowed_actions == ()


@pytest.mark.parametrize(
    ("ended", "status_code"),
    [("completed", 200), ("failed", 200)],
    ids=["earlier retry succeeded", "earlier retry failed"],
)
def test_a_retry_key_the_synchronous_api_used_returns_that_attempt_and_admits_nothing(
    session, seeded, ended, status_code
):
    """Before #53 a retry was only an answer row with its key; resending that key must not retry again."""
    question = save(
        session,
        Message(
            conversation_id=seeded.a, role="user", status="completed", content="옛 질문", request_key="old",
            completed_at=func.now(),
        ),
    )  # fmt: skip
    save(
        session,
        Message(conversation_id=seeded.a, role="assistant", status="failed", reply_to_message_id=question,
                attempt_no=1, error_code="provider_failed"),
    )  # fmt: skip
    done = (
        {"content": "옛 재시도 답", "completed_at": func.now()}
        if ended == "completed"
        else {"error_code": "provider_failed"}
    )
    attempt = save(
        session,
        Message(conversation_id=seeded.a, role="assistant", status=ended, reply_to_message_id=question,
                attempt_no=2, retry_key="old-r1", **done),
    )  # fmt: skip
    session.commit()

    again = call("POST", f"/api/conversations/{seeded.a}/messages/{question}/retry", json={"retry_key": "old-r1"})
    assert again.status_code == status_code
    body = again.json()
    assert body["job"] is None and (body["answer"]["id"], body["answer"]["status"]) == (str(attempt), ended)
    # Nothing was admitted or reserved, so no start can reuse the key.
    assert session.scalar(select(func.count()).select_from(ChatJob)) == 0
    assert session.scalar(select(func.count()).select_from(QuotaReservation)) == 0
    assert chat_queue.claim_next(session, LEDGER, seeded.user_a)[0] is None
    session.commit()
    # A new key after a failed attempt is a new retry, as before.
    if ended == "failed":
        fresh = call("POST", f"/api/conversations/{seeded.a}/messages/{question}/retry", json={"retry_key": "new"})
        assert fresh.status_code == 202 and fresh.json()["job"]["kind"] == "retry"


def test_a_pending_answer_left_without_a_job_does_not_block_the_next_start(session, seeded):
    """The synchronous API before #53 could leave one; the next start interrupts it."""
    old_question = save(
        session,
        Message(
            conversation_id=seeded.a, role="user", status="completed", content="옛 질문", request_key="old",
            completed_at=func.now(),
        ),
    )  # fmt: skip
    old_answer = save(
        session,
        Message(conversation_id=seeded.a, role="assistant", status="pending", reply_to_message_id=old_question,
                attempt_no=1),
    )  # fmt: skip
    session.commit()
    ask(session, seeded, seeded.a, "a1")
    run(session, seeded.user_a, Outcome("completed", text="새 답"))
    session.expire_all()
    left = session.get(Message, old_answer)
    assert (left.status, left.error_code) == ("interrupted", "outcome_unknown")
    # Its same-key resend returns the stored pair without a job.
    legacy = call("POST", f"/api/conversations/{seeded.a}/messages", json={"content": "옛 질문", "request_key": "old"})
    assert legacy.status_code == 200 and legacy.json()["job"] is None


@pytest.fixture
def committed_sessions():
    engine = create_db_engine(guarded_live_url())
    schema = f"chat_api_test_{uuid4().hex}"
    scoped = engine.execution_options(schema_translate_map={None: schema})
    models = (User, Conversation, Message, ChatQuota, QuotaReservation, ChatQueueMember, ChatJob, ConversationHold)
    created = False
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        User.metadata.create_all(scoped, tables=[model.__table__ for model in models])
        yield sessionmaker(bind=scoped, autoflush=False, expire_on_commit=False)
    finally:
        try:
            if created:
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            engine.dispose()


def race(first, second):
    """Start both at once, each on its own session, and return what each returned or raised."""
    start = Barrier(2)

    def call_one(work):
        start.wait(timeout=10)
        try:
            return work()
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        return [future.result(timeout=30) for future in [pool.submit(call_one, first), pool.submit(call_one, second)]]


def skipped_failure_with_a_later_question(sessions):
    """A failed and skipped question with a later question queued behind it, all committed."""
    with sessions() as setup:
        user_id = save(setup, User(email=f"race-{uuid4().hex}@example.test", password_hash="fake-hash"))
        conversation_id = save(setup, Conversation(user_id=user_id))
        failed = chat_queue.admit_question(setup, LEDGER, user_id, conversation_id, "질문 1", "k1").job
        setup.commit()
        claimed, _ = chat_queue.claim_next(setup, LEDGER, user_id)
        chat_queue.finish(setup, LEDGER, claimed.job_id, claimed.run_token, PROVIDER_FAILED)
        setup.execute(
            update(ChatQuota)
            .where(ChatQuota.user_id == user_id)
            .values(last_reserved_at=func.now() - timedelta(hours=1))
        )
        later = chat_queue.admit_question(setup, LEDGER, user_id, conversation_id, "질문 2", "k2").job
        setup.execute(
            update(ChatQuota)
            .where(ChatQuota.user_id == user_id)
            .values(last_reserved_at=func.now() - timedelta(hours=1))
        )
        setup.commit()
        return user_id, conversation_id, failed, later


@pytest.mark.parametrize("repeat", range(3))
def test_a_retry_racing_the_later_questions_start_never_runs_after_it(committed_sessions, repeat):
    sessions = committed_sessions
    user_id, conversation_id, failed, later = skipped_failure_with_a_later_question(sessions)
    with sessions() as session:
        chat_queue.skip(session, user_id, conversation_id, failed.question_message_id)
        session.commit()

    def admit_retry():
        with sessions() as session:
            admission = chat_queue.admit_retry(
                session, LEDGER, user_id, conversation_id, failed.question_message_id, "r1"
            )
            session.commit()
            return admission.job.id

    def start():
        with sessions() as session:
            claimed, _ = chat_queue.claim_next(session, LEDGER, user_id)
            session.commit()
            return claimed

    retried, started = race(admit_retry, start)
    with sessions() as reader:
        later_state = reader.get(ChatJob, later.id).state
    if isinstance(retried, int):
        # The retry was admitted first, so the start took it, not the later question.
        assert started is not None and started.job_id == retried and later_state == "queued"
    else:
        assert isinstance(retried, chat.NotLatestQuestion)
        assert started is not None and started.job_id == later.id and later_state == "running"


@pytest.mark.parametrize("repeat", range(3))
def test_a_retry_racing_a_skip_leaves_a_consistent_conversation(committed_sessions, repeat):
    sessions = committed_sessions
    user_id, conversation_id, failed, later = skipped_failure_with_a_later_question(sessions)

    def admit_retry():
        with sessions() as session:
            admission = chat_queue.admit_retry(
                session, LEDGER, user_id, conversation_id, failed.question_message_id, "r1"
            )
            session.commit()
            return admission.job

    def skip():
        with sessions() as session:
            chat_queue.skip(session, user_id, conversation_id, failed.question_message_id)
            session.commit()
            return "skipped"

    retried, skipped = race(admit_retry, skip)
    assert isinstance(retried, ChatJob)
    with sessions() as reader:
        held = reader.get(ConversationHold, conversation_id)
        later_state = reader.get(ChatJob, later.id).state
        skipped_at = reader.get(ChatJob, failed.id).skipped_at
    if skipped == "skipped":
        # The skip went first: the retry came after it, and the later question waits behind the retry.
        assert held is None and later_state == "queued" and skipped_at <= retried.accepted_at
    else:
        # The retry went first, so the skip was refused and the conversation stays held.
        assert isinstance(skipped, chat_queue.SkipNotAllowed)
        assert held is not None and later_state == "paused" and skipped_at is None
