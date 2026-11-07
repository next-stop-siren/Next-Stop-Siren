"""Run Issue #54's browser integration against a guarded, temporary PostgreSQL schema."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import local
from sqlalchemy import create_engine, inspect, select
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
        connect_args={"connect_timeout": 3, "options": f"-c search_path={schema} -c statement_timeout=5000"},
    )


class ProbeHandler(BaseHTTPRequestHandler):
    engine = None

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/state":
            self.send_error(404)
            return
        question = parse_qs(urlsplit(self.path).query).get("question", [""])[0]
        from app.models.chat_job import ChatJob
        from app.models.conversation import Message
        from app.models.quota import QuotaReservation

        with Session(self.engine) as session:
            job = session.scalar(
                select(ChatJob)
                .join(Message, ChatJob.question_message_id == Message.id)
                .where(Message.role == "user", Message.content == question)
            )
            if job is None:
                body = {"error": "not found"}
                status = 404
            else:
                answer = session.get(Message, job.answer_message_id) if job.answer_message_id else None
                reservation = session.get(QuotaReservation, job.reservation_id) if job.reservation_id else None
                body = {
                    "job_id": str(job.id),
                    "job_state": job.state,
                    "answer_status": answer.status if answer else None,
                    "answer_content": answer.content if answer else None,
                    "reservation_state": reservation.state if reservation else None,
                }
                status = 200
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format, *_args):
        pass


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
            api_port, web_port = free_port(), free_port()
            api_origin = f"http://127.0.0.1:{api_port}"
            web_origin = f"http://127.0.0.1:{web_port}"

            ProbeHandler.engine = engine
            probe = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
            probe.daemon_threads = True
            probe_thread = threading.Thread(target=probe.serve_forever, daemon=True)
            probe_thread.start()

            api_env = {
                **local.command_env(),
                **local.e2e_auth_env(),
                "DATABASE_URL": url,
                "S54_SCHEMA": schema,
                "S54_CONTROL_DIR": str(control_dir),
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
                    "npm", "run", "dev", "--", "--config", "e2e/issue54.vite.config.ts", "--port", str(web_port)
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
                    "e2e/issue54.cases.ts",
                ),
                cwd=FRONTEND,
                timeout=240,
                env=browser_env,
            )

            records = [json.loads(line) for line in evidence_file.read_text(encoding="utf-8").splitlines() if line]
            if len(records) != 3:
                raise local.Failure(f"Expected three browser/DB evidence records; found {len(records)}", 4)
            with Session(engine) as session:
                for record in records:
                    job = session.get(ChatJob, int(record["job_id"]))
                    if job is None or job.state != "completed":
                        raise local.Failure(f"Job {record['job_id']} was not completed in the isolated database", 4)
                    question = session.get(Message, job.question_message_id)
                    answer = session.get(Message, job.answer_message_id) if job.answer_message_id else None
                    reservation = session.get(QuotaReservation, job.reservation_id) if job.reservation_id else None
                    if (
                        question is None
                        or question.content != record["question"]
                        or answer is None
                        or answer.status != "completed"
                        or reservation is None
                        or reservation.state != "settled"
                    ):
                        raise local.Failure(f"Job {record['job_id']} has an unexpected saved answer or reservation", 4)
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
                "DB evidence: 3 targeted jobs completed, each has one saved completed answer and a settled "
                f"reservation; isolated-schema totals={counts}.",
                flush=True,
            )
    finally:
        for child in (vite, api):
            if child is not None:
                local.stop_child(child)
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
            finally:
                engine.dispose()


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
