from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
SIGNING_KEY = "ACS_SIGNING_" + "SECRET"


def test_required_oss_files_exist() -> None:
    required = {
        ".env.example",
        "AGENTS.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "docs/architecture/index.html",
        "docs/specification.md",
        "docs/threat-model/README.md",
        "packages/contracts/openapi.yaml",
    }
    missing = sorted(path for path in required if not (ROOT / path).is_file())
    assert not missing, f"missing required OSS artifacts: {missing}"


def test_makefile_is_only_a_thin_poe_wrapper() -> None:
    text = (ROOT / "Makefile").read_text()
    targets = {
        "bootstrap",
        "format",
        "lint",
        "unit",
        "integration",
        "bdd",
        "security",
        "eval",
        "up",
        "seed",
        "demo",
        "down",
        "clean",
        "verify",
    }
    assert targets.issubset(set(re.findall(r"\b[a-z]+", text.splitlines()[0])))
    assert text.count("uv run poe") == 1


def test_example_environment_contains_no_secret() -> None:
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    assert values
    assert all("sk-" not in value and "ghp_" not in value for value in values.values())
    assert values[SIGNING_KEY] == "replace-with-at-least-32-characters"


def test_openapi_has_every_required_endpoint_and_no_raw_dsl() -> None:
    document = yaml.safe_load((ROOT / "packages/contracts/openapi.yaml").read_text())
    required = {
        "/v1/context:retrieve",
        "/v1/context:batchRetrieve",
        "/v1/memories",
        "/v1/memories:search",
        "/v1/memories/{id}",
        "/v1/feedback",
        "/v1/sources/{source}/freshness",
        "/health/live",
        "/health/ready",
    }
    assert required.issubset(document["paths"])
    serialized = str(document).lower()
    assert "opensearch_dsl" not in serialized
    assert "raw_dsl" not in serialized


def test_local_indexer_consumes_every_declared_showcase_cdc_topic() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose/docker-compose.yml").read_text())
    indexer_environment = compose["services"]["indexer"]["environment"]

    assert indexer_environment["ACS_KAFKA_TOPICS"] == (
        "catalog.public.pricing_rules,fulfillment.public.fulfillment_rules"
    )


def test_inward_layers_import_no_vendor_frameworks() -> None:
    forbidden = {
        "fastapi",
        "pydantic",
        "opensearchpy",
        "aiokafka",
        "httpx",
        "langgraph",
        "sentence_transformers",
    }
    files = [
        *ROOT.glob("src/agentic_context_service/domain/**/*.py"),
        *ROOT.glob("src/agentic_context_service/application/**/*.py"),
        *ROOT.glob("src/agentic_context_service/ports/**/*.py"),
    ]
    violations: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text())
        imports = {
            node.names[0].name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        }
        imports.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        if imports & forbidden:
            violations.append(f"{path.relative_to(ROOT)}: {sorted(imports & forbidden)}")
    assert not violations, "inward dependency violations:\n" + "\n".join(violations)


def test_personal_project_has_no_gap_inc_reference() -> None:
    prohibited = "gap" + " inc"
    text_paths = [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and ".venv" not in path.parts
        and path.suffix in {"", ".md", ".py", ".toml", ".yaml", ".yml", ".json", ".rego"}
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in text_paths
        if prohibited in path.read_text(errors="ignore").lower()
    ]
    assert not offenders, f"personal project contains prohibited employer reference: {offenders}"
