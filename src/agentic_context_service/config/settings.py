"""Validated environment configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _missing_signing_secret() -> SecretStr:
    raise ValueError("ACS_SIGNING_SECRET is required")


class Settings(BaseSettings):
    """All process configuration, loaded from ACS_* variables."""

    model_config = SettingsConfigDict(
        env_prefix="ACS_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    opensearch_url: str = "http://localhost:9200"
    opa_url: str = "http://localhost:8181"
    kafka_bootstrap_servers: str = "localhost:19092"
    kafka_topics: str = "catalog.public.pricing_rules,fulfillment.public.fulfillment_rules"
    kafka_group_id: str = "agentic-context-indexer-v1"
    kafka_dlq_topic: str = "context.dlq"
    showcase_events_topic: str = "context.showcase.events"
    showcase_kafka_group_id: str = "agentic-context-showcase-v1"
    showcase_database_dsn: SecretStr | None = None
    embedding_model: str = "all-MiniLM-L6-v2"
    signing_secret: SecretStr = Field(default_factory=_missing_signing_secret, min_length=32)
    demo_token: SecretStr | None = None
    demo_subject: str = "demo-analyst"
    demo_tenant_id: str = "demo-retail"
    demo_team_id: str = "pricing"
    demo_entitlements: str = (
        "pricing-analysis,fulfillment-analysis,customer-support,memory-management"
    )
    opensearch_index_prefix: str = Field(default="context", pattern=r"^[a-z0-9-]+$")
    request_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @property
    def cdc_topics(self) -> tuple[str, ...]:
        """Return the explicit, non-empty connector topic allowlist."""
        topics = tuple(topic.strip() for topic in self.kafka_topics.split(",") if topic.strip())
        if not topics:
            raise ValueError("ACS_KAFKA_TOPICS must contain at least one topic")
        return topics
