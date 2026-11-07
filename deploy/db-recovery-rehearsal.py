"""Isolated #75 rehearsal: synthetic data in a disposable, network-less PostgreSQL container.

Run with the backend virtualenv's Python (it builds the schema from the app models):

    backend/.venv/bin/python deploy/db-recovery-rehearsal.py --context desktop-linux \
        --evidence-dir /absolute/new/directory/outside/the/repository

It never touches the development/test databases, Compose projects or other
containers: it creates one labelled container and one labelled volume and removes
only those. Every DB operation goes through deploy/db-recovery.py, the same tool the
operator uses, except the deliberate fault injections, which are marked below.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "deploy" / "db-recovery.py"
IMAGE = "postgres:17.11"
LABEL = "org.next-stop-siren.issue75.rehearsal"
spec = importlib.util.spec_from_file_location("db_recovery", TOOL)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)

T0 = "timestamptz '2026-11-05 10:00:00+09'"


def seed_sql(canary: str) -> str:
    """A #52-era shape: completed, failed and paused work in one conversation, queued work in another."""
    return f"""BEGIN;
INSERT INTO users (email, password_hash) VALUES
  ('rehearsal-local@example.test', 'synthetic-password-hash'), ('rehearsal-google@example.test', NULL);
INSERT INTO auth_identities (user_id, issuer, subject) VALUES (2, 'https://accounts.google.com', 'synthetic-subject');
INSERT INTO refresh_sessions (user_id, token_digest, issued_at, expires_at, revoked_at) VALUES
  (1, decode(repeat('ab', 32), 'hex'), {T0}, {T0} + interval '14 days', {T0} + interval '1 hour');
INSERT INTO refresh_sessions (user_id, token_digest, rotated_from_id, issued_at, expires_at) VALUES
  (1, decode(repeat('cd', 32), 'hex'), 1, {T0} + interval '1 hour', {T0} + interval '14 days');
INSERT INTO conversations (user_id, created_at, updated_at) VALUES (1, {T0}, {T0}), (1, {T0}, {T0}), (2, {T0}, {T0});
INSERT INTO messages (conversation_id, role, status, content, request_key, created_at, updated_at, completed_at) VALUES
  (1, 'user', 'completed', '{canary} 질문 1', 'rq-1', {T0}, {T0}, {T0});
INSERT INTO messages (conversation_id, role, status, content, reply_to_message_id, attempt_no, created_at, updated_at,
  completed_at) VALUES (1, 'assistant', 'completed', '{canary} 답변 1', 1, 1, {T0}, {T0}, {T0});
INSERT INTO messages (conversation_id, role, status, content, request_key, created_at, updated_at, completed_at) VALUES
  (1, 'user', 'completed', '{canary} 질문 2', 'rq-2', {T0}, {T0}, {T0});
INSERT INTO messages (conversation_id, role, status, reply_to_message_id, attempt_no, error_code, created_at,
  updated_at) VALUES (1, 'assistant', 'failed', 3, 1, 'provider_error', {T0}, {T0});
INSERT INTO messages (conversation_id, role, status, content, request_key, created_at, updated_at, completed_at) VALUES
  (1, 'user', 'completed', '{canary} 질문 3', 'rq-3', {T0}, {T0}, {T0}),
  (2, 'user', 'completed', '{canary} 질문 4', 'rq-4', {T0}, {T0}, {T0});
INSERT INTO chat_quotas (user_id, last_reserved_at) VALUES (1, {T0});
INSERT INTO chat_quota_reservations (user_id, day_start, six_hour_start, state, refund_reason, created_at,
  resolved_at) VALUES
  (1, {T0}, {T0}, 'settled', NULL, {T0}, {T0}),
  (1, {T0}, {T0}, 'refunded', 'failed', {T0}, {T0}),
  (1, {T0}, {T0}, 'reserved', NULL, {T0}, NULL),
  (1, {T0}, {T0}, 'reserved', NULL, {T0}, NULL);
INSERT INTO chat_queue_members (user_id, created_at) VALUES (1, {T0});
INSERT INTO chat_jobs (user_id, conversation_id, question_message_id, kind, state, reservation_id,
  answer_message_id, run_token, accepted_at, expires_at, started_at, deadline_at, finished_at) VALUES
  (1, 1, 1, 'question', 'completed', 1, 2, '00000000-0000-4000-8000-000000000001',
   {T0}, {T0} + interval '5 minutes', {T0}, {T0} + interval '30 seconds', {T0} + interval '2 seconds'),
  (1, 1, 3, 'question', 'failed', 2, 4, '00000000-0000-4000-8000-000000000002',
   {T0}, {T0} + interval '5 minutes', {T0}, {T0} + interval '30 seconds', {T0} + interval '3 seconds');
INSERT INTO chat_jobs (user_id, conversation_id, question_message_id, kind, state, reservation_id,
  paused_by_message_id, accepted_at, expires_at) VALUES
  (1, 1, 5, 'question', 'paused', 3, 3, {T0}, {T0} + interval '5 minutes');
INSERT INTO chat_jobs (user_id, conversation_id, question_message_id, kind, state, reservation_id, accepted_at,
  expires_at) VALUES (1, 2, 6, 'question', 'queued', 4, {T0}, {T0} + interval '5 minutes');
INSERT INTO chat_conversation_holds (conversation_id, question_message_id, job_id, created_at) VALUES (1, 3, 2, {T0});
COMMIT;
"""


# FAULT INJECTION (rehearsal only): a queued job whose reservation row does not exist,
# the kind of row a #52-era DB without the FK could hold.
ORPHAN_SQL = f"""BEGIN;
INSERT INTO messages (conversation_id, role, status, content, request_key, created_at, updated_at, completed_at) VALUES
  (2, 'user', 'completed', 'synthetic orphan question', 'rq-orphan', {T0}, {T0}, {T0});
INSERT INTO chat_jobs (user_id, conversation_id, question_message_id, kind, state, reservation_id, accepted_at,
  expires_at) VALUES (1, 2, 7, 'question', 'queued', 999999, {T0}, {T0} + interval '5 minutes');
COMMIT;
"""

# FAULT INJECTION: the documented FK statement run directly, to show PostgreSQL itself rejects it atomically.
RAW_FK_SQL = "BEGIN;\n" + recovery.APPLY_FK.split("BEGIN;\n", 1)[1]


def new_job_sql(reservation: str) -> str:
    """A write the current app would make; always rolled back so snapshots stay comparable."""
    return f"""BEGIN;
WITH m AS (INSERT INTO messages (conversation_id, role, status, content, request_key, completed_at)
  VALUES (2, 'user', 'completed', 'synthetic new question', 'rq-new', now()) RETURNING id)
INSERT INTO chat_jobs (user_id, conversation_id, question_message_id, kind, state, reservation_id, accepted_at,
  expires_at) SELECT 1, 2, m.id, 'question', 'queued', {reservation}, {T0}, {T0} + interval '5 minutes' FROM m;
ROLLBACK;
"""


VALID_RESERVATION = "(SELECT id FROM chat_quota_reservations WHERE state = 'reserved' ORDER BY id LIMIT 1)"
COLUMNS_SQL = (
    "SELECT coalesce(json_agg(json_build_array(table_name, column_name)), '[]') FROM information_schema.columns "
    "WHERE table_schema = 'public';\n"
)
SEQUENCE_SQL = "BEGIN;\nINSERT INTO users (email) VALUES ('rehearsal-sequence@example.test') RETURNING id;\nROLLBACK;\n"


def model_ddl() -> str:
    """The exact DDL `create_all()` emits for the registered models, compiled without a connection."""
    sys.path.insert(0, str(ROOT / "backend"))
    from sqlalchemy import create_mock_engine

    from app.db import Base
    from app.models import register_models

    register_models()
    statements = []
    engine = create_mock_engine(
        "postgresql+psycopg://",
        lambda sql, *multiparams, **params: statements.append(str(sql.compile(dialect=engine.dialect)).strip() + ";"),
    )
    Base.metadata.create_all(engine, checkfirst=False)
    return "BEGIN;\n" + "\n".join(statements) + "\nCOMMIT;\n"


def schema_compatible(columns: list) -> str:
    """The app's own readiness rule: every registered table and column exists."""
    from app.readiness import compare_schema, expected_columns

    found: dict[str, set[str]] = {}
    for table, column in columns:
        found.setdefault(table, set()).add(column)
    return compare_schema(expected_columns(), found)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--context", choices=["colima", "desktop-linux"], required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    if evidence == ROOT or ROOT in evidence.parents or evidence.exists():
        parser.error("Use a new evidence directory outside the repository")
    evidence.mkdir(parents=True, mode=0o700)
    work, vault = evidence / "work", evidence / "vault-copy"
    nonce = secrets.token_hex(6)
    canary = "REHEARSAL_PRIVATE_CANARY_" + nonce
    name = f"b71-issue75-{nonce}-db"
    volume = f"b71-issue75-{nonce}-data"
    docker = ["docker", "--context", args.context]
    commands, checks, timings, outputs = [], [], {}, []
    result = {"nonce": nonce, "context": args.context, "image": IMAGE, "checks": checks, "status": "FAIL"}
    owned = False

    def run(argv, *, check=True, input_text=None, timeout=300):
        completed = subprocess.run(
            argv, input=input_text, capture_output=True, text=True, encoding="utf-8", timeout=timeout, cwd=ROOT
        )
        commands.append({"argv": argv, "exit": completed.returncode})
        (evidence / "commands.json").write_text(json.dumps(commands, indent=2), encoding="utf-8")
        if check and completed.returncode:
            (evidence / "failed-command.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
            raise RuntimeError("command_failed")
        return completed

    def psql(sql, *, check=True, database='"$POSTGRES_DB"'):
        script = 'exec psql -X -q -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d ' + database
        return run(docker + ["exec", "-i", name, "sh", "-c", script], input_text=sql, check=check)

    def tool(*command, expect="ok"):
        completed = run(
            [sys.executable, str(TOOL), "--context", args.context, "--db-container", name, "--work-dir", str(work)]
            + [str(part) for part in command],
            check=False,
        )
        outputs.append(completed.stdout + completed.stderr)
        if expect == "ok":
            if completed.returncode:
                (evidence / "failed-command.txt").write_text(completed.stderr, encoding="utf-8")
                raise RuntimeError("tool_failed_" + command[0])
            return json.loads(completed.stdout)
        if completed.returncode != 1 or completed.stderr.strip() != f"DB_RECOVERY_FAILED: {expect}":
            raise RuntimeError("expected_tool_failure_" + expect)
        return None

    def snapshot():
        report = tool("precheck")
        return json.loads(Path(report["report"]).read_text(encoding="utf-8"))["snapshot"]

    def same(before, after, **change):
        changed = recovery.differences(before, after, **change)
        if changed:
            raise RuntimeError("unexpected_changes:" + ",".join(changed))

    def pre_change_backup():
        return Path(tool("backup", "--purpose", "pre-change")["manifest"])

    def passed(check):
        checks.append(check)
        print("PASS", check, flush=True)

    def start_database():
        run(docker + ["volume", "create", "--label", f"{LABEL}={nonce}", volume])
        run(
            docker
            + ["run", "-d", "--name", name, "--label", f"{LABEL}={nonce}", "--network", "none"]
            + ["--platform", "linux/amd64", "--mount", f"type=volume,source={volume},target=/var/lib/postgresql/data"]
            + ["-e", "POSTGRES_USER=rehearsal", "-e", "POSTGRES_DB=rehearsal"]
            + ["-e", "POSTGRES_PASSWORD=" + secrets.token_hex(24), IMAGE],
            timeout=600,
        )
        # The image's init server listens on the socket only; TCP on loopback means the final server is up.
        for _ in range(90):
            ready = ["exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "rehearsal", "-d", "rehearsal"]
            if run(docker + ready, check=False).returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError("database_not_ready")

    def remove_database():
        for kind, target in (("container", name), ("volume", volume)):
            inspected = run(docker + [kind, "inspect", target], check=False)
            if inspected.returncode:
                continue
            labels = json.loads(inspected.stdout)[0]
            labels = labels["Config"]["Labels"] if kind == "container" else labels["Labels"]
            if (labels or {}).get(LABEL) != nonce:
                raise RuntimeError("ownership_changed")
            run(docker + (["rm", "-f", "-v", name] if kind == "container" else ["volume", "rm", volume]))

    try:
        endpoint = json.loads(run(docker + ["context", "inspect", args.context]).stdout)[0]["Endpoints"]["docker"]
        if not endpoint["Host"].startswith(("unix://", "npipe://")):
            raise RuntimeError("local_daemon_required")
        for kind, target in (("container", name), ("volume", volume)):
            if run(docker + [kind, "inspect", target], check=False).returncode == 0:
                raise RuntimeError("rehearsal_resource_exists")
        owned = True
        start_database()
        inspected = json.loads(run(docker + ["inspect", name]).stdout)[0]
        if set(inspected["NetworkSettings"]["Networks"]) != {"none"} or inspected["HostConfig"].get("PortBindings"):
            raise RuntimeError("isolation_required")
        result["server_version"] = psql("SHOW server_version;\n").stdout.strip()
        passed("disposable_networkless_labelled_postgres_17")

        psql(model_ddl())
        psql(f"ALTER TABLE chat_jobs DROP CONSTRAINT {recovery.FK_NAME};\n")  # The #52 state before #53's FK.
        psql(seed_sql(canary))
        base = snapshot()
        if recovery.classify(base) != "needs_reservation_fk" or base["orphan_job_reservations"] != 0:
            raise RuntimeError("unexpected_seed_state")
        result["seed_rows"] = recovery.summary(base)["rows"]
        passed("pre53_schema_from_models_with_synthetic_rows_relations_reservations_jobs")

        same(base, snapshot())
        passed("precheck_is_read_only_and_repeatable")

        backup_0 = Path(tool("backup", "--purpose", "daily")["manifest"])
        manifest_0 = json.loads(backup_0.read_text(encoding="utf-8"))
        if not manifest_0["consistent"]:
            raise RuntimeError("backup_not_consistent")
        same(base, manifest_0["snapshot"])
        verified = tool("verify", backup_0, "--restore-check")
        timings["verify_restore_check_seconds"] = verified["restore_seconds"]
        if psql(f"SELECT count(*) FROM pg_database WHERE datname = '{recovery.CHECK_DB}';\n").stdout.strip() != "0":
            raise RuntimeError("check_database_left_behind")
        same(base, snapshot())
        passed("backup_hash_manifest_and_scratch_restore_match_without_touching_live_db")

        tampered = work / "tampered"
        tampered.mkdir()
        shutil.copy2(backup_0, tampered / backup_0.name)
        data = bytearray((work / manifest_0["file"]).read_bytes())
        data[len(data) // 2] ^= 0xFF
        (tampered / manifest_0["file"]).write_bytes(bytes(data))
        tool("verify", tampered / backup_0.name, expect="backup_sha256_mismatch")
        passed("corrupted_backup_rejected_before_use")

        stale = pre_change_backup()  # Taken before the data below changes.
        psql(ORPHAN_SQL)
        orphaned = snapshot()
        if orphaned["orphan_job_reservations"] != 1:
            raise RuntimeError("orphan_not_injected")
        tool("apply-reservation-fk", backup_0, expect="fresh_pre_change_backup_required")
        tool("apply-reservation-fk", stale, expect="backup_does_not_match_current")
        backup_orphan = pre_change_backup()
        tool("apply-reservation-fk", backup_orphan, expect="orphan_reservations")
        same(orphaned, snapshot())
        passed("daily_or_stale_backup_and_orphan_rows_stop_apply_without_change")

        if psql(RAW_FK_SQL, check=False).returncode == 0:
            raise RuntimeError("database_accepted_orphan_fk")
        same(orphaned, snapshot())
        passed("database_rejects_fk_on_orphans_atomically")

        restored = tool("restore", backup_0)
        timings["restore_after_failure_seconds"] = restored["restore_seconds"]
        same(base, snapshot())
        retained = restored["retained_database"]
        kept_jobs = psql("SELECT count(*) FROM chat_jobs;\n", database=retained).stdout.strip()
        if kept_jobs != str(orphaned["tables"]["chat_jobs"]["rows"]):
            raise RuntimeError("replaced_database_not_retained")
        result["retained_after_failure"] = {"database": retained, "chat_jobs_rows": int(kept_jobs)}
        passed("restore_returns_pre_failure_state_and_keeps_replaced_db")

        backup_1 = pre_change_backup()
        tool("apply-reservation-fk", backup_1)
        applied = snapshot()
        same(base, applied, added=frozenset({recovery.FK_NAME}))
        if tool("apply-reservation-fk", backup_1) != {"command": "apply-reservation-fk", "result": "already_applied"}:
            raise RuntimeError("apply_not_idempotent")
        same(applied, snapshot())
        passed("fk_applied_rows_hashes_relations_reservations_jobs_unchanged_and_idempotent")

        psql(new_job_sql(VALID_RESERVATION))
        if psql(new_job_sql("999999"), check=False).returncode == 0:
            raise RuntimeError("fk_not_enforced")
        same(applied, snapshot())
        passed("fk_accepts_valid_writes_and_rejects_new_orphans")

        columns = json.loads(psql(COLUMNS_SQL).stdout)
        if schema_compatible(columns) != "ok":
            raise RuntimeError("app_schema_incompatible_after_apply")
        backup_2 = pre_change_backup()
        tool("revert-reservation-fk", backup_2)
        reverted = snapshot()
        same(base, reverted)
        if schema_compatible(json.loads(psql(COLUMNS_SQL).stdout)) != "ok":
            raise RuntimeError("app_schema_incompatible_after_revert")
        passed("app_readiness_columns_ok_with_and_without_fk_and_revert_keeps_data")

        backup_3 = pre_change_backup()
        tool("apply-reservation-fk", backup_3)
        final = snapshot()
        same(applied, final)
        backup_4 = Path(tool("backup", "--purpose", "daily")["manifest"])
        vault.mkdir()
        manifest_4 = json.loads(backup_4.read_text(encoding="utf-8"))
        for file in (backup_4, work / manifest_4["file"]):
            shutil.copy2(file, vault / file.name)
        remove_database()
        if run(docker + ["volume", "inspect", volume], check=False).returncode == 0:
            raise RuntimeError("volume_still_present")
        start_database()
        if recovery.classify(snapshot()) != "empty_use_db_init":
            raise RuntimeError("new_volume_not_empty")
        tool("verify", vault / backup_4.name)
        recovered = tool("restore", vault / backup_4.name)
        timings["restore_after_volume_loss_seconds"] = recovered["restore_seconds"]
        same(final, snapshot())
        next_id = int(psql(SEQUENCE_SQL).stdout.strip().splitlines()[0])
        if next_id <= base["tables"]["users"]["rows"]:
            raise RuntimeError("identity_sequence_not_restored")
        passed("volume_loss_recovered_from_offhost_copy_with_sequences")

        leaked = [
            str(path.relative_to(evidence))
            for path in evidence.rglob("*")
            if path.is_file() and path.suffix != ".dump" and canary in path.read_text(encoding="utf-8", errors="ignore")
        ]
        if leaked or any(canary in text for text in outputs):
            raise RuntimeError("row_content_in_reports")
        passed("reports_and_tool_output_hold_no_row_content")
        result["timings"] = timings
        result["status"] = "PASS"
    except Exception as error:
        result["failure_type"] = type(error).__name__
        result["failure"] = str(error)
        import traceback

        (evidence / "failure-stack.txt").write_text(traceback.format_exc(), encoding="utf-8")
        print("FAIL", type(error).__name__, str(error), flush=True)
    finally:
        if owned:
            try:
                remove_database()
                result["cleanup"] = "owned_container_and_volume_removed"
            except Exception:
                result["cleanup"] = "FAILED_preserved_for_manual_inspection"
                result["status"] = "FAIL"
        (evidence / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
