"""Validate public files and prevent adapter/schema naming drift."""

from __future__ import annotations

import json
from pathlib import Path

from openapi_spec_validator import validate_spec
from openapi_spec_validator.readers import read_from_filename

ROOT = Path(__file__).resolve().parents[1]


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _validate_openapi() -> None:
    document, base_uri = read_from_filename(str(ROOT / "packages/contracts/openapi.yaml"))
    validate_spec(document, base_uri=base_uri)


def _validate_json_contracts() -> None:
    for path in sorted((ROOT / "packages/contracts/jsonschema").glob("*.json")):
        document = _json(path)
        if document.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError(f"{path} is not JSON Schema 2020-12")


def _validate_opensearch_contract() -> None:
    institutional = _json(ROOT / "connectors/opensearch/index-template.json")
    memory = _json(ROOT / "connectors/opensearch/memory-index-template.json")
    if institutional.get("index_patterns") != ["context-institutional-v1-*"]:
        raise ValueError("institutional template/adapter version drift")
    if memory.get("index_patterns") != ["context-memory-v1-*"]:
        raise ValueError("memory template/adapter version drift")
    bootstrap = (ROOT / "connectors/opensearch/bootstrap.sh").read_text(encoding="utf-8")
    for alias in (
        "context-institutional-read",
        "context-institutional-write",
        "context-memory-read",
        "context-memory-write",
    ):
        if alias not in bootstrap:
            raise ValueError(f"bootstrap is missing canonical alias {alias}")


def main() -> int:
    _validate_openapi()
    _validate_json_contracts()
    _validate_opensearch_contract()
    print("OpenAPI, JSON Schema, and OpenSearch contracts are aligned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
