"""Remove only known generated backend caches within this project."""

from pathlib import Path
from shutil import rmtree

backend = Path(__file__).resolve().parents[1]
for relative_path in ("app/__pycache__", "tests/__pycache__", ".pytest_cache", ".ruff_cache"):
    cache = backend / relative_path
    if cache.is_dir():
        rmtree(cache)
        print(f"removed {relative_path}")
