"""Test-only authentication data; no accounts or valid tokens are created."""

import json
from pathlib import Path
from typing import Any


def load_auth_cases() -> dict[str, Any]:
    """Load fresh nested data on every call, independently of the working directory."""
    path = Path(__file__).with_name("auth_cases.json")
    return json.loads(path.read_text(encoding="utf-8"))
