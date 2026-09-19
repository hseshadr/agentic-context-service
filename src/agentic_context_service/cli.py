"""Small command-line entry point for local operation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn


def entrypoint(argv: Sequence[str] | None = None) -> None:
    """Run the API using the same composition root as containers."""
    parser = argparse.ArgumentParser(prog="agentic-context")
    parser.add_argument("command", choices=("serve",), nargs="?", default="serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    uvicorn.run(
        "agentic_context_service.bootstrap:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )
