"""Idempotently seed the running local showcase and register its CDC connector."""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/compose/docker-compose.yml"
SEED = ROOT / "examples/retail-pricing/seed.sql"
CONNECTOR = ROOT / "connectors/postgres-debezium/connector.json"


def _docker() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("docker executable is not available")
    return executable


def _seed_postgres() -> None:
    # Fixed argv and checked-in synthetic SQL; no shell expansion is used.
    subprocess.run(  # nosec B603
        [
            _docker(),
            "compose",
            "-f",
            str(COMPOSE),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "context",
            "-d",
            "context",
        ],
        input=SEED.read_bytes(),
        check=True,
    )


def _register_connector() -> None:
    config = json.loads(CONNECTOR.read_text(encoding="utf-8"))
    body = json.dumps(config).encode()
    request = urllib.request.Request(
        "http://localhost:8083/connectors/agentic-context-retail/config",
        data=body,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    # Fixed localhost Debezium origin and fixed connector identity.
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
        if response.status != HTTPStatus.OK:
            raise RuntimeError(f"Debezium registration returned HTTP {response.status}")


def main() -> int:
    try:
        _seed_postgres()
        _register_connector()
    except (subprocess.CalledProcessError, OSError, urllib.error.URLError, RuntimeError) as exc:
        print(f"Seed failed: {exc}")
        print("Start the stack first with `make up`; no success is being claimed.")
        return 1
    print("Synthetic retail data seeded and Debezium connector registered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
