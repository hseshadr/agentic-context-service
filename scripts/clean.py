"""Remove generated local artifacts without touching source or named data volumes."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = (
    ".artifacts",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "build",
    "dist",
)
FILES = (".coverage", "coverage.xml")


def main() -> int:
    for name in DIRECTORIES:
        shutil.rmtree(ROOT / name, ignore_errors=True)
    for name in FILES:
        (ROOT / name).unlink(missing_ok=True)
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    print("Removed generated local artifacts; source, reports, and Compose volumes were preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
