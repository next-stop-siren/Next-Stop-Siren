"""Persistent member chat queue, execution right and recovery on PostgreSQL (#52).

Single-transaction checks roll back with `orm_test_session`. Races and the runner
need commits that other connections can see, so those run in a temporary schema
that is dropped afterwards. The quota ledger is a fake that records its calls; every
user, question and answer is fake and no real provider is called.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db import create_db_engine
from app.integrations.provider import ContextMessage
from app.models.chat_job import ChatJob, ChatQueueMember, ConversationHold
from app.models.conversation import Conversation, Message
from app.models.user import User
from app.services import chat_queue, chat_runner
from app.services.chat import NotLatestQuestion, RetryNotAllowed
from app.services.chat_queue import (
    OUTCOME_UNKNOWN,
    PROVIDER_FAILED,
    RECOVERY_GRACE_SECONDS,
    Claimed,
    JobNotCancelable,
    JobNotFound,
    Outcome,
    QueueFull,
    RetryQueueFull,
    SkipNotAllowed,
)
from db_guard import guarded_live_url

pytestmark = pytest.mark.db

WORKERS = 6
T0 = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
# A completed answer must have content; the database refuses one without.
ANSWERED = Outcome("completed", text="모의 답변")


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class Refused(Exception):
    """Stands for the quota's own refusal."""


class FakeLedger:
    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.calls: list[tuple] = []
        self._next = 0
        self._lock = threading.Lock()

    def reserve(self, session, user_id, *, now):
        if self.refuse:
            raise Refused
        with self._lock:
            self._next += 1
            self.calls.append(("reserve", self._next))
            return self._next

    def settle(self, session, reservation_id):
        with self._lock:
            self.calls.append(("settle", reservation_id))
        return True

    def refund(self, session, reservation_id, reason):
        with self._lock:
            self.calls.append(("refund", reservation_id, reason))
        return True

    def resolutions(self, reservation_id):
        return [call for call in self.calls if call[0] != "reserve" and call[1] == reservation_id]


def add_user(session, email: str = "queue@example.test") -> int:
    user = User(email=email, password_hash="fake-hash")
    session.add(user)
    session.flush()
    return user.id


def add_conversation(session, user_id: int) -> int:
    conversation = Conversation(user_id=user_id)
    session.add(conversation)
    session.flush()
    return conversation.id


def ask(session, ledger, user_id, conversation_id, key, now):
    return chat_queue.admit_question(session, ledger, user_id, conversation_id, f"질문 {key}", key, now=now).job


def run(session, ledger, user_id, outcome, now) -> Claimed:
    claimed, _ = chat_queue.claim_next(session, ledger, user_id, now=now)
    assert claimed is not None
    chat_queue.finish(session, ledger, claimed.job_id, claimed.run_token, outcome, now=now)
    return claimed


def state(session, job_id) -> str:
    session.expire_all()
    return session.get(ChatJob, job_id).state


def test_the_database_rejects_jobs_of_the_wrong_shape(orm_test_session):
    session = orm_test_session
    user_id = add_user(session)
    conversation_id = add_conversation(session, user_id)
    question = Message(
        conversation_id=conversation_id,
        role="user",
        status="completed",
        content="질문",
        request_key="k",
        completed_at=T0,
    )
    session.add_all([ChatQueueMember(user_id=user_id), question])
    session.flush()
    base = dict(user_id=user_id, conversation_id=conversation_id, question_message_id=question.id)
    wrong = [
        dict(kind="question", state="queued", accepted_at=T0, expires_at=at(60)),
        dict(kind="retry", state="queued", accepted_at=T0, expires_at=at(300)),
        dict(kind="retry", retry_key="r", state="paused", paused_by_message_id=question.id, accepted_at=T0,
             expires_at=at(300)),
        dict(kind="question", state="running", accepted_at=T0, expires_at=at(300), started_at=T0),
        dict(kind="question", state="canceled", accepted_at=T0, expires_at=at(300)),
        dict(kind="question", state="queued", accepted_at=T0, expires_at=at(300), skipped_at=T0),
    ]  # fmt: skip
    for values in wrong:
        nested = session.begin_nested()
        session.add(ChatJob(**base, **values))
        with pytest.raises(IntegrityError):
            session.flush()
        nested.rollback()


def test_three_new_questions_and_one_retry_wait_besides_the_running_job(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    first, second, third = (add_conversation(session, user_id) for _ in range(3))
    failed_a = ask(session, ledger, user_id, first, "a", at(0))
    run(session, ledger, user_id, PROVIDER_FAILED, at(1))
    failed_b = ask(session, ledger, user_id, second, "b", at(20))
    run(session, ledger, user_id, PROVIDER_FAILED, at(21))

    for index in range(3):
        ask(session, ledger, user_id, third, f"wait-{index}", at(40 + index))
    with pytest.raises(QueueFull):
        ask(session, ledger, user_id, third, "wait-3", at(50))
    # The running job is not a waiting one.
    claimed, _ = chat_queue.claim_next(session, ledger, user_id, now=at(51))
    assert claimed is not None
    ask(session, ledger, user_id, third, "wait-3", at(52))

    retry = chat_queue.admit_retry(session, ledger, user_id, first, failed_a.question_message_id, "r1", now=at(53))
    assert retry.job.kind == "retry" and retry.job.state == "queued"
    with pytest.raises(RetryQueueFull):
        chat_queue.admit_retry(session, ledger, user_id, second, failed_b.question_message_id, "r2", now=at(54))
    # The same key returns the stored job without a new reservation.
    reserved = len(ledger.calls)
    again = chat_queue.admit_retry(session, ledger, user_id, first, failed_a.question_message_id, "r1", now=at(55))
    assert (again.created, again.job.id, len(ledger.calls)) == (False, retry.job.id, reserved)
    assert chat_queue.queue_counts(session, user_id) == chat_queue.QueueCounts(1, 3, 1)


def test_a_refused_reservation_stores_neither_question_nor_job(orm_test_session):
    session = orm_test_session
    user_id = add_user(session)
    conversation_id = add_conversation(session, user_id)
    with pytest.raises(Refused):
        ask(session, FakeLedger(refuse=True), user_id, conversation_id, "k", at(0))
    assert session.scalar(select(func.count()).select_from(Message)) == 0
    assert session.scalar(select(func.count()).select_from(ChatJob)) == 0


def test_another_members_conversation_and_job_are_not_found(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    owner = add_user(session, "owner@example.test")
    other = add_user(session, "other@example.test")
    conversation_id = add_conversation(session, owner)
    job = ask(session, ledger, owner, conversation_id, "k", at(0))
    with pytest.raises(chat_queue.ConversationNotFound):
        ask(session, ledger, other, conversation_id, "x", at(1))
    with pytest.raises(JobNotFound):
        chat_queue.cancel(session, ledger, other, job.id, now=at(2))
    assert chat_queue.claim_next(session, ledger, other, now=at(3))[0] is None


def test_jobs_run_first_in_first_out_and_a_failure_pauses_only_its_conversation(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    a, b = add_conversation(session, user_id), add_conversation(session, user_id)
    failed = ask(session, ledger, user_id, a, "a1", at(0))
    run(session, ledger, user_id, PROVIDER_FAILED, at(1))
    paused = ask(session, ledger, user_id, a, "a2", at(20))
    other = ask(session, ledger, user_id, b, "b1", at(30))
    assert (paused.state, paused.paused_by_message_id) == ("paused", failed.question_message_id)
    assert other.state == "queued"

    retry = chat_queue.admit_retry(session, ledger, user_id, a, failed.question_message_id, "r", now=at(40)).job
    # The older job of the other conversation is not overtaken.
    assert run(session, ledger, user_id, ANSWERED, at(41)).job_id == other.id
    retried = run(session, ledger, user_id, Outcome("completed", text="재시도 답변"), at(42))
    assert retried.job_id == retry.id
    assert state(session, paused.id) == "queued"
    resumed = run(session, ledger, user_id, ANSWERED, at(43))
    assert resumed.job_id == paused.id
    # The context is read at execution: the retried pair before the paused question.
    assert resumed.context == [
        ContextMessage("user", "질문 a1"),
        ContextMessage("assistant", "재시도 답변"),
        ContextMessage("user", "질문 a2"),
    ]
    assert session.get(ConversationHold, a) is None
    answers = session.scalars(
        select(Message.attempt_no).where(Message.reply_to_message_id == failed.question_message_id)
    ).all()
    assert sorted(answers) == [1, 2]


def test_skip_resumes_and_a_retry_still_goes_first_and_pauses_again_on_failure(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    a = add_conversation(session, user_id)
    failed = ask(session, ledger, user_id, a, "a1", at(0))
    run(session, ledger, user_id, PROVIDER_FAILED, at(1))
    follower = ask(session, ledger, user_id, a, "a2", at(20))

    retry = chat_queue.admit_retry(session, ledger, user_id, a, failed.question_message_id, "r1", now=at(30)).job
    with pytest.raises(SkipNotAllowed):
        chat_queue.skip(session, user_id, a, failed.question_message_id, now=at(31))
    chat_queue.cancel(session, ledger, user_id, retry.id, now=at(32))
    # A canceled retry keeps the conversation held.
    assert state(session, follower.id) == "paused"

    changes = chat_queue.skip(session, user_id, a, failed.question_message_id, now=at(33))
    assert set(changes.job_ids) == {failed.id, follower.id}
    assert state(session, follower.id) == "queued"
    assert session.get(ChatJob, failed.id).skipped_at == at(33)

    second = chat_queue.admit_retry(session, ledger, user_id, a, failed.question_message_id, "r2", now=at(40)).job
    assert run(session, ledger, user_id, OUTCOME_UNKNOWN, at(41)).job_id == second.id
    assert state(session, follower.id) == "paused"
    assert session.get(ConversationHold, a).job_id == second.id


def test_retry_is_refused_after_a_later_question_started(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    a = add_conversation(session, user_id)
    failed = ask(session, ledger, user_id, a, "a1", at(0))
    run(session, ledger, user_id, PROVIDER_FAILED, at(1))
    ask(session, ledger, user_id, a, "a2", at(20))
    chat_queue.skip(session, user_id, a, failed.question_message_id, now=at(21))
    run(session, ledger, user_id, ANSWERED, at(22))
    with pytest.raises(NotLatestQuestion):
        chat_queue.admit_retry(session, ledger, user_id, a, failed.question_message_id, "r", now=at(30))
    completed = ask(session, ledger, user_id, a, "a3", at(40))
    run(session, ledger, user_id, ANSWERED, at(41))
    with pytest.raises(RetryNotAllowed):
        chat_queue.admit_retry(session, ledger, user_id, a, completed.question_message_id, "r", now=at(50))


def test_waits_expire_after_five_minutes_including_paused_ones_and_refund_once(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    a, b = add_conversation(session, user_id), add_conversation(session, user_id)
    ask(session, ledger, user_id, a, "a1", at(0))
    run(session, ledger, user_id, PROVIDER_FAILED, at(1))
    paused = ask(session, ledger, user_id, a, "a2", at(10))
    queued = ask(session, ledger, user_id, b, "b1", at(13))

    chat_queue.recover(session, ledger, user_id, now=at(10 + 299))
    assert (state(session, paused.id), state(session, queued.id)) == ("paused", "queued")
    changes = chat_queue.recover(session, ledger, user_id, now=at(13 + 300))
    assert {paused.id, queued.id} <= set(changes.job_ids)
    assert (state(session, paused.id), state(session, queued.id)) == ("expired", "expired")
    chat_queue.recover(session, ledger, user_id, now=at(400))
    assert ledger.resolutions(paused.reservation_id) == [("refund", paused.reservation_id, "expired")]
    with pytest.raises(JobNotCancelable):
        chat_queue.cancel(session, ledger, user_id, queued.id, now=at(401))
    # An expired wait never starts.
    assert chat_queue.claim_next(session, ledger, user_id, now=at(402))[0] is None


def test_only_waiting_jobs_are_canceled_and_a_second_cancel_returns_the_same(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    a = add_conversation(session, user_id)
    running = ask(session, ledger, user_id, a, "a1", at(0))
    waiting = ask(session, ledger, user_id, a, "a2", at(10))
    chat_queue.claim_next(session, ledger, user_id, now=at(11))
    with pytest.raises(JobNotCancelable):
        chat_queue.cancel(session, ledger, user_id, running.id, now=at(12))
    first, _ = chat_queue.cancel(session, ledger, user_id, waiting.id, now=at(13))
    second, _ = chat_queue.cancel(session, ledger, user_id, waiting.id, now=at(14))
    assert first.state == second.state == "canceled"
    assert ledger.resolutions(waiting.reservation_id) == [("refund", waiting.reservation_id, "canceled")]


def test_recovery_keeps_a_live_execution_and_reconciles_one_past_its_deadline(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    a = add_conversation(session, user_id)
    job = ask(session, ledger, user_id, a, "a1", at(0))
    follower = ask(session, ledger, user_id, a, "a2", at(10))
    claimed, _ = chat_queue.claim_next(session, ledger, user_id, now=at(11))

    chat_queue.recover(session, ledger, user_id, now=at(11 + 30 + RECOVERY_GRACE_SECONDS - 1))
    assert state(session, job.id) == "running"
    chat_queue.recover(session, ledger, user_id, now=at(11 + 30 + RECOVERY_GRACE_SECONDS))
    assert state(session, job.id) == "interrupted"
    answer = session.get(Message, claimed.answer_message_id)
    assert (answer.status, answer.error_code) == ("interrupted", "outcome_unknown")
    assert ledger.resolutions(job.reservation_id) == [("refund", job.reservation_id, "interrupted")]
    assert state(session, follower.id) == "paused"

    # A result of the interrupted execution arrives late and changes nothing.
    assert chat_queue.finish(session, ledger, job.id, claimed.run_token, ANSWERED, now=at(60)) is None
    session.expire_all()
    assert session.get(Message, claimed.answer_message_id).status == "interrupted"
    assert len(ledger.resolutions(job.reservation_id)) == 1


def test_recovery_keeps_a_stored_completed_answer(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    a = add_conversation(session, user_id)
    job = ask(session, ledger, user_id, a, "a1", at(0))
    claimed, _ = chat_queue.claim_next(session, ledger, user_id, now=at(1))
    answer = session.get(Message, claimed.answer_message_id)
    answer.status, answer.content, answer.completed_at = "completed", "저장된 답변", at(2)
    session.flush()
    chat_queue.recover(session, ledger, user_id, now=at(120))
    assert state(session, job.id) == "completed"
    assert ledger.resolutions(job.reservation_id) == [("settle", job.reservation_id)]


def test_a_late_result_cannot_overwrite_a_new_attempt_or_its_completed_answer(orm_test_session):
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    a = add_conversation(session, user_id)
    job = ask(session, ledger, user_id, a, "a1", at(0))
    old, _ = chat_queue.claim_next(session, ledger, user_id, now=at(1))
    chat_queue.recover(session, ledger, user_id, now=at(100))
    retry = chat_queue.admit_retry(session, ledger, user_id, a, job.question_message_id, "r", now=at(110)).job
    new = run(session, ledger, user_id, Outcome("completed", text="새 답변"), at(111))
    assert new.job_id == retry.id

    assert chat_queue.finish(session, ledger, job.id, old.run_token, Outcome("completed", text="늦은 답변")) is None
    assert chat_queue.finish(session, ledger, retry.id, new.run_token, PROVIDER_FAILED) is None
    session.expire_all()
    assert session.get(Message, new.answer_message_id).content == "새 답변"
    assert (state(session, job.id), state(session, retry.id)) == ("interrupted", "completed")


@pytest.fixture
def committed_queue_sessions():
    """Sessions that commit in a temporary schema of the guarded test DB."""
    engine = create_db_engine(guarded_live_url())
    schema = f"queue_test_{uuid4().hex}"
    scoped = engine.execution_options(schema_translate_map={None: schema})
    tables = [model.__table__ for model in (User, Conversation, Message, ChatQueueMember, ChatJob, ConversationHold)]
    created = False
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        created = True
        User.metadata.create_all(scoped, tables=tables)
        yield sessionmaker(bind=scoped, autoflush=False, expire_on_commit=False)
    finally:
        try:
            if created:
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
        finally:
            engine.dispose()


def run_together(count: int, work):
    """Start `count` calls at once, each on its own session, and return what each returned or raised."""
    start = Barrier(count)

    def call(index: int):
        start.wait(timeout=10)
        try:
            return work(index)
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=count) as workers:
        return [future.result(timeout=30) for future in [workers.submit(call, index) for index in range(count)]]


def setup_member(sessions, questions: int = 0):
    """A member with one conversation and `questions` queued jobs admitted by the database clock."""
    ledger = FakeLedger()
    with sessions() as setup:
        user_id = add_user(setup)
        conversation_id = add_conversation(setup, user_id)
        jobs = [ask(setup, ledger, user_id, conversation_id, f"k{index}", None).id for index in range(questions)]
        setup.commit()
    return ledger, user_id, conversation_id, jobs


def test_concurrent_claims_start_one_job(committed_queue_sessions):
    sessions = committed_queue_sessions
    ledger, user_id, _, _ = setup_member(sessions, questions=3)

    def claim(_index: int):
        with sessions() as session:
            claimed, _ = chat_queue.claim_next(session, ledger, user_id)
            session.commit()
            return claimed

    results = run_together(WORKERS, claim)

    assert sum(isinstance(result, Claimed) for result in results) == 1
    assert sum(result is None for result in results) == WORKERS - 1
    with sessions() as reader:
        assert reader.scalar(select(func.count()).where(ChatJob.state == "running").select_from(ChatJob)) == 1
        assert reader.scalar(select(func.count()).where(Message.role == "assistant").select_from(Message)) == 1


def test_concurrent_admissions_never_exceed_three_waiting_questions(committed_queue_sessions):
    sessions = committed_queue_sessions
    ledger, user_id, conversation_id, _ = setup_member(sessions)

    def admit(index: int):
        with sessions() as session:
            job = ask(session, ledger, user_id, conversation_id, f"race-{index}", None)
            session.commit()
            return job.id

    results = run_together(WORKERS, admit)

    assert sum(isinstance(result, int) for result in results) == 3
    assert sum(isinstance(result, QueueFull) for result in results) == WORKERS - 3
    assert [call[0] for call in ledger.calls] == ["reserve"] * 3


def test_cancel_and_claim_racing_leave_one_result(committed_queue_sessions):
    sessions = committed_queue_sessions
    ledger, user_id, _, (job_id,) = setup_member(sessions, questions=1)

    def act(index: int):
        with sessions() as session:
            if index % 2:
                result = chat_queue.claim_next(session, ledger, user_id)[0]
            else:
                result = chat_queue.cancel(session, ledger, user_id, job_id)[0].state
            session.commit()
            return result

    results = run_together(WORKERS, act)

    with sessions() as reader:
        final = reader.get(ChatJob, job_id)
        reservation_id = final.reservation_id
        assert final.state in ("running", "canceled")
    started = [result for result in results if isinstance(result, Claimed)]
    if final.state == "running":
        assert len(started) == 1 and ledger.resolutions(reservation_id) == []
        assert all(isinstance(result, (JobNotCancelable, type(None))) for result in results if result not in started)
    else:
        assert started == [] and ledger.resolutions(reservation_id) == [("refund", reservation_id, "canceled")]


class SlowProvider:
    """Sends one piece, then waits past the time limit; checks that no queue lock is held meanwhile."""

    def __init__(self, sessions, user_id: int, job_id: int) -> None:
        self.sessions = sessions
        self.user_id = user_id
        self.job_id = job_id
        self.locks_free: bool | None = None

    def complete(self, messages, *, timeout_seconds):
        raise AssertionError("the runner streams")

    def stream(self, messages, *, timeout_seconds):
        with self.sessions() as probe:
            probe.execute(text("SET LOCAL lock_timeout = '1s'"))
            probe.execute(
                select(ChatQueueMember.user_id)
                .where(ChatQueueMember.user_id == self.user_id)
                .with_for_update(nowait=True)
            )
            probe.execute(select(ChatJob.id).where(ChatJob.id == self.job_id).with_for_update(nowait=True))
            probe.rollback()
        # NOWAIT raised otherwise: neither the member row nor the job row is locked during the call.
        self.locks_free = True
        yield "조각"
        time.sleep(3.0)
        yield "늦은 조각"


def test_the_runner_holds_no_lock_during_the_call_and_interrupts_at_the_time_limit(committed_queue_sessions):
    sessions = committed_queue_sessions
    ledger, user_id, _, (job_id,) = setup_member(sessions, questions=1)
    provider = SlowProvider(sessions, user_id, job_id)
    deltas: list[tuple[int, str]] = []

    assert chat_runner.run_next(
        sessions, provider, ledger, user_id, time_limit=2.0, on_delta=lambda *a: deltas.append(a)
    )

    assert provider.locks_free is True
    assert deltas == [(job_id, "조각")]
    with sessions() as reader:
        job = reader.get(ChatJob, job_id)
        answer = reader.get(Message, job.answer_message_id)
        assert job.state == "interrupted" and job.deadline_at == job.started_at + timedelta(seconds=30)
        assert (answer.status, answer.content, answer.error_code) == ("interrupted", None, "outcome_unknown")
    assert ledger.resolutions(job.reservation_id) == [("refund", job.reservation_id, "interrupted")]


def test_after_a_restart_waiting_jobs_run_and_a_cut_off_execution_is_interrupted(committed_queue_sessions):
    sessions = committed_queue_sessions
    ledger, user_id, conversation_id, (cut_off, waiting) = setup_member(sessions, questions=2)
    with sessions() as session:
        # The process that started this job stopped before the provider answered.
        claimed, _ = chat_queue.claim_next(session, ledger, user_id)
        assert claimed is not None and claimed.job_id == cut_off
        job = session.get(ChatJob, cut_off)
        job.started_at -= timedelta(minutes=1)
        job.deadline_at -= timedelta(minutes=1)
        session.commit()

    from app.integrations.provider import MockProvider

    # A new worker after the restart.
    worker = chat_runner.ChatJobWorker(sessions, MockProvider(), ledger, poll_seconds=0.05)
    assert worker.tick() == [user_id]
    worker.stop(timeout=10)

    with sessions() as reader:
        interrupted = reader.get(ChatJob, cut_off)
        assert interrupted.state == "interrupted"
        # The waiting question of the same conversation now waits behind the interrupted one.
        assert reader.get(ChatJob, waiting).state == "paused"
        assert reader.get(ConversationHold, conversation_id).job_id == cut_off
        question_id = interrupted.question_message_id

    with sessions() as session:
        chat_queue.skip(session, user_id, conversation_id, question_id)
        session.commit()
    assert chat_runner.run_next(sessions, MockProvider(), ledger, user_id)
    with sessions() as reader:
        resumed = reader.get(ChatJob, waiting)
        assert resumed.state == "completed"
        assert reader.get(Message, resumed.answer_message_id).content == "모의 답변입니다."
