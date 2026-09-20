"""Serve the real showcase route with isolated, non-networked dependencies for Playwright."""

from __future__ import annotations

from typing import Any

import uvicorn

from agentic_context_service.api.app import create_app
from agentic_context_service.api.request_context import (
    AuthenticatedPrincipal,
    StaticTokenAuthenticator,
)
from agentic_context_service.application.showcase_source import FulfillmentPromiseShowcaseWriter


class _NoopService:
    async def execute(
        self, operation: str, context: Any, payload: dict[str, Any]
    ) -> dict[str, object]:
        del operation, context, payload
        return {"items": []}

    async def ready(self) -> bool:
        return True


class _BrowserShowcaseStore:
    async def start(self, run_id: str, correlation_id: str) -> int:
        del run_id, correlation_id
        return 1


def main() -> None:
    app = create_app(
        service=_NoopService(),
        signing_secret=b"a sufficiently long isolated showcase browser test secret",
        authenticator=StaticTokenAuthenticator(
            {
                "browser-test": AuthenticatedPrincipal(
                    subject="browser-test",
                    tenant_id="browser-test",
                    teams=("showcase",),
                    entitlements=("pricing-analysis",),
                )
            }
        ),
        showcase_writer=FulfillmentPromiseShowcaseWriter(_BrowserShowcaseStore()),
    )
    uvicorn.run(app, host="127.0.0.1", port=8081)


if __name__ == "__main__":
    main()
