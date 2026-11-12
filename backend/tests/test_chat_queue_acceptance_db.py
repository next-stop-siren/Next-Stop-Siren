"""A small HTTP/JWT acceptance check over the real PostgreSQL queue and ledger."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.models.chat_job import ChatJob, ConversationHold
from app.models.conversation import Conversation, Message
from app.models.quota import QuotaReservation
from app.models.user import User
from app.repositories.chat_jobs import database_time
from app.services import chat, chat_queue, quota
from app.services.chat_queue import Claimed, PROVIDER_FAILED
from test_auth_chat_boundaries_db import (
    allow_next_admission,
    boundary_database as boundary_database,
    members as members,
)
from test_auth_chat_db import bearer
from test_auth_chat_events_db import live_api as live_api
from test_chat_api_db import committed_sessions as committed_sessions

pytestmark = pytest.mark.db
LEDGER = quota.MemberQuota()


def race_three(*works):
    start = Barrier(3)

    def run(work):
        start.wait(timeout=10)
        try:
            return work()
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=3) as pool:
        return [future.result(timeout=30) for future in [pool.submit(run, work) for work in works]]


def test_real_http_concurrent_admissions_enforce_member_rate_and_waiting_limit(members, live_api):
    actor = members.users[0]
    base_url = str(live_api.client.base_url)
    headers = bearer(actor.token)

    def new_conversation(client):
        response = client.post("/api/conversations", headers=headers, json={})
        assert response.status_code == 201
        return response.json()["conversation"]["id"]

    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as setup_client:
        conversations = [new_conversation(setup_client) for _ in range(4)]

    start = Barrier(2)

    def submit(conversation_id, key):
        start.wait(timeout=10)
        with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
            return client.post(
                f"/api/conversations/{conversation_id}/messages",
                headers=headers,
                json={"content": f"HTTP 합성 질문 {key}", "request_key": key},
            )

    # Two separate authenticated clients race distinct conversations for one member.
    # The fixture's original queued question is admitted at setup time; move only its
    # persisted timestamp back so the race starts with an open 10-second gate.
    allow_next_admission(members.factory, actor.user_id)
    attempts = list(zip(conversations[:2], ("race-a", "race-b")))
    with ThreadPoolExecutor(max_workers=2) as pool:
        raced = list(pool.map(lambda args: (args, submit(*args)), attempts))
    accepted = [(args, response) for args, response in raced if response.status_code == 202]
    refused = [response for _, response in raced if response.status_code == 429]
    assert len(accepted) == len(refused) == 1
    assert refused[0].json()["error"]["code"] == "attempt_too_soon"
    (winner_conversation, winner_key), winner_response = accepted[0]
    winner = winner_response.json()

    # A concurrent resend of the accepted key succeeds inside the gate and creates no second reservation.
    start = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        duplicates = list(
            pool.map(
                lambda _: submit(winner_conversation, winner_key),
                range(2),
            )
        )
    assert all(response.status_code == 202 for response in duplicates)
    assert {response.json()["job"]["id"] for response in duplicates} == {winner["job"]["id"]}

    # Open the time gate only to isolate the 3-wait capacity check from rate refusal.
    allow_next_admission(members.factory, actor.user_id)
    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
        third = client.post(
            f"/api/conversations/{conversations[2]}/messages",
            headers=headers,
            json={"content": "세 번째 대기 질문", "request_key": "third"},
        )
        assert third.status_code == 202
        full = client.post(
            f"/api/conversations/{conversations[3]}/messages",
            headers=headers,
            json={"content": "네 번째 대기 질문", "request_key": "fourth"},
        )
        assert full.status_code == 409 and full.json()["error"]["code"] == "queue_full"
        limits = client.get("/api/chat/limits", headers=headers)
        assert limits.status_code == 200
        assert limits.json()["queue"]["questions_waiting"] == 3

    with members.factory() as reader:
        assert reader.scalar(select(func.count()).select_from(ChatJob).where(ChatJob.user_id == actor.user_id)) == 3
        assert (
            reader.scalar(
                select(func.count()).select_from(QuotaReservation).where(QuotaReservation.user_id == actor.user_id)
            )
            == 3
        )
        assert set(reader.scalars(select(QuotaReservation.state).where(QuotaReservation.user_id == actor.user_id))) == {
            "reserved"
        }
        assert (
            reader.scalar(
                select(func.count())
                .select_from(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.user_id == actor.user_id, Message.role == "user")
            )
            == 3
        )

    # Fill the remaining six-hour window with settled rows; the stored key must still resolve.
    with members.factory() as setup:
        prototype = setup.scalar(select(QuotaReservation).where(QuotaReservation.user_id == actor.user_id).limit(1))
        assert prototype is not None
        setup.add_all(
            [
                QuotaReservation(
                    user_id=actor.user_id,
                    day_start=prototype.day_start,
                    six_hour_start=prototype.six_hour_start,
                    state="settled",
                    created_at=prototype.six_hour_start,
                    resolved_at=prototype.six_hour_start,
                )
                for _ in range(quota.SIX_HOUR_LIMIT - 3)
            ]
        )
        setup.commit()
    with httpx.Client(base_url=base_url, timeout=5, trust_env=False) as client:
        repeat_at_zero = client.post(
            f"/api/conversations/{winner_conversation}/messages",
            headers=headers,
            json={"content": f"HTTP 합성 질문 {winner_key}", "request_key": winner_key},
        )
        assert repeat_at_zero.status_code == 202
        assert repeat_at_zero.json()["job"]["id"] == winner["job"]["id"]
        assert client.get("/api/chat/limits", headers=headers).json()["six_hour"]["remaining"] == 0
    with members.factory() as reader:
        assert (
            reader.scalar(
                select(func.count()).select_from(QuotaReservation).where(QuotaReservation.user_id == actor.user_id)
            )
            == quota.SIX_HOUR_LIMIT
        )


@pytest.mark.parametrize("cancel_at_deadline", [False, True])
def test_expiry_cancel_and_claim_race_resolves_the_real_reservation_once(committed_sessions, cancel_at_deadline):
    with committed_sessions() as setup:
        user = User(email=f"expiry-{uuid4().hex}@example.test", password_hash="fake-hash")
        setup.add(user)
        setup.flush()
        conversation = Conversation(user_id=user.id)
        setup.add(conversation)
        setup.flush()
        accepted_at = database_time(setup)
        admission = chat_queue.admit_question(
            setup, LEDGER, user.id, conversation.id, "경계 합성 질문", "expiry-key", now=accepted_at
        )
        setup.commit()
        user_id, job_id, reservation_id = user.id, admission.job.id, admission.job.reservation_id
        expires_at = admission.job.expires_at

    def claim_before_expiry():
        with committed_sessions() as session:
            claimed, _ = chat_queue.claim_next(session, LEDGER, user_id, now=expires_at - timedelta(seconds=1))
            session.commit()
            return claimed

    def cancel_at_expiry():
        with committed_sessions() as session:
            cancel_time = expires_at if cancel_at_deadline else expires_at - timedelta(milliseconds=500)
            result, _ = chat_queue.cancel(session, LEDGER, user_id, job_id, now=cancel_time)
            session.commit()
            return result.state

    def recover_at_expiry():
        with committed_sessions() as session:
            changes = chat_queue.recover(session, LEDGER, user_id, now=expires_at)
            session.commit()
            return changes.job_ids

    results = race_three(claim_before_expiry, cancel_at_expiry, recover_at_expiry)
    assert results[0] is None or isinstance(results[0], Claimed)
    assert results[1] == "canceled" or isinstance(results[1], chat_queue.JobNotCancelable)
    assert isinstance(results[2], list)
    with committed_sessions() as reader:
        job = reader.get(ChatJob, job_id)
        reservation = reader.get(QuotaReservation, reservation_id)
        answers = list(
            reader.scalars(
                select(Message).where(
                    Message.reply_to_message_id == job.question_message_id,
                    Message.role == "assistant",
                )
            )
        )

    assert job.state in ({"running", "expired"} if cancel_at_deadline else {"running", "expired", "canceled"})
    if job.state == "running":
        assert isinstance(results[0], Claimed) and results[0].job_id == job_id
        assert reservation.state == "reserved" and len(answers) == 1 and answers[0].status == "pending"
    else:
        reason = "expired" if job.state == "expired" else "canceled"
        assert reservation.state == "refunded" and reservation.refund_reason == reason
        assert answers == []
        if job.state == "expired":
            assert job_id in results[2]
    if job.state == "canceled":
        assert results[1] == "canceled"
    elif cancel_at_deadline:
        assert isinstance(results[1], chat_queue.JobNotCancelable)


def test_retry_skip_and_follower_claim_race_preserves_hold_priority_and_quota(committed_sessions):
    with committed_sessions() as setup:
        user = User(email=f"retry-race-{uuid4().hex}@example.test", password_hash="fake-hash")
        setup.add(user)
        setup.flush()
        conversation = Conversation(user_id=user.id)
        setup.add(conversation)
        setup.flush()
        first_at = database_time(setup)
        first = chat_queue.admit_question(
            setup, LEDGER, user.id, conversation.id, "실패 질문", "failed-key", now=first_at
        ).job
        setup.commit()
        failed_run, _ = chat_queue.claim_next(setup, LEDGER, user.id, now=first_at + timedelta(seconds=1))
        chat_queue.finish(
            setup, LEDGER, first.id, failed_run.run_token, PROVIDER_FAILED, now=first_at + timedelta(seconds=2)
        )
        setup.commit()
        follower = chat_queue.admit_question(
            setup,
            LEDGER,
            user.id,
            conversation.id,
            "멈춰 있는 후속 질문",
            "follower-key",
            now=first_at + timedelta(seconds=12),
        ).job
        setup.commit()
        user_id, conversation_id = user.id, conversation.id
        question_id, follower_id = first.question_message_id, follower.id
        first_reservation_id, follower_reservation_id = first.reservation_id, follower.reservation_id
        race_at = first_at + timedelta(seconds=23)

    def admit_retry():
        with committed_sessions() as session:
            result = chat_queue.admit_retry(
                session, LEDGER, user_id, conversation_id, question_id, "retry-key", now=race_at
            )
            session.commit()
            return result.job.id

    def skip_failure():
        with committed_sessions() as session:
            chat_queue.skip(session, user_id, conversation_id, question_id, now=race_at)
            session.commit()
            return "skipped"

    def claim_follower():
        with committed_sessions() as session:
            claimed, _ = chat_queue.claim_next(session, LEDGER, user_id, now=race_at)
            session.commit()
            return claimed

    results = race_three(admit_retry, skip_failure, claim_follower)
    assert isinstance(results[0], int) or isinstance(results[0], chat.NotLatestQuestion)
    assert results[1] == "skipped" or isinstance(results[1], chat_queue.SkipNotAllowed)
    assert results[2] is None or isinstance(results[2], Claimed)
    with committed_sessions() as session:
        queued_retry = session.scalar(select(ChatJob).where(ChatJob.user_id == user_id, ChatJob.kind == "retry"))
        if queued_retry is not None and queued_retry.state == "queued":
            claimed, _ = chat_queue.claim_next(session, LEDGER, user_id, now=race_at + timedelta(seconds=1))
            session.commit()
            assert claimed is not None and claimed.job_id == queued_retry.id
    with committed_sessions() as reader:
        first_job = reader.get(ChatJob, first.id)
        follower_job = reader.get(ChatJob, follower_id)
        retries = list(reader.scalars(select(ChatJob).where(ChatJob.user_id == user_id, ChatJob.kind == "retry")))
        hold = reader.get(ConversationHold, conversation_id)
        reservations = list(reader.scalars(select(QuotaReservation).where(QuotaReservation.user_id == user_id)))
        running_count = reader.scalar(
            select(func.count()).select_from(ChatJob).where(ChatJob.user_id == user_id, ChatJob.state == "running")
        )

    assert first_job.state == "failed"
    assert running_count <= 1
    assert len(retries) <= 1
    retry = retries[0] if retries else None
    if retry is not None:
        # A retry admitted before the follower starts must win claim order; after a start it must be refused.
        assert follower_job.state != "running"
        assert retry.state in {"queued", "running"}
        assert results[0] == retry.id
        assert retry.reservation_id is not None
        retry_reservation = next(row for row in reservations if row.id == retry.reservation_id)
        assert retry_reservation.state == "reserved"
        if first_job.skipped_at is None:
            assert follower_job.state == "paused"
            assert hold is not None and hold.question_message_id == question_id
            assert isinstance(results[1], chat_queue.SkipNotAllowed)
        else:
            assert follower_job.state == "queued"
            assert hold is None and results[1] == "skipped"
    else:
        assert follower_job.state == "running"
        assert isinstance(results[2], Claimed) and results[2].job_id == follower_id
        assert hold is None and first_job.skipped_at is not None
        assert any(isinstance(result, chat.NotLatestQuestion) for result in results)
    assert next(row for row in reservations if row.id == first_reservation_id).state == "refunded"
    assert next(row for row in reservations if row.id == follower_reservation_id).state == "reserved"
    assert sum(row.state == "reserved" for row in reservations) == 1 + int(retry is not None)
