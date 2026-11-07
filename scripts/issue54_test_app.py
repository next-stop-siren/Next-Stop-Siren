"""Test-only API app for Issue #54's controlled stream and notification barriers."""

from collections.abc import Iterator, Sequence
import os
from pathlib import Path
import re
import time

from sqlalchemy import create_engine

import app.db as db
import app.main as main
from app.api.chat_events import ChatEvents
from app.integrations.provider import AnswerProvider, ContextMessage, ProviderTimeout, get_provider as mock_provider
from app.models.chat_job import ChatJob
from app.models.conversation import Message

SCHEMA = os.environ["S54_SCHEMA"]
CONTROL_DIR = Path(os.environ["S54_CONTROL_DIR"])
WORKTREE_BACKEND = Path(__file__).resolve().parents[1] / "backend"
TAG = re.compile(r"^issue54-(pre|middle|notify)-([0-9a-f]{8})$")

if not Path(main.__file__).resolve().is_relative_to(WORKTREE_BACKEND):
    raise RuntimeError("Issue #54 API source resolved outside this worktree")
print(f"Issue #54 API source: {Path(main.__file__).resolve()}", flush=True)


def isolated_engine(url: str):
    """Route every application session into this run's temporary schema."""
    return create_engine(
        db.psycopg_url(url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
        connect_args={"connect_timeout": 3, "options": f"-c search_path={SCHEMA} -c statement_timeout=5000"},
    )


def marker(messages: Sequence[ContextMessage]) -> tuple[str, str] | None:
    match = TAG.fullmatch(messages[-1].content) if messages else None
    return (match.group(1), match.group(2)) if match else None


def gate(token: str, stage: str, timeout: float) -> None:
    reached = CONTROL_DIR / f"{token}.{stage}"
    release = CONTROL_DIR / f"{token}.{stage}.release"
    reached.write_text("reached", encoding="utf-8")
    deadline = time.monotonic() + timeout
    while not release.exists():
        if time.monotonic() >= deadline:
            raise ProviderTimeout("Issue #54 test barrier expired")
        time.sleep(0.02)


class Issue54Provider:
    def __init__(self, fallback: AnswerProvider) -> None:
        self.fallback = fallback

    def complete(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> str:
        return self.fallback.complete(messages, timeout_seconds=timeout_seconds)

    def stream(self, messages: Sequence[ContextMessage], *, timeout_seconds: float) -> Iterator[str]:
        selected = marker(messages)
        if selected is None:
            yield from self.fallback.stream(messages, timeout_seconds=timeout_seconds)
            return

        phase, token = selected
        if phase == "pre":
            gate(token, "before_first_delta", min(timeout_seconds - 2, 25))
            yield "첫 조각 "
            yield "저장 답변"
        elif phase == "middle":
            yield "중간 조각 "
            gate(token, "during_delta", min(timeout_seconds - 2, 25))
            yield "뒤 저장 답변"
        else:
            yield "완료 알림 "
            yield "직전 저장 답변"


_publish = ChatEvents.publish


def publish_with_barrier(self, session, user_id: int, job_ids: Sequence[int]) -> None:
    """Pause only after the completed result has committed, immediately before event publication."""
    for job_id in job_ids:
        job = session.get(ChatJob, job_id)
        if job is None or job.state != "completed":
            continue
        question = session.get(Message, job.question_message_id)
        selected = None if question is None else TAG.fullmatch(question.content or "")
        if selected and selected.group(1) == "notify":
            gate(selected.group(2), "after_commit_before_publish", 60)
            break
    _publish(self, session, user_id, job_ids)


db.create_db_engine = isolated_engine
main.get_provider = lambda: Issue54Provider(mock_provider())
ChatEvents.publish = publish_with_barrier
app = main.app
