"""Sole production composition root for the API process."""

from __future__ import annotations

import os

from fastapi import FastAPI
from opensearchpy import AsyncOpenSearch

from agentic_context_service.adapters.embedding import SentenceTransformerEmbedder
from agentic_context_service.adapters.observability import configure_otlp
from agentic_context_service.adapters.opa import OPAClient
from agentic_context_service.adapters.opensearch import OpenSearchContextStore
from agentic_context_service.adapters.service import GovernedContextService
from agentic_context_service.api.app import create_app as create_http_app
from agentic_context_service.api.request_context import (
    AuthenticatedPrincipal,
    StaticTokenAuthenticator,
)
from agentic_context_service.config import Settings


def create_app() -> FastAPI:
    """Build a zero-argument Uvicorn factory from validated environment settings."""
    settings = Settings()
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        configure_otlp(otlp_endpoint)
    token = settings.demo_token
    if token is None:
        raise ValueError("ACS_DEMO_TOKEN is required until an external authenticator is configured")
    client = AsyncOpenSearch(hosts=[settings.opensearch_url])
    store = OpenSearchContextStore(
        client,
        index_prefix=settings.opensearch_index_prefix,
        embedder=SentenceTransformerEmbedder(settings.embedding_model),
    )
    opa = OPAClient(settings.opa_url, timeout=settings.request_timeout_seconds)
    service = GovernedContextService(
        opa=opa,
        store=store,
        embedding_model=settings.embedding_model,
    )
    principal = AuthenticatedPrincipal(
        subject=settings.demo_subject,
        tenant_id=settings.demo_tenant_id,
        teams=(settings.demo_team_id,),
        entitlements=tuple(_csv(settings.demo_entitlements)),
    )
    app = create_http_app(
        service=service,
        signing_secret=settings.signing_secret.get_secret_value().encode(),
        authenticator=StaticTokenAuthenticator({token.get_secret_value(): principal}),
    )
    app.router.add_event_handler("startup", store.ensure_schema)
    app.router.add_event_handler("shutdown", client.close)
    return app


def _csv(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError("ACS_DEMO_ENTITLEMENTS must contain at least one value")
    return items
