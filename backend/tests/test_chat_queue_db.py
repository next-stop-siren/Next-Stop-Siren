"""Persistent member chat queue, execution right and recovery on PostgreSQL (#52).

Single-transaction checks roll back with `orm_test_session`. Races and the runner
need commits that other connections can see, so those run in a temporary schema
that is dropped afterwards. The quota ledger is a fake that records its calls; every
user, question and answer is fake and no real provider is called.
"""

import logging
import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest
from sqlalchemy import func, make_url, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db import create_db_engine
from app.integrations.provider import ContextMessage
from app.models.chat_job import ChatJob, ChatQueueMember, ConversationHold
from app.models.conversation import Conversation, Message
from app.models.quota import ChatQuota, QuotaReservation
from app.models.user import User
from app.repositories import quotas as quota_repository
from app.services import chat_context, chat_queue, chat_runner, quota
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
    """Records its calls; a reservation is a real row, which `chat_jobs.reservation_id` must point to (#53)."""

    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.calls: list[tuple] = []
        self._lock = threading.Lock()

    def reserve(self, session, user_id, *, now):
        if self.refuse:
            raise Refused
        periods = quota.periods_at(now)
        row = quota_repository.add_reservation(
            session,
            quota_repository.lock_quota(session, user_id),
            day_start=periods.day_start,
            six_hour_start=periods.six_hour_start,
            created_at=now,
        )
        with self._lock:
            self.calls.append(("reserve", row.id))
        return row.id

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


def test_the_context_is_in_pair_order_and_leaves_out_the_oldest_pairs_over_the_budget(orm_test_session, monkeypatch):
    """#68 on top of #66: Q2 and Q3 are stored before A1, and the budget drops whole pairs from the oldest."""
    session, ledger = orm_test_session, FakeLedger()
    user_id = add_user(session)
    a = add_conversation(session, user_id)
    for number, key in enumerate(("q1", "q2", "q3")):
        ask(session, ledger, user_id, a, key, at(number * 11))
    run(session, ledger, user_id, Outcome("completed", text="답 q1"), at(40))
    ask(session, ledger, user_id, a, "q4", at(45))
    run(session, ledger, user_id, Outcome("completed", text="답 q2"), at(51))
    third = run(session, ledger, user_id, Outcome("completed", text="답 q3"), at(52))
    assert third.context == [
        ContextMessage("user", "질문 q1"),
        ContextMessage("assistant", "답 q1"),
        ContextMessage("user", "질문 q2"),
        ContextMessage("assistant", "답 q2"),
        ContextMessage("user", "질문 q3"),
    ]
    # Each pair here is 9 characters and the question 5: room for the two newest pairs only.
    monkeypatch.setattr(chat_context, "CONTEXT_CHAR_BUDGET", 23)
    fourth = run(session, ledger, user_id, ANSWERED, at(53))
    assert fourth.context == [
        ContextMessage("user", "질문 q2"),
        ContextMessage("assistant", "답 q2"),
        ContextMessage("user", "질문 q3"),
        ContextMessage("assistant", "답 q3"),
        ContextMessage("user", "질문 q4"),
    ]
    # The left-out pair stays in the history.
    assert session.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == a)) == 8


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


def test_an_expired_retry_keeps_the_conversation_held_and_pauses_a_new_question(orm_test_session):
    session, ledger = orm_test_session, quota.MemberQuota()
    user_id = add_user(session)
    conversation_id = add_conversation(session, user_id)
    failed = ask(session, ledger, user_id, conversation_id, "q1", at(0))
    run(session, ledger, user_id, PROVIDER_FAILED, at(1))
    retry = chat_queue.admit_retry(
        session, ledger, user_id, conversation_id, failed.question_message_id, "r1", now=at(20)
    ).job
    reservation = session.get(QuotaReservation, retry.reservation_id)
    assert reservation.state == "reserved"

    chat_queue.recover(session, ledger, user_id, now=at(20 + 299))
    assert state(session, retry.id) == "queued" and reservation.state == "reserved"
    changes = chat_queue.recover(session, ledger, user_id, now=at(20 + 300))
    assert retry.id in changes.job_ids and state(session, retry.id) == "expired"
    assert (reservation.state, reservation.refund_reason) == ("refunded", "expired")

    held = session.get(ConversationHold, conversation_id)
    assert held.job_id == failed.id and held.question_message_id == failed.question_message_id
    follower = ask(session, ledger, user_id, conversation_id, "q2", at(331))
    assert (follower.state, follower.paused_by_message_id) == ("paused", failed.question_message_id)
    chat_queue.recover(session, ledger, user_id, now=at(400))
    assert state(session, follower.id) == "paused"
    assert session.get(ConversationHold, conversation_id).job_id == failed.id


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
    models = (User, Conversation, Message, ChatQuota, QuotaReservation, ChatQueueMember, ChatJob, ConversationHold)
    tables = [model.__table__ for model in models]
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


@pytest.mark.parametrize("crash_stage", ["accepted", "claim", "provider", "before_commit", "after_commit"])
def test_api_process_restart_recovers_committed_work_at_each_boundary(
    committed_queue_sessions, tmp_path, auth_environment, crash_stage
):
    """SIGKILL the API at durable queue boundaries and inspect the same temporary PostgreSQL schema."""
    from app.security.settings import AuthSettings
    from app.security.tokens import issue_access_token

    sessions = committed_queue_sessions
    with sessions() as setup:
        user_id = add_user(setup, f"process-restart-{uuid4().hex}@example.test")
        first_conversation = add_conversation(setup, user_id)
        later_conversation = add_conversation(setup, user_id) if crash_stage != "accepted" else None
        if crash_stage == "accepted":
            setup.commit()
            first_id = later_id = None
        else:
            now = quota_repository.database_time(setup)
            ledger = quota.MemberQuota()
            first = chat_queue.admit_question(
                setup, ledger, user_id, first_conversation, "프로세스 장애 전 질문", "process-1", now=now
            ).job
            later = chat_queue.admit_question(
                setup,
                ledger,
                user_id,
                later_conversation,
                "재시작 뒤 대기 질문",
                "process-2",
                now=now + timedelta(seconds=11),
            ).job
            setup.commit()
            first_id, later_id = first.id, later.id
    schema = sessions.kw["bind"]._execution_options["schema_translate_map"][None]
    token = issue_access_token(user_id, AuthSettings.from_env(auth_environment)).token

    script = r"""
import os
import time
from sqlalchemy.orm import sessionmaker
import app.db as db

def record(line):
    with open(os.environ["QUEUE_TEST_MARKER"], "a", encoding="utf-8") as marker:
        marker.write(line + "\n")

def hold(stage):
    record("hook:" + stage)
    while True:
        time.sleep(1)

engine = db.create_db_engine(os.environ["TEST_DATABASE_URL"])
scoped = engine.execution_options(schema_translate_map={None: os.environ["QUEUE_TEST_SCHEMA"]})
sessions = sessionmaker(bind=scoped, autoflush=False, expire_on_commit=False)
db.get_session_factory = lambda: sessions
from app import main
main.get_session_factory = lambda: sessions
from app.api.chat_events import ChatEvents
notify = ChatEvents.notify
def observed_notify(self, user_id, job_ids):
    record("notify:" + str(user_id) + ":" + ",".join(str(job_id) for job_id in job_ids))
    return notify(self, user_id, job_ids)
ChatEvents.notify = observed_notify
from app.integrations.provider import MockProvider
from app.services import chat_queue, chat_runner

class CountingProvider:
    def complete(self, messages, *, timeout_seconds):
        raise AssertionError("the runner must stream")

    def stream(self, messages, *, timeout_seconds):
        record("provider:" + messages[-1].content)
        yield from MockProvider.PIECES

mode = os.environ["QUEUE_TEST_MODE"]
if mode == "claim":
    chat_runner.generate = lambda *args, **kwargs: hold("claim")
elif mode == "provider":
    class BlockingProvider:
        def complete(self, messages, *, timeout_seconds):
            raise AssertionError("the runner must stream")

        def stream(self, messages, *, timeout_seconds):
            record("provider:" + messages[-1].content)
            hold("provider")

    main.get_provider = BlockingProvider
elif mode == "before_commit":
    finish = chat_queue.finish
    def before_commit(*args, **kwargs):
        changes = finish(*args, **kwargs)
        hold("before_commit")
        return changes
    chat_queue.finish = before_commit
elif mode == "after_commit":
    log_quota = chat_queue.log_quota
    def before_event(changes):
        if changes.quota:
            hold("after_commit")
        log_quota(changes)
    chat_queue.log_quota = before_event

main.get_provider = main.get_provider if mode == "provider" else CountingProvider
os.environ["CHAT_WORKER"] = "off" if mode == "accepted" else "on"
import uvicorn
uvicorn.run(main.create_app(), host="127.0.0.1", port=int(os.environ["QUEUE_TEST_PORT"]), log_level="critical")
"""

    def start_api(mode: str, marker_path) -> tuple[subprocess.Popen, int]:
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        environment = os.environ.copy()
        environment.update(
            {
                **auth_environment,
                "QUEUE_TEST_MARKER": str(marker_path),
                "QUEUE_TEST_MODE": mode,
                "QUEUE_TEST_PORT": str(port),
                "QUEUE_TEST_SCHEMA": schema,
                "TEST_DATABASE_URL": guarded_live_url(),
            }
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return process, port

    def get_health(process: subprocess.Popen, port: int) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as response:
                    if response.status == 200:
                        assert response.read() == b'{"status":"ok"}'
                        return
            except (HTTPError, URLError, TimeoutError, OSError):
                time.sleep(0.05)
        pytest.fail("the API process did not answer the health request")

    marker = tmp_path / "process-events"
    crash_mode = "accepted" if crash_stage == "accepted" else crash_stage
    crashed, first_port = start_api(crash_mode, marker)
    try:
        get_health(crashed, first_port)
        if crash_stage == "accepted":
            body = json.dumps({"content": "프로세스 장애 전 질문", "request_key": "process-http"}).encode()
            request = Request(
                f"http://127.0.0.1:{first_port}/api/conversations/{first_conversation}/messages",
                data=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                assert response.status == 202
                first_id = int(json.loads(response.read())["job"]["id"])
        else:
            hook = f"hook:{crash_stage}"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert crashed.poll() is None
                if marker.exists() and hook in marker.read_text(encoding="utf-8").splitlines():
                    break
                time.sleep(0.05)
            assert marker.exists() and hook in marker.read_text(encoding="utf-8").splitlines()
        crashed.kill()
        assert crashed.wait(timeout=10) < 0
    finally:
        if crashed.poll() is None:
            crashed.kill()
            crashed.wait(timeout=10)

    before_restart = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
    first_question = "프로세스 장애 전 질문"
    later_question = "재시작 뒤 대기 질문"
    with sessions() as reader:
        first_job = reader.get(ChatJob, first_id)
        first_answer = reader.get(Message, first_job.answer_message_id) if first_job.answer_message_id else None
        first_reservation = reader.get(QuotaReservation, first_job.reservation_id)
        if crash_stage == "accepted":
            assert (first_job.state, first_answer, first_reservation.state) == ("queued", None, "reserved")
        elif crash_stage == "after_commit":
            assert (first_job.state, first_answer.status, first_answer.content, first_reservation.state) == (
                "completed",
                "completed",
                "모의 답변입니다.",
                "settled",
            )
        else:
            assert (first_job.state, first_answer.status, first_reservation.state) == (
                "running",
                "pending",
                "reserved",
            )
            now = quota_repository.database_time(reader)
            first_job.accepted_at = now - timedelta(minutes=3)
            first_job.expires_at = first_job.accepted_at + timedelta(minutes=5)
            first_job.started_at = now - timedelta(minutes=2)
            first_job.deadline_at = first_job.started_at + timedelta(seconds=30)
            reader.commit()

    first_start_notice = f"notify:{user_id}:{first_id}"
    if crash_stage == "accepted":
        assert not any(line.startswith("notify:") for line in before_restart)
    else:
        assert [line for line in before_restart if line.startswith("notify:")] == [first_start_notice]
    if crash_stage == "after_commit":
        assert "hook:after_commit" in before_restart
        assert [line for line in before_restart if line.startswith("provider:")] == [f"provider:{first_question}"]

    restart, restart_port = start_api("restart", marker)
    try:
        get_health(restart, restart_port)
        expected_first = "completed" if crash_stage in {"accepted", "after_commit"} else "interrupted"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with sessions() as reader:
                first_job = reader.get(ChatJob, first_id)
                later_job = reader.get(ChatJob, later_id) if later_id is not None else None
                if first_job.state == expected_first and (later_job is None or later_job.state == "completed"):
                    break
            time.sleep(0.05)
        with sessions() as reader:
            first_job = reader.get(ChatJob, first_id)
            first_answer = reader.get(Message, first_job.answer_message_id)
            first_reservation = reader.get(QuotaReservation, first_job.reservation_id)
            assert first_job.state == expected_first
            if expected_first == "interrupted":
                assert (first_answer.status, first_answer.error_code) == ("interrupted", "outcome_unknown")
                assert (first_reservation.state, first_reservation.refund_reason) == ("refunded", "interrupted")
            else:
                assert (first_answer.status, first_answer.content, first_reservation.state) == (
                    "completed",
                    "모의 답변입니다.",
                    "settled",
                )
            first_attempts = reader.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.reply_to_message_id == first_job.question_message_id)
            )
            assert first_attempts == 1
            if later_id is not None:
                later_job = reader.get(ChatJob, later_id)
                later_answer = reader.get(Message, later_job.answer_message_id)
                later_reservation = reader.get(QuotaReservation, later_job.reservation_id)
                assert (later_job.state, later_answer.status, later_answer.content) == (
                    "completed",
                    "completed",
                    "모의 답변입니다.",
                )
                assert (later_reservation.state, later_reservation.refund_reason) == ("settled", None)
    finally:
        if restart.poll() is None:
            restart.terminate()
            try:
                restart.wait(timeout=10)
            except subprocess.TimeoutExpired:
                restart.kill()
                restart.wait(timeout=10)

    provider_calls = [line for line in marker.read_text(encoding="utf-8").splitlines() if line.startswith("provider:")]
    if crash_stage == "accepted":
        assert provider_calls == [f"provider:{first_question}"]
    elif crash_stage == "claim":
        assert provider_calls == [f"provider:{later_question}"]
    elif crash_stage == "provider":
        assert provider_calls == [f"provider:{first_question}", f"provider:{later_question}"]
    else:
        assert provider_calls == [f"provider:{first_question}", f"provider:{later_question}"]


def test_a_job_id_traces_the_run_to_the_stored_answer_the_settle_and_the_notice(committed_queue_sessions, caplog):
    """From the start to the settle and the completion notice, every step names the admitted job (#70)."""
    sessions = committed_queue_sessions
    ledger, user_id, _, (job_id,) = setup_member(sessions, questions=1)
    from app.integrations.provider import MockProvider

    notified: list[list[int]] = []
    with caplog.at_level(logging.INFO, "app.services"):
        assert chat_runner.run_next(
            sessions, MockProvider(), ledger, user_id, notify=lambda ids: notified.append(list(ids))
        )
    with sessions() as reader:
        job = reader.get(ChatJob, job_id)
        assert job.state == "completed"
        answer_id, reservation_id = job.answer_message_id, job.reservation_id
    traced = [record.getMessage() for record in caplog.records if f"job_id={job_id}," in record.getMessage()]
    assert traced[0] == f"Job started, job_id={job_id}, answer_id={answer_id}"
    assert traced[-2:] == [
        f"Job outcome stored, job_id={job_id}, outcome=completed, answer_id={answer_id}",
        f"Quota resolved, job_id={job_id}, state=completed, reservation_id={reservation_id}, quota=settled",
    ]
    assert ledger.resolutions(reservation_id) == [("settle", reservation_id)]
    assert notified[-1][0] == job_id
    assert "모의 답변입니다." not in caplog.text


def test_a_real_database_error_in_the_runner_logs_no_sql_values_question_or_connection(
    committed_queue_sessions, monkeypatch, caplog
):
    """PostgreSQL repeats a refused bound value in its own error text; the runner's log must not (#70)."""
    sessions = committed_queue_sessions
    ledger, user_id, _, (job_id,) = setup_member(sessions, questions=1)
    marks = {"sql": "sql_marker_70", "value": "value-marker-70", "token": "token-marker-70"}

    def broken_finish(session, _ledger, _job_id, _run_token, _outcome):
        session.execute(
            text("SELECT /* sql_marker_70 */ CAST(:value AS integer), :token"),
            {"value": marks["value"], "token": marks["token"]},
        )

    monkeypatch.setattr(chat_queue, "finish", broken_finish)
    from app.integrations.provider import MockProvider

    notified = []
    worker = chat_runner.ChatJobWorker(
        sessions, MockProvider(), ledger, notify=lambda member_id, job_ids: notified.append((member_id, list(job_ids)))
    )
    with caplog.at_level(logging.INFO):
        worker._drain(user_id)
    worker.stop(timeout=10)

    password = make_url(guarded_live_url()).password
    secrets = [*marks.values(), "질문 k0", "모의 답변입니다.", *([password] if password else [])]
    assert all(record.exc_info is None for record in caplog.records)
    assert not any(secret in caplog.text for secret in secrets)
    assert (
        f"Job outcome not stored, job_id={job_id}, outcome=completed, cause=database_error, "
        "error_type=DataError/InvalidTextRepresentation/22P02"
    ) in caplog.text
    assert f"Chat job run failed, user_id={user_id}, cause=database_error" in caplog.text
    # Not stored is never reported as stored: the job is still running, for recovery to interrupt.
    assert notified == [(user_id, [job_id])]
    with sessions() as reader:
        assert reader.get(ChatJob, job_id).state == "running"
