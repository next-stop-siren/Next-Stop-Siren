"""Run Issue #54's browser integration against a guarded, temporary PostgreSQL schema."""

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import local
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def app_engine(url: str, schema: str):
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.db import psycopg_url

    return create_engine(
        psycopg_url(url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
        connect_args={
            "connect_timeout": 3,
            "options": f"-c search_path={schema} -c statement_timeout=5000",
        },
    )


class ProbeHandler(BaseHTTPRequestHandler):
    engine = None
    control_dir: Path | None = None
    provider_log: Path | None = None

    @staticmethod
    def iso(value):
        return value.isoformat() if value is not None else None

    @classmethod
    def serialize_job(cls, session, job):
        from app.models.conversation import Message
        from app.models.quota import QuotaReservation

        question = session.get(Message, job.question_message_id)
        answer = session.get(Message, job.answer_message_id) if job.answer_message_id else None
        reservation = session.get(QuotaReservation, job.reservation_id) if job.reservation_id else None
        return {
            "user_id": str(job.user_id),
            "conversation_id": str(job.conversation_id),
            "question_id": str(job.question_message_id),
            "question": question.content if question else None,
            "job_id": str(job.id),
            "job_state": job.state,
            "job_kind": job.kind,
            "accepted_at": cls.iso(job.accepted_at),
            "expires_at": cls.iso(job.expires_at),
            "answer_id": str(answer.id) if answer else None,
            "answer_status": answer.status if answer else None,
            "answer_content": answer.content if answer else None,
            "reservation_id": str(reservation.id) if reservation else None,
            "reservation_state": reservation.state if reservation else None,
            "refund_reason": reservation.refund_reason if reservation else None,
        }

    def send_json(self, status: int, body):
        encoded = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/provider-events":
            rows = []
            if self.provider_log is not None and self.provider_log.exists():
                rows = [json.loads(line) for line in self.provider_log.read_text(encoding="utf-8").splitlines() if line]
            self.send_json(200, rows)
            return

        from app.models.chat_job import ChatJob
        from app.models.conversation import Message
        from app.models.quota import QuotaReservation
        from app.models.user import User

        with Session(self.engine) as session:
            if parsed.path == "/state":
                question = params.get("question", [""])[0]
                job = session.scalar(
                    select(ChatJob)
                    .join(Message, ChatJob.question_message_id == Message.id)
                    .where(Message.role == "user", Message.content == question)
                    .order_by(ChatJob.id.desc())
                    .limit(1)
                )
                if job is None:
                    self.send_json(404, {"error": "not found"})
                    return
                self.send_json(200, self.serialize_job(session, job))
                return

            if parsed.path == "/member":
                email = params.get("email", [""])[0]
                user = session.scalar(select(User).where(User.email == email))
                if user is None:
                    self.send_json(404, {"error": "not found"})
                    return
                jobs = session.scalars(select(ChatJob).where(ChatJob.user_id == user.id).order_by(ChatJob.id)).all()
                from app.repositories import chat_jobs as job_repository
                from app.services import quota as quota_service

                quota = quota_service.limits(session, user.id)
                queue = job_repository.waiting_counts(session, user.id)
                question_count = session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        Message.conversation_id.in_(select_from_user_conversations(user.id)),
                        Message.role == "user",
                    )
                )
                reservation_count = session.scalar(
                    select(func.count()).select_from(QuotaReservation).where(QuotaReservation.user_id == user.id)
                )
                self.send_json(
                    200,
                    {
                        "user_id": str(user.id),
                        "email": user.email,
                        "question_count": question_count,
                        "reservation_count": reservation_count,
                        "items": [self.serialize_job(session, job) for job in jobs],
                        "limits": {
                            "daily": {
                                "limit": quota.daily.limit,
                                "used": quota.daily.used,
                                "reserved": quota.daily.reserved,
                                "remaining": quota.daily.remaining,
                                "resets_at": self.iso(quota.daily.resets_at),
                            },
                            "six_hour": {
                                "limit": quota.six_hour.limit,
                                "used": quota.six_hour.used,
                                "reserved": quota.six_hour.reserved,
                                "remaining": quota.six_hour.remaining,
                                "resets_at": self.iso(quota.six_hour.resets_at),
                            },
                            "queue": {
                                "running": queue[0],
                                "questions_waiting": queue[1],
                                "questions_max": 3,
                                "retries_waiting": queue[2],
                                "retries_max": 1,
                            },
                            "next_attempt_at": self.iso(quota.next_attempt_at),
                        },
                    },
                )
                return

        self.send_error(404)

    def do_POST(self):
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        from app.models.chat_job import ChatJob
        from app.models.quota import ChatQuota, QuotaReservation
        from app.models.user import User

        with Session(self.engine) as session:
            if parsed.path == "/control/age":
                from datetime import timedelta

                from app.models.conversation import Message

                question = params.get("question", [""])[0]
                seconds = int(params.get("seconds", ["0"])[0])
                job = session.scalar(
                    select(ChatJob)
                    .join(Message, ChatJob.question_message_id == Message.id)
                    .where(Message.role == "user", Message.content == question)
                    .order_by(ChatJob.id.desc())
                    .limit(1)
                )
                if job is None or job.state not in ("queued", "paused") or seconds <= 0:
                    self.send_json(409, {"error": "waiting job and positive age required"})
                    return
                job.accepted_at -= timedelta(seconds=seconds)
                job.expires_at -= timedelta(seconds=seconds)
                session.commit()
                self.send_json(
                    200,
                    {
                        "job_id": str(job.id),
                        "accepted_at": self.iso(job.accepted_at),
                        "expires_at": self.iso(job.expires_at),
                    },
                )
                return

            email = params.get("email", [""])[0]
            user = session.scalar(select(User).where(User.email == email))
            if user is None:
                self.send_json(404, {"error": "not found"})
                return
            quota_row = session.get(ChatQuota, user.id)
            if parsed.path == "/control/interval":
                from datetime import timedelta

                from app.repositories import quotas as quota_repository

                now = quota_repository.database_time(session)
                if quota_row is None:
                    quota_row = ChatQuota(user_id=user.id, last_reserved_at=now - timedelta(seconds=11))
                    session.add(quota_row)
                else:
                    quota_row.last_reserved_at = now - timedelta(seconds=11)
                session.commit()
                self.send_json(200, {"last_reserved_at": self.iso(quota_row.last_reserved_at)})
                return

            if parsed.path == "/control/quota":
                from app.repositories import quotas as quota_repository
                from app.services.quota import SIX_HOUR_LIMIT, periods_at

                now = quota_repository.database_time(session)
                period = periods_at(now)
                existing = session.scalar(
                    select(func.count())
                    .select_from(QuotaReservation)
                    .where(
                        QuotaReservation.user_id == user.id,
                        QuotaReservation.six_hour_start == period.six_hour_start,
                        QuotaReservation.state != "refunded",
                    )
                )
                if existing > SIX_HOUR_LIMIT:
                    self.send_json(409, {"error": "current six-hour quota already over limit"})
                    return
                for _ in range(SIX_HOUR_LIMIT - existing):
                    session.add(
                        QuotaReservation(
                            user_id=user.id,
                            day_start=period.day_start,
                            six_hour_start=period.six_hour_start,
                            state="settled",
                            refund_reason=None,
                            created_at=now,
                            resolved_at=now,
                        )
                    )
                if quota_row is None:
                    session.add(ChatQuota(user_id=user.id, last_reserved_at=None))
                session.commit()
                self.send_json(200, {"six_hour_settled": SIX_HOUR_LIMIT})
                return

        self.send_error(404)

    def log_message(self, _format, *_args):
        pass


def select_from_user_conversations(user_id: int):
    from app.models.conversation import Conversation

    return select(Conversation.id).where(Conversation.user_id == user_id)


def run_checked(url: str) -> None:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from app.db_init import initialize_with_engine
    from app.models import register_models
    from app.models.chat_job import ChatJob
    from app.models.conversation import Message
    from app.models.quota import QuotaReservation
    from sqlalchemy import func

    schema = f"issue54_{uuid4().hex}"
    print(f"Issue #54 temporary schema: {schema}", flush=True)
    engine = None
    api = None
    vite = None
    probe = None
    probe_thread = None
    try:
        engine = app_engine(url, schema)
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        register_models()
        initialize_with_engine(engine.execution_options(schema_translate_map={None: schema}))

        with TemporaryDirectory(prefix="issue54-") as temporary:
            temp = Path(temporary)
            control_dir = temp / "control"
            control_dir.mkdir()
            evidence_file = temp / "browser-evidence.jsonl"
            evidence_file.touch()
            provider_log = temp / "provider-events.jsonl"
            provider_log.touch()
            api_port, web_port = free_port(), free_port()
            api_origin = f"http://127.0.0.1:{api_port}"
            web_origin = f"http://127.0.0.1:{web_port}"

            ProbeHandler.engine = engine
            ProbeHandler.control_dir = control_dir
            ProbeHandler.provider_log = provider_log
            probe = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
            probe.daemon_threads = True
            probe_thread = threading.Thread(target=probe.serve_forever, daemon=True)
            probe_thread.start()

            api_env = {
                **local.command_env(),
                **local.e2e_auth_env(),
                # One E2E API process creates more fixture accounts than the default registration cap.
                "AUTH_REGISTER_LIMIT": "100",
                "DATABASE_URL": url,
                "S54_SCHEMA": schema,
                "S54_CONTROL_DIR": str(control_dir),
                "S54_PROVIDER_LOG": str(provider_log),
                "AUTH_API_ORIGIN": api_origin,
                "AUTH_ALLOWED_ORIGINS": f"{web_origin},{api_origin}",
                "PYTHONPATH": os.pathsep.join((str(ROOT), str(BACKEND))),
            }
            api = local.spawn(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "scripts.issue54_test_app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                BACKEND,
                env=api_env,
            )
            local.wait_http(f"{api_origin}/api/health", 200)

            vite_env = {
                **local.command_env(),
                "S54_API_ORIGIN": api_origin,
                "S54_WEB_PORT": str(web_port),
            }
            vite = local.spawn(
                local.tool(
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--config",
                    "e2e/issue54.vite.config.ts",
                    "--port",
                    str(web_port),
                ),
                FRONTEND,
                env=vite_env,
            )
            local.wait_http(f"{web_origin}/", 200)

            browser_env = {
                **local.command_env(),
                "E2E_ORIGIN": web_origin,
                "S54_PROBE_URL": f"http://127.0.0.1:{probe.server_port}",
                "S54_CONTROL_DIR": str(control_dir),
                "S54_EVIDENCE_FILE": str(evidence_file),
                "PLAYWRIGHT_BROWSERS_PATH": str(ROOT / ".cache" / "playwright"),
            }
            local.run(
                local.tool(
                    "npm",
                    "run",
                    "test:e2e",
                    "--",
                    "--config",
                    "e2e/issue54.playwright.config.ts",
                    *sys.argv[1:],
                ),
                cwd=FRONTEND,
                timeout=300,
                env=browser_env,
            )

            records = [json.loads(line) for line in evidence_file.read_text(encoding="utf-8").splitlines() if line]
            provider_events = [
                json.loads(line) for line in provider_log.read_text(encoding="utf-8").splitlines() if line
            ]
            verified = []
            with Session(engine) as session:
                for record in records:
                    job = session.get(ChatJob, int(record["job_id"]))
                    if job is None:
                        raise local.Failure(
                            f"Job {record['job_id']} is missing from the isolated database",
                            4,
                        )
                    question = session.get(Message, job.question_message_id)
                    answer = session.get(Message, job.answer_message_id) if job.answer_message_id else None
                    reservation = session.get(QuotaReservation, job.reservation_id) if job.reservation_id else None
                    if question is None or question.content != record["question"]:
                        raise local.Failure(
                            f"Job {record['job_id']} no longer matches its browser evidence",
                            4,
                        )
                    verified.append(
                        {
                            "job_id": str(job.id),
                            "state": job.state,
                            "answer_state": answer.status if answer else None,
                            "reservation_state": reservation.state if reservation else None,
                        }
                    )
                counts = {
                    "questions": session.scalar(
                        select(func.count()).select_from(Message).where(Message.role == "user")
                    ),
                    "answers": session.scalar(
                        select(func.count()).select_from(Message).where(Message.role == "assistant")
                    ),
                    "jobs": session.scalar(select(func.count()).select_from(ChatJob)),
                    "reservations": session.scalar(select(func.count()).select_from(QuotaReservation)),
                }
            print(
                f"DB evidence: {len(verified)} targeted browser records verified; schema totals={counts}.",
                flush=True,
            )
            print(
                f"Provider call log: {len(provider_events)} start/finish records; "
                "includes question labels, attempt numbers, and call order only.",
                flush=True,
            )
    finally:
        cleanup_issues = []
        for child in (vite, api):
            if child is not None:
                try:
                    local.stop_child(child)
                except (local.Failure, OSError) as error:
                    cleanup_issues.append(f"process group: {error}")
                    try:
                        if child.poll() is None:
                            child.terminate()
                            try:
                                child.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait(timeout=5)
                    except Exception as fallback_error:
                        cleanup_issues.append(f"direct process fallback: {fallback_error}")
        if probe is not None:
            probe.shutdown()
            probe.server_close()
        if probe_thread is not None:
            probe_thread.join(timeout=2)
        if engine is not None:
            try:
                with engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True, if_exists=True))
                if inspect(engine).has_schema(schema):
                    raise local.Failure("Temporary Issue #54 schema remains after cleanup", 4)
                print("Temporary schema cleanup: passed.", flush=True)
            except Exception as cleanup_error:
                cleanup_issues.append(f"temporary schema cleanup: {cleanup_error}")
            finally:
                engine.dispose()
        if cleanup_issues:
            print(
                f"Issue #54 cleanup issues: {' | '.join(cleanup_issues)}",
                flush=True,
            )
            raise local.Failure("Issue #54 cleanup did not finish cleanly", 4)


def database_url_from_local_test_settings() -> str:
    settings = local.read_settings(local.ENV)
    try:
        user, password, database = (settings[key] for key in ("TEST_DB_USER", "TEST_DB_PASSWORD", "TEST_DB_NAME"))
    except KeyError as error:
        raise local.Failure("Local .env is missing dedicated db-test settings", 2) from error
    if "TEST_DATABASE_URL" not in os.environ:
        os.environ["TEST_DATABASE_URL"] = (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@127.0.0.1:55433/{quote(database, safe='')}"
        )
    return os.environ["TEST_DATABASE_URL"]


def main() -> None:
    database_url_from_local_test_settings()
    local.with_test_database(run_checked)


if __name__ == "__main__":
    main()
