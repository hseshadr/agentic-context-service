"""Production composition root checks."""

from __future__ import annotations

import pytest

from agentic_context_service.bootstrap import create_app


def test_zero_argument_factory_wires_the_complete_http_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACS_SIGNING_SECRET", "x" * 32)
    monkeypatch.setenv("ACS_DEMO_TOKEN", "local-demo-token")
    monkeypatch.setenv("ACS_DEMO_TENANT_ID", "demo-retail")
    monkeypatch.setenv("ACS_DEMO_ENTITLEMENTS", "pricing-analysis,customer-support")

    app = create_app()

    assert app.title == "Agentic Context Service"
    assert "/v1/context:retrieve" in app.openapi()["paths"]


def test_factory_refuses_unconfigured_bearer_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACS_SIGNING_SECRET", "x" * 32)
    monkeypatch.delenv("ACS_DEMO_TOKEN", raising=False)

    with pytest.raises(ValueError, match="ACS_DEMO_TOKEN"):
        create_app()
