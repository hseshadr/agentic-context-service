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
from agentic_context_service.api.showcase import KafkaShowcaseConsumer, ShowcaseRegistry
from agentic_context_service.application.showcase_source import (
    FulfillmentPromiseShowcaseWriter,
    PostgresFulfillmentShowcaseStore,
)
from agentic_context_service.application.showcase_workflow import (
    FulfillmentShowcaseProcessor,
    PydanticDeepProposalProvider,
    ShowcaseWorkflowIdentity,
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
    showcase_writer = None
    if settings.showcase_database_dsn is not None:
        showcase_writer = FulfillmentPromiseShowcaseWriter(
            PostgresFulfillmentShowcaseStore(
                settings.showcase_database_dsn.get_secret_value(),
            )
        )
    showcase_registry = ShowcaseRegistry(
        FulfillmentShowcaseProcessor(
            service,
            ShowcaseWorkflowIdentity(
                subject=settings.demo_subject,
                tenant_id=settings.demo_tenant_id,
                teams=(settings.demo_team_id,),
                entitlements=tuple(_csv(settings.demo_entitlements)),
                team_id=settings.demo_team_id,
                environment=settings.environment,
                cost_center="oss",
            ),
            proposer=_showcase_proposer(settings),
        )
    )
    app = create_http_app(
        service=service,
        signing_secret=settings.signing_secret.get_secret_value().encode(),
        authenticator=StaticTokenAuthenticator({token.get_secret_value(): principal}),
        showcase_registry=showcase_registry,
        showcase_writer=showcase_writer,
    )
    showcase_consumer = KafkaShowcaseConsumer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        topic=settings.showcase_events_topic,
        group_id=settings.showcase_kafka_group_id,
        registry=showcase_registry,
    )
    app.router.add_event_handler("startup", store.ensure_schema)
    app.router.add_event_handler("startup", showcase_consumer.start)
    app.router.add_event_handler("shutdown", showcase_consumer.stop)
    app.router.add_event_handler("shutdown", client.close)
    return app


def _csv(value: str) -> tuple[str, ...]:
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError("ACS_DEMO_ENTITLEMENTS must contain at least one value")
    return items


def _showcase_proposer(settings: Settings) -> PydanticDeepProposalProvider | None:
    if settings.showcase_agent_mode == "deterministic":
        return None
    if settings.agent_model is None or settings.openrouter_api_key is None:
        raise ValueError(
            "ACS_AGENT_MODEL and OPENROUTER_API_KEY are required for deep showcase mode"
        )
    return PydanticDeepProposalProvider(settings.agent_model, settings.openrouter_api_key)
