"""Pilot database precheck, backup, verification, schema apply and restore (#75).

An operator tool for the host that runs the stack, used only inside an operation the
Owner approved. It reaches PostgreSQL through `docker exec` into the DB container, so
credentials stay in that container. Reports hold counts, SHA-256 hashes and object
names only, never row contents or settings. Nothing here deletes the live database:
a restore builds a staging database, checks it and then swaps names, keeping the
replaced database until the Owner removes it. See deploy/data-recovery.md.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]

# Registered product tables (app.models.register_models) and the key that orders their rows.
TABLES = {
    "users": "id",
    "auth_identities": "id",
    "refresh_sessions": "id",
    "conversations": "id",
    "messages": "id",
    "chat_quotas": "user_id",
    "chat_quota_reservations": "id",
    "chat_queue_members": "user_id",
    "chat_jobs": "id",
    "chat_conversation_holds": "conversation_id",
}
GROUPS = {"messages": "role || ':' || status", "chat_jobs": "state", "chat_quota_reservations": "state"}
COMPARED = ("tables", "groups", "orphan_job_reservations", "indexes")
FK_NAME = "chat_jobs_reservation_fk"
STAGING_DB = "nss_restore_staging"
CHECK_DB = "nss_restore_check"
BACKUP_MAX_AGE = timedelta(hours=1)
# A restore writes a full copy of the database and about as much WAL again; PostgreSQL's default
# max_wal_size (1 GiB) bounds the WAL it keeps, so that much headroom is reserved on top.
RESTORE_HEADROOM = 1 << 30
MANIFEST_VERSION = 2
PURPOSES = ("daily", "pre-change")
MAIN_DB = '"$POSTGRES_DB"'
# Shell text run inside the DB container; the container supplies user and database names.
PSQL = 'exec psql -X -q -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d {database}'
PG_DUMP = 'exec pg_dump --format=custom -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
PG_RESTORE = (
    'exec pg_restore --exit-on-error --single-transaction --no-owner --no-privileges -U "$POSTGRES_USER" -d {database}'
)
PG_LIST = "exec pg_restore --list"
SESSION = (
    "SET TimeZone = 'UTC'; SET DateStyle = 'ISO, YMD'; SET IntervalStyle = 'postgres'; SET extra_float_digits = 1;\n"
)

APPLY_FK = f"""SET lock_timeout = '5s';
SET statement_timeout = '120s';
BEGIN;
LOCK TABLE chat_jobs, chat_quota_reservations IN SHARE ROW EXCLUSIVE MODE;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{FK_NAME}' AND conrelid = 'chat_jobs'::regclass) THEN
    RAISE EXCEPTION 'already_applied';
  END IF;
END $$;
ALTER TABLE chat_jobs ADD CONSTRAINT {FK_NAME}
  FOREIGN KEY (reservation_id) REFERENCES chat_quota_reservations (id) ON DELETE RESTRICT;
COMMIT;
"""
REVERT_FK = f"""SET lock_timeout = '5s';
BEGIN;
ALTER TABLE chat_jobs DROP CONSTRAINT {FK_NAME};
COMMIT;
"""


class Failure(Exception):
    """A stop with a fixed code; codes never contain data, names of people or settings."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    """Second precision, usable in database names."""
    return moment.strftime("%Y%m%dT%H%M%SZ")


def file_stamp(moment: datetime) -> str:
    """Microsecond precision so reports of quick successive runs never collide."""
    return moment.strftime("%Y%m%dT%H%M%S%fZ")


def snapshot_sql(present: set[str]) -> str:
    """One read-only, repeatable-read query: counts, row hashes, groups and schema object names."""
    tables = ", ".join(
        f"'{name}', (SELECT json_build_object('rows', count(*), 'sha256', encode(sha256(convert_to("
        f"coalesce(string_agg(t::text, E'\\n' ORDER BY t.{key}), ''), 'UTF8')), 'hex')) FROM {name} AS t)"
        for name, key in TABLES.items()
        if name in present
    )
    groups = ", ".join(
        f"'{name}', (SELECT coalesce(json_object_agg(k, n ORDER BY k), '{{}}') "
        f"FROM (SELECT {expression} AS k, count(*) AS n FROM {name} GROUP BY 1) AS g)"
        for name, expression in GROUPS.items()
        if name in present
    )
    orphans = (
        "(SELECT count(*) FROM chat_jobs AS j WHERE j.reservation_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM chat_quota_reservations AS r WHERE r.id = j.reservation_id))"
        if {"chat_jobs", "chat_quota_reservations"} <= present
        else "NULL"
    )
    return (
        SESSION
        + "BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ;\n"
        + "SELECT json_build_object("
        + "'server_version_num', current_setting('server_version_num'), "
        + f"'tables', json_build_object({tables}), 'groups', json_build_object({groups}), "
        + f"'orphan_job_reservations', {orphans}, "
        + "'constraints', (SELECT coalesce(json_agg(c.conname ORDER BY c.conname), '[]') FROM pg_constraint AS c "
        + "JOIN pg_namespace AS n ON n.oid = c.connamespace WHERE n.nspname = 'public'), "
        + "'indexes', (SELECT coalesce(json_agg(indexname ORDER BY indexname), '[]') FROM pg_indexes "
        + "WHERE schemaname = 'public'), "
        + "'database_bytes', pg_database_size(current_database()));\n"
        + "COMMIT;\n"
    )


PRESENT_SQL = (
    "BEGIN READ ONLY;\nSELECT coalesce(json_agg(table_name ORDER BY table_name), '[]') FROM information_schema.tables "
    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';\nCOMMIT;\n"
)


def differences(before: dict, after: dict, *, added: frozenset = frozenset(), removed: frozenset = frozenset()) -> list:
    """Names of what changed between two snapshots, apart from the expected constraint change."""
    changed = []
    for key in COMPARED:
        if key == "tables":
            names = sorted(set(before.get("tables", {})) | set(after.get("tables", {})))
            changed += [f"tables.{name}" for name in names if before["tables"].get(name) != after["tables"].get(name)]
        elif before.get(key) != after.get(key):
            changed.append(key)
    # Constraint names are unique per table only, so compare them as a multiset.
    expected = Counter(before.get("constraints", [])) + Counter(added)
    expected.subtract(Counter(removed))
    if Counter(after.get("constraints", [])) != +expected:
        changed.append("constraints")
    return changed


def classify(snapshot: dict) -> str:
    """Which path applies: empty DB initialization, existing schema apply, or nothing to do."""
    present = set(snapshot["tables"])
    if not present:
        return "empty_use_db_init"
    if present != set(TABLES):
        return "partial_stop"
    if FK_NAME in snapshot["constraints"]:
        return "current"
    return "needs_reservation_fk"


def summary(snapshot: dict) -> dict:
    """Counts only, for console output and handoff notes."""
    return {
        "rows": {name: value["rows"] for name, value in sorted(snapshot["tables"].items())},
        "groups": snapshot["groups"],
        "orphan_job_reservations": snapshot["orphan_job_reservations"],
        "reservation_fk": FK_NAME in snapshot["constraints"],
    }


Runner = Callable[..., bytes]


def run_command(argv, *, code, input_bytes=None, stdin_path=None, stdout_path=None, diagnostics=None, timeout=900):
    """Run one command; on failure keep its error text in a private file and raise only the code."""
    stdin = stdin_path.open("rb") if stdin_path else None
    stdout = stdout_path.open("xb") if stdout_path else None
    try:
        if stdout_path:
            stdout_path.chmod(0o600)
        result = subprocess.run(
            argv,
            input=input_bytes,
            stdin=stdin,
            stdout=stdout or subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise Failure(code + "_timeout") from None
    except OSError:
        raise Failure(code + "_unavailable") from None
    finally:
        for handle in (stdin, stdout):
            if handle:
                handle.close()
    if result.returncode:
        if diagnostics is not None:
            # Server messages can quote key values; keep them in the private work directory only.
            path = diagnostics / f"diagnostics-{file_stamp(utc_now())}-{code}.txt"
            path.write_bytes(result.stderr)
            path.chmod(0o600)
        raise Failure(code)
    return result.stdout or b""


class Database:
    """The DB container reached by `docker exec`; SQL and archives travel on stdin/stdout only."""

    def __init__(self, exec_prefix, *, backend_check=None, runner: Runner = run_command, diagnostics=None):
        self.exec_prefix = exec_prefix
        self.backend_check = backend_check
        self.runner = runner
        self.diagnostics = diagnostics

    def shell(self, script, *, code, **options) -> bytes:
        return self.runner(self.exec_prefix + ["sh", "-c", script], code=code, diagnostics=self.diagnostics, **options)

    def sql(self, statements, *, code, database=MAIN_DB, variables="") -> str:
        script = PSQL.format(database=database) + variables
        return self.shell(script, code=code, input_bytes=statements.encode()).decode().strip()

    def snapshot(self, database=MAIN_DB) -> dict:
        present = set(json.loads(self.sql(PRESENT_SQL, code="schema_read_failed", database=database)))
        result = json.loads(self.sql(snapshot_sql(present), code="snapshot_failed", database=database))
        result["other_tables"] = sorted(present - set(TABLES))
        return result

    def backend_running(self) -> bool:
        if self.backend_check is None:
            return False
        return bool(self.runner(self.backend_check, code="backend_state_unknown").strip())

    def free_bytes(self) -> int:
        out = self.shell("exec df -Pk /var/lib/postgresql/data", code="volume_space_unknown").decode()
        return int(out.strip().splitlines()[-1].split()[3]) * 1024

    def database_exists(self, name) -> bool:
        query = f"SELECT count(*) FROM pg_database WHERE datname = '{name}';\n"
        return self.sql(query, code="database_list_failed", database="postgres") == "1"

    def dump(self, path: Path) -> None:
        self.shell(PG_DUMP, code="backup_dump_failed", stdout_path=path)

    def archive_list(self, path: Path) -> str:
        return self.shell(PG_LIST, code="backup_unreadable", stdin_path=path).decode()

    def restore_into(self, name, path: Path) -> None:
        self.shell(PG_RESTORE.format(database=name), code="restore_failed", stdin_path=path)

    def create(self, name) -> None:
        self.sql(f"CREATE DATABASE {name};\n", code="staging_create_failed", database="postgres")

    def drop(self, name) -> None:
        self.sql(f"DROP DATABASE IF EXISTS {name} WITH (FORCE);\n", code="staging_drop_failed", database="postgres")

    def swap(self, retained) -> None:
        """Atomically put the staged database under the live name and keep the old one."""
        statements = (
            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
            "WHERE datname IN (:'target', '" + STAGING_DB + "') AND pid <> pg_backend_pid();\n"
            "BEGIN;\n"
            f'ALTER DATABASE :"target" RENAME TO {retained};\n'
            f'ALTER DATABASE {STAGING_DB} RENAME TO :"target";\n'
            "COMMIT;\n"
        )
        self.sql(statements, code="swap_failed", database="postgres", variables=' -v target="$POSTGRES_DB"')


def private_write(path: Path, data: dict) -> Path:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    path.chmod(0o600)
    return path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


MANIFEST_KEYS = {
    "schema_version",
    "file",
    "bytes",
    "sha256",
    "created_at",
    "purpose",
    "backend_running",
    "consistent",
    "database_bytes",
    "snapshot",
}


def load_manifest(path: Path) -> tuple[dict, Path]:
    """Validate a manifest and its archive: same directory, size and SHA-256."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Failure("manifest_unreadable") from None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != MANIFEST_KEYS
        or manifest["schema_version"] != MANIFEST_VERSION
        or manifest["purpose"] not in PURPOSES
        or type(manifest["database_bytes"]) is not int
        or manifest["database_bytes"] <= 0
        or not isinstance(manifest["file"], str)
        or Path(manifest["file"]).name != manifest["file"]
        or not manifest["file"].endswith(".dump")
    ):
        raise Failure("manifest_invalid")
    archive = path.parent / manifest["file"]
    if not archive.is_file() or archive.is_symlink():
        raise Failure("backup_missing")
    if archive.stat().st_size != manifest["bytes"]:
        raise Failure("backup_size_mismatch")
    if file_sha256(archive) != manifest["sha256"]:
        raise Failure("backup_sha256_mismatch")
    return manifest, archive


def require_stopped_backend(db: Database) -> None:
    if db.backend_running():
        raise Failure("stop_backend_first")


def require_space(available: int, needed: int, code: str) -> None:
    if available < needed:
        raise Failure(code)


def restore_space_needed(manifest: dict) -> int:
    """Volume space a staged restore needs, from the DB size measured at backup time.

    The compressed archive size says little about the restored size, and a full disk
    during a restore would also stop the live database that shares the volume.
    """
    return manifest["database_bytes"] * 2 + RESTORE_HEADROOM


def require_restore_space(db: "Database", manifest: dict) -> None:
    require_space(db.free_bytes(), restore_space_needed(manifest), "insufficient_volume_space")


def precheck(db: Database, work: Path, now: datetime) -> dict:
    snapshot = db.snapshot()
    report = {
        "command": "precheck",
        "at": now.isoformat(),
        "classification": classify(snapshot),
        "backend_running": db.backend_running(),
        "snapshot": snapshot,
    }
    report["path"] = str(private_write(work / f"precheck-{file_stamp(now)}.json", report))
    return report


def backup(db: Database, work: Path, purpose: str, now: datetime) -> dict:
    running = db.backend_running()
    if purpose == "pre-change" and running:
        raise Failure("stop_backend_first")
    before = db.snapshot()
    # The custom archive is compressed, but keep room for an uncompressed worst case plus margin.
    require_space(shutil.disk_usage(work).free, before["database_bytes"] * 2 + (64 << 20), "insufficient_backup_disk")
    archive = work / f"backup-{file_stamp(now)}-{purpose}.dump"
    if archive.exists():
        raise Failure("backup_exists")
    try:
        db.dump(archive)
    except Failure:
        archive.unlink(missing_ok=True)  # A partial archive must never look like a backup.
        raise
    after = db.snapshot()
    consistent = not differences(before, after)
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "file": archive.name,
        "bytes": archive.stat().st_size,
        "sha256": file_sha256(archive),
        "created_at": now.isoformat(),
        "purpose": purpose,
        "backend_running": running,
        "consistent": consistent,
        # The larger of the two measurements; restore space checks are based on it.
        "database_bytes": max(before["database_bytes"], after["database_bytes"]),
        # Writes during the dump leave no trusted expectation; verify --restore-check then provides one.
        "snapshot": before if consistent else None,
    }
    path = private_write(work / (archive.name.removesuffix(".dump") + ".manifest.json"), manifest)
    if purpose == "pre-change" and not consistent:
        raise Failure("changed_during_backup")
    return {"command": "backup", "manifest": str(path), "consistent": consistent, "bytes": manifest["bytes"]}


def archive_tables(listing: str) -> list[str]:
    tables = set()
    for line in listing.splitlines():
        parts = line.split()
        if "TABLE" in parts and "DATA" in parts and "public" in parts:
            index = parts.index("public")
            if index + 1 < len(parts):
                tables.add(parts[index + 1])
    return sorted(tables)


def staged_restore(db: Database, name: str, archive: Path, expected: dict | None) -> dict:
    """Restore into a new database and snapshot it; drop it again if anything fails."""
    if db.database_exists(name):
        raise Failure("staging_database_exists")
    db.create(name)
    try:
        db.restore_into(name, archive)
        restored = db.snapshot(database=name)
        if expected is not None and differences(expected, restored):
            raise Failure("restored_snapshot_mismatch")
    except BaseException:
        db.drop(name)
        raise
    return restored


def verify(db: Database, work: Path, manifest_path: Path, restore_check: bool, now: datetime) -> dict:
    manifest, archive = load_manifest(manifest_path)
    tables = archive_tables(db.archive_list(archive))
    expected = manifest["snapshot"]
    if expected is not None and not set(expected["tables"]) <= set(tables):
        raise Failure("backup_tables_missing")
    report = {
        "command": "verify",
        "at": now.isoformat(),
        "manifest": manifest_path.name,
        "sha256": manifest["sha256"],
        "archive_tables": tables,
        "restore_check": restore_check,
    }
    if restore_check:
        require_restore_space(db, manifest)
        started = time.monotonic()
        restored = staged_restore(db, CHECK_DB, archive, expected)
        db.drop(CHECK_DB)
        report["restore_seconds"] = round(time.monotonic() - started, 2)
        report["restored_snapshot"] = restored
        report["matches_manifest"] = expected is not None
    report["path"] = str(private_write(work / f"verify-{file_stamp(now)}-{manifest_path.stem}.json", report))
    return report


def change_gate(db: Database, manifest_path: Path, now: datetime) -> dict:
    """A schema change needs a stopped backend and a verified pre-change backup of exactly this state."""
    require_stopped_backend(db)
    before = db.snapshot()
    if classify(before) not in {"current", "needs_reservation_fk"}:
        raise Failure("schema_incomplete")
    manifest, _ = load_manifest(manifest_path)
    created = datetime.fromisoformat(manifest["created_at"])
    if manifest["purpose"] != "pre-change" or not now - BACKUP_MAX_AGE <= created <= now:
        raise Failure("fresh_pre_change_backup_required")
    if manifest["snapshot"] is None or differences(manifest["snapshot"], before):
        raise Failure("backup_does_not_match_current")
    return before


def apply_fk(db: Database, work: Path, manifest_path: Path, now: datetime) -> dict:
    current = db.snapshot()
    if classify(current) == "current":
        return {"command": "apply-reservation-fk", "result": "already_applied"}
    before = change_gate(db, manifest_path, now)
    if before["orphan_job_reservations"]:
        raise Failure("orphan_reservations")
    db.sql(APPLY_FK, code="apply_failed")
    after = db.snapshot()
    changed = differences(before, after, added=frozenset({FK_NAME}))
    report = {
        "command": "apply-reservation-fk",
        "at": now.isoformat(),
        "backup_manifest": manifest_path.name,
        "before": before,
        "after": after,
        "unexpected_changes": changed,
    }
    path = private_write(work / f"apply-{file_stamp(now)}.json", report)
    if changed:
        raise Failure("unexpected_change_after_apply")
    return {"command": "apply-reservation-fk", "result": "applied", "report": str(path)}


def revert_fk(db: Database, work: Path, manifest_path: Path, now: datetime) -> dict:
    """Data-preserving schema rollback: drop only the added constraint, never tables."""
    before = change_gate(db, manifest_path, now)
    if FK_NAME not in before["constraints"]:
        return {"command": "revert-reservation-fk", "result": "not_present"}
    db.sql(REVERT_FK, code="revert_failed")
    after = db.snapshot()
    changed = differences(before, after, removed=frozenset({FK_NAME}))
    report = {"command": "revert-reservation-fk", "at": now.isoformat(), "before": before, "after": after}
    report["unexpected_changes"] = changed
    path = private_write(work / f"revert-{file_stamp(now)}.json", report)
    if changed:
        raise Failure("unexpected_change_after_revert")
    return {"command": "revert-reservation-fk", "result": "reverted", "report": str(path)}


def restore(db: Database, work: Path, manifest_path: Path, verify_report: Path | None, now: datetime) -> dict:
    """Stage, check, then swap names; the replaced database is kept for the Owner to remove later."""
    require_stopped_backend(db)
    manifest, archive = load_manifest(manifest_path)
    expected = manifest["snapshot"]
    if expected is None:
        if verify_report is None:
            raise Failure("expected_snapshot_required")
        try:
            report = json.loads(verify_report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Failure("verify_report_unreadable") from None
        if report.get("sha256") != manifest["sha256"] or not isinstance(report.get("restored_snapshot"), dict):
            raise Failure("verify_report_mismatch")
        expected = report["restored_snapshot"]
    try:
        before = db.snapshot()
    except Failure:
        before = None  # A damaged live database is a reason to restore, not a blocker.
    require_restore_space(db, manifest)
    retained = "nss_replaced_" + stamp(now).lower()
    if db.database_exists(retained):
        raise Failure("retained_database_exists")
    started = time.monotonic()
    staged_restore(db, STAGING_DB, archive, expected)
    try:
        db.swap(retained)
    except Failure:
        db.drop(STAGING_DB)
        raise
    after = db.snapshot()
    changed = differences(expected, after)
    report = {
        "command": "restore",
        "at": now.isoformat(),
        "backup_manifest": manifest_path.name,
        "before": before,
        "after": after,
        "retained_database": retained,
        "restore_seconds": round(time.monotonic() - started, 2),
        "unexpected_changes": changed,
    }
    path = private_write(work / f"restore-{file_stamp(now)}.json", report)
    if changed:
        raise Failure("restored_snapshot_mismatch")
    return {
        "command": "restore",
        "result": "restored",
        "retained_database": retained,
        "restore_seconds": report["restore_seconds"],
        "report": str(path),
    }


def docker_prefix(context: str | None) -> list[str]:
    return ["docker"] + (["--context", context] if context else [])


def compose_prefix(context, project, files, env_files) -> list[str]:
    command = docker_prefix(context) + ["compose", "--project-name", project]
    for env_file in env_files:
        command += ["--env-file", str(env_file)]
    for file in files:
        command += ["--file", str(file)]
    return command


def build_database(args, diagnostics) -> Database:
    if args.db_container:
        return Database(docker_prefix(args.context) + ["exec", "-i", args.db_container], diagnostics=diagnostics)
    compose = compose_prefix(args.context, args.compose_project, args.compose_file, args.env_file)
    return Database(
        compose + ["exec", "-T", "db"],
        backend_check=compose + ["ps", "--status", "running", "--quiet", "backend"],
        diagnostics=diagnostics,
    )


def work_directory(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise Failure("work_dir_inside_repository")
    if path.is_symlink():
        raise Failure("work_dir_symlink")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    return resolved


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Pilot DB precheck/backup/verify/apply/restore (#75).")
    result.add_argument("--context", help="Docker context; omit to use the host default")
    target = result.add_mutually_exclusive_group(required=True)
    target.add_argument("--compose-project", help="Compose project of the running stack")
    target.add_argument("--db-container", help="A disposable DB container (rehearsal or vault-side checks)")
    result.add_argument("--compose-file", type=Path, action="append", default=[])
    result.add_argument("--env-file", type=Path, action="append", default=[])
    result.add_argument("--work-dir", type=Path, required=True, help="Private directory outside the repository")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("precheck")
    backup_command = commands.add_parser("backup")
    backup_command.add_argument("--purpose", choices=PURPOSES, required=True)
    verify_command = commands.add_parser("verify")
    verify_command.add_argument("manifest", type=Path)
    verify_command.add_argument("--restore-check", action="store_true")
    for name in ("apply-reservation-fk", "revert-reservation-fk"):
        commands.add_parser(name).add_argument("manifest", type=Path)
    restore_command = commands.add_parser("restore")
    restore_command.add_argument("manifest", type=Path)
    restore_command.add_argument("--verify-report", type=Path)
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.compose_project and not args.compose_file:
        parser().error("--compose-file is required with --compose-project")
    os.umask(0o077)
    try:
        work = work_directory(args.work_dir)
        db = build_database(args, work)
        now = utc_now()
        if args.command == "precheck":
            report = precheck(db, work, now)
            result = {"command": "precheck", "classification": report["classification"], "report": report["path"]}
            result["summary"] = summary(report["snapshot"])
        elif args.command == "backup":
            result = backup(db, work, args.purpose, now)
        elif args.command == "verify":
            report = verify(db, work, args.manifest, args.restore_check, now)
            result = {"command": "verify", "result": "verified", "report": report["path"]}
            if args.restore_check:
                result["restore_seconds"] = report["restore_seconds"]
        elif args.command == "apply-reservation-fk":
            result = apply_fk(db, work, args.manifest, now)
        elif args.command == "revert-reservation-fk":
            result = revert_fk(db, work, args.manifest, now)
        else:
            result = restore(db, work, args.manifest, args.verify_report, now)
    except Failure as failure:
        print(f"DB_RECOVERY_FAILED: {failure}", file=sys.stderr)
        return 1
    except Exception:
        # Tracebacks can quote values; nothing beyond a fixed code reaches the console.
        print("DB_RECOVERY_FAILED: unexpected_error", file=sys.stderr)
        return 1
    # ASCII-only output survives any console encoding (non-ASCII paths are escaped).
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
