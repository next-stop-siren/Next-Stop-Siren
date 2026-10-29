"""Validate two external env files without evaluating or printing their values."""

import re
import stat
import sys
from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise ValueError("invalid_env")
        # Plain dotenv only: no shell evaluation, interpolation or multiline secrets.
        if any(character.isspace() for character in value) or "$" in value or "`" in value:
            raise ValueError("invalid_env")
        values[name] = value
    return values


def main() -> int:
    try:
        if len(sys.argv) != 3:
            raise ValueError("arguments")
        release_path, runtime_path = map(Path, sys.argv[1:])
        release, runtime = read_env(release_path), read_env(runtime_path)
        if stat.S_IMODE(runtime_path.stat().st_mode) & 0o077:
            raise ValueError("permissions")
        if release.get("RUNTIME_ENV_FILE") != str(runtime_path.resolve()):
            raise ValueError("env_path")
        for name in ("FRONTEND_IMAGE", "BACKEND_IMAGE", "CERTBOT_IMAGE"):
            if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", release.get(name, "")):
                raise ValueError("image_digest")
        if not all(runtime.get(name) for name in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")):
            raise ValueError("database")
    except (OSError, ValueError):
        print(
            "Release rejected: check env syntax, permissions, paths and image digests (values hidden).", file=sys.stderr
        )
        return 2
    print("Release file shape valid; backend --validate-only and functional checks are still required.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
