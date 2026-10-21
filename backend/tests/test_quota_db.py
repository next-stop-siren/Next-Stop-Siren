"""Member quota reservation, settlement and concurrent admission on PostgreSQL (#15).

Constraint and settlement checks roll back with `orm_test_session`. Concurrent
admissions need commits that other connections can see, so those run in a
temporary schema that is dropped afterwards. Every user and message is fake.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db import create_db_engine
from app.models.conversation import Conversation, Message
from app.models.quota import ChatQuota, QuotaReservation
from app.models.user import User
from app.services import chat, quota
from app.services.quota import DAILY_LIMIT, KST, SIX_HOUR_LIMIT, QuotaRejected, periods_at
from db_guard import guarded_live_url

pytestmark = pytest.mark.db

WORKERS = 6


def kst(hour: int, minute: int = 0, second: int = 0, day: int = 20) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=KST)


def add_user(session, email: str = "quota@example.test") -> int:
    user = User(email=email, password_hash="fake-hash")
    session.add(user)
    session.flush()
    return user.id


def fill(session, user_id: int, count: int, moment: datetime, *, state: str = "settled") -> None:
    """Add earlier reservations in the periods of `moment` without the interval check."""
    if session.get(ChatQuota, user_id) is None:
        session.add(ChatQuota(user_id=user_id))
        session.flush()
    periods = periods_at(moment)
    for _ in range(count):
        session.add(
            QuotaReservation(
                user_id=user_id,
                day_start=periods.day_start,
                six_hour_start=periods.six_hour_start,
                state=state,
                created_at=moment - timedelta(hours=1),
                resolved_at=None if state == "reserved" else moment - timedelta(hours=1),
            )
        )
    session.flush()


@pytest.mark.parametrize(
    "values",
    [
        {"state": "unknown"},
        {"state": "reserved", "refund_reason": "failed"},
        {"state": "settled"},
        {"state": "refunded", "refund_reason": "failed"},
        {"state": "refunded", "refund_reason": "other", "resolved_at": kst(1)},
        # NULL IN (...) is unknown, which a CHECK alone would let through.
        {"state": "refunded", "refund_reason": None, "resolved_at": kst(1)},
        # The job state is spelled `canceled`.
        {"state": "refunded", "refund_reason": "cancelled", "resolved_at": kst(1)},
        {"state": "settled", "refund_reason": "failed", "resolved_at": kst(1)},
    ],
)
def test_the_database_rejects_reservations_of_the_wrong_shape(orm_test_session, values):
    user_id = add_user(orm_test_session)
    orm_test_session.add(ChatQuota(user_id=user_id))
    orm_test_session.flush()
    orm_test_session.add(
        QuotaReservation(user_id=user_id, day_start=kst(0), six_hour_start=kst(0), created_at=kst(1), **values)
    )
    with pytest.raises(IntegrityError):
        orm_test_session.flush()


def test_a_reservation_is_settled_or_refunded_exactly_once(orm_test_session):
    user_id = add_user(orm_test_session)
    first = quota.reserve(orm_test_session, user_id, now=kst(9))
    second = quota.reserve(orm_test_session, user_id, now=kst(9, 0, 10))

    assert quota.settle(orm_test_session, first.id) is True
    assert quota.settle(orm_test_session, first.id) is False
    assert quota.refund(orm_test_session, first.id, "failed") is False
    assert quota.refund(orm_test_session, second.id, "interrupted") is True
    assert quota.refund(orm_test_session, second.id, "interrupted") is False
    assert quota.settle(orm_test_session, second.id) is False

    orm_test_session.expire_all()
    stored = {row.id: (row.state, row.refund_reason) for row in orm_test_session.scalars(select(QuotaReservation))}
    assert stored == {first.id: ("settled", None), second.id: ("refunded", "interrupted")}
    current = quota.limits(orm_test_session, user_id, now=kst(9, 1))
    assert (current.daily.used, current.daily.reserved, current.daily.remaining) == (1, 0, DAILY_LIMIT - 1)
    assert (current.six_hour.used, current.six_hour.reserved, current.six_hour.remaining) == (1, 0, SIX_HOUR_LIMIT - 1)


def test_reserved_and_settled_count_against_the_limit_and_a_refund_frees_it(orm_test_session):
    user_id = add_user(orm_test_session)
    fill(orm_test_session, user_id, SIX_HOUR_LIMIT - 2, kst(13), state="settled")
    held = quota.reserve(orm_test_session, user_id, now=kst(13))
    quota.reserve(orm_test_session, user_id, now=kst(13, 0, 10))

    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(orm_test_session, user_id, now=kst(13, 0, 20))
    assert (rejected.value.reason, rejected.value.retry_after) == ("quota_exceeded", 5 * 3600 - 20)
    current = quota.limits(orm_test_session, user_id, now=kst(13, 0, 20))
    assert (current.six_hour.used, current.six_hour.reserved, current.six_hour.remaining) == (98, 2, 0)

    # An unknown outcome keeps its reservation until it is resolved.
    assert quota.refund(orm_test_session, held.id, "failed")
    assert quota.reserve(orm_test_session, user_id, now=kst(13, 0, 30)).six_hour_start == kst(12)


def test_the_daily_limit_spans_the_six_hour_periods(orm_test_session):
    user_id = add_user(orm_test_session)
    for hour in (0, 6, 12, 18):
        fill(orm_test_session, user_id, SIX_HOUR_LIMIT, kst(hour))
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(orm_test_session, user_id, now=kst(23, 30))
    assert (rejected.value.reason, rejected.value.retry_after) == ("quota_exceeded", 1800)
    # No carry-over: the next KST day starts again from the full limit.
    reserved = quota.reserve(orm_test_session, user_id, now=kst(0, 0, 0, day=21))
    assert reserved.day_start == kst(0, day=21)


def test_a_refund_after_a_reset_does_not_raise_the_new_period(orm_test_session):
    user_id = add_user(orm_test_session)
    old = quota.reserve(orm_test_session, user_id, now=kst(17, 59, 55))
    fill(orm_test_session, user_id, SIX_HOUR_LIMIT, kst(18))

    assert quota.refund(orm_test_session, old.id, "expired")
    with pytest.raises(QuotaRejected) as rejected:
        quota.reserve(orm_test_session, user_id, now=kst(18, 5))
    assert rejected.value.reason == "quota_exceeded"
    current = quota.limits(orm_test_session, user_id, now=kst(18, 5))
    assert current.six_hour.remaining == 0
    assert current.daily.remaining == DAILY_LIMIT - SIX_HOUR_LIMIT


def test_members_have_separate_quotas(orm_test_session):
    first = add_user(orm_test_session, "quota-a@example.test")
    second = add_user(orm_test_session, "quota-b@example.test")
    fill(orm_test_session, first, SIX_HOUR_LIMIT, kst(7))
    with pytest.raises(QuotaRejected):
        quota.reserve(orm_test_session, first, now=kst(7, 30))
    assert quota.reserve(orm_test_session, second, now=kst(7, 30)).user_id == second


@pytest.fixture
def committed_quota_sessions():
    """Sessions that commit in a temporary schema of the guarded test DB."""
    engine = create_db_engine(guarded_live_url())
    schema = f"quota_test_{uuid4().hex}"
    scoped = engine.execution_options(schema_translate_map={None: schema})
    tables = [table.__table__ for table in (User, ChatQuota, QuotaReservation, Conversation, Message)]
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


def test_concurrent_admissions_never_reserve_beyond_the_last_place(committed_quota_sessions, monkeypatch):
    # The interval is checked first and would refuse every admission after the first
    # at the same moment, so turn it off to show that the limit itself is counted atomically.
    monkeypatch.setattr(quota, "MIN_INTERVAL_SECONDS", 0)
    sessions = committed_quota_sessions
    with sessions() as setup:
        user_id = add_user(setup)
        fill(setup, user_id, SIX_HOUR_LIMIT - 1, kst(10))
        setup.commit()

    def admit(_index: int):
        with sessions() as session:
            reservation = quota.reserve(session, user_id, now=kst(10))
            session.commit()
            return reservation.id

    results = run_together(WORKERS, admit)

    admitted = [result for result in results if isinstance(result, int)]
    refused = [result for result in results if isinstance(result, QuotaRejected)]
    assert len(admitted) == 1 and len(refused) == WORKERS - 1
    assert {error.reason for error in refused} == {"quota_exceeded"}
    with sessions() as reader:
        assert reader.scalar(select(func.count()).select_from(QuotaReservation)) == SIX_HOUR_LIMIT


def test_concurrent_admissions_from_many_tabs_start_one_generation_per_interval(committed_quota_sessions):
    sessions = committed_quota_sessions
    with sessions() as setup:
        user_id = add_user(setup)
        setup.commit()

    def admit(_index: int):
        with sessions() as session:
            reservation = quota.reserve(session, user_id)
            session.commit()
            return reservation.id

    results = run_together(WORKERS, admit)

    admitted = [result for result in results if isinstance(result, int)]
    refused = [result for result in results if isinstance(result, QuotaRejected)]
    assert len(admitted) == 1 and len(refused) == WORKERS - 1
    assert all(error.reason == "attempt_too_soon" for error in refused)
    with sessions() as reader:
        assert reader.scalar(select(func.count()).select_from(QuotaReservation)) == 1


def test_concurrent_refunds_of_one_reservation_resolve_it_once(committed_quota_sessions):
    sessions = committed_quota_sessions
    with sessions() as setup:
        user_id = add_user(setup)
        reservation_id = quota.reserve(setup, user_id, now=kst(8)).id
        setup.commit()

    def resolve(index: int):
        with sessions() as session:
            done = (
                quota.settle(session, reservation_id)
                if index % 2
                else quota.refund(session, reservation_id, "interrupted")
            )
            session.commit()
            return done

    results = run_together(WORKERS, resolve)

    assert results.count(True) == 1 and results.count(False) == WORKERS - 1
    with sessions() as reader:
        stored = reader.scalars(select(QuotaReservation)).one()
        assert stored.state in ("settled", "refunded") and stored.resolved_at is not None


def test_concurrent_questions_in_one_conversation_leave_one_pending_answer(committed_quota_sessions):
    sessions = committed_quota_sessions
    with sessions() as setup:
        user_id = add_user(setup)
        conversation = Conversation(user_id=user_id)
        setup.add(conversation)
        setup.flush()
        conversation_id = conversation.id
        setup.commit()

    def ask(index: int):
        with sessions() as session:
            return chat.begin_question(
                session, user_id, conversation_id, f"질문 {index}", f"q-race-{index}", trace_id=f"trace-{index}"
            )

    results = run_together(WORKERS, ask)

    assert sum(isinstance(result, chat.Started) for result in results) == 1
    assert sum(isinstance(result, chat.ConversationBusy) for result in results) == WORKERS - 1
    with sessions() as reader:
        pending = select(func.count()).where(Message.role == "assistant", Message.status == "pending")
        assert reader.scalar(pending.select_from(Message)) == 1
