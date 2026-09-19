"""CLI composition smoke."""

from __future__ import annotations

from unittest.mock import patch

from agentic_context_service.cli import entrypoint


def test_cli_starts_uvicorn_factory_with_requested_bind() -> None:
    with patch("agentic_context_service.cli.uvicorn.run") as run:
        entrypoint(["serve", "--host", "127.0.0.2", "--port", "9090"])

    run.assert_called_once_with(
        "agentic_context_service.bootstrap:create_app",
        factory=True,
        host="127.0.0.2",
        port=9090,
    )
