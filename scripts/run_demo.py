"""Exercise the complete local showcase through public boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

from agentic_context_service.api.request_context import RequestContextSigner

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/compose/docker-compose.yml"
API = "http://localhost:8080"
DEMO_SUBJECT = "demo-analyst"
TARGET_RECORD_ID = "NORTHSTAR-104"
MEMORY_TEXT = "Prefer margin deltas as percentages."
HEADERS = {
    "Authorization": "Bearer demo-retail-token",
    "Content-Type": "application/json",
    "X-Team-ID": "pricing",
    "X-App-ID": "demo-agent",
    "X-Workflow-ID": "pricing-analysis",
    "X-Workflow-Revision": "v1",
    "X-Agent-ID": "demo",
    "X-Environment": "development",
    "X-Cost-Center": "oss",
    "X-Request-ID": "demo-request",
    "X-Trace-ID": "0123456789abcdef0123456789abcdef",
}


def _docker() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("docker executable is not available")
    return executable


def _signed_headers() -> dict[str, str]:
    headers = {**HEADERS, "X-ACS-Timestamp": str(int(time.time()))}
    secret = os.environ.get("ACS_SIGNING_SECRET", "local-showcase-only-change-this-key").encode()
    headers["X-ACS-Signature"] = RequestContextSigner(secret).sign(headers)
    return headers


def _request(
    path: str, payload: dict[str, object], headers: dict[str, str] | None = None
) -> dict[str, object]:
    request = urllib.request.Request(  # noqa: S310 - URL is a fixed local demo origin.
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers=headers or _signed_headers(),
        method="POST",
    )
    # Fixed localhost origin; callers can vary only the owned relative API path.
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
        decoded = json.loads(response.read())
    if not isinstance(decoded, dict):
        raise RuntimeError("API returned a non-object response")
    return decoded


def _retrieval() -> dict[str, object]:
    return {
        "query": "What is the promotional floor for NORTHSTAR-104?",
        "corpora": ["pricing", "product", "approved-decisions"],
        "filters": {
            "brand": ["NORTHSTAR"],
            "market": ["US"],
            "record_id": [TARGET_RECORD_ID],
            "entity_type": ["pricing_rule"],
            "source": ["catalog"],
        },
        "retrieval": {
            "mode": "hybrid",
            "candidate_limit": 100,
            "result_limit": 12,
            "rerank": False,
            "max_context_tokens": 6000,
            "max_age_seconds": 300,
        },
        "purpose": "pricing-analysis",
        "session_id": "sess_demo",
    }


def _update_source() -> str:
    sql = (
        "UPDATE pricing_rules SET content='SKU NORTHSTAR-104 has a promotional price floor "
        "of 18 percent below list price.', source_version=source_version + 1, updated_at=now() "
        "WHERE sku='NORTHSTAR-104' RETURNING source_version;"
    )
    # Fixed argv and SQL over synthetic local data; no shell expansion is used.
    result = subprocess.run(  # nosec B603
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
            "-qAt",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    version = result.stdout.strip()
    if not version.isdigit():
        raise RuntimeError("PostgreSQL did not return a numeric source version")
    return version


def _await_version(version: str) -> dict[str, object]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = _request("/v1/context:retrieve", _retrieval())
        if _has_citation(response, TARGET_RECORD_ID, version):
            return response
        time.sleep(1)
    raise TimeoutError(f"CDC source version {version} was not visible within 60 seconds")


def _has_citation(response: dict[str, object], record_id: str, version: str) -> bool:
    results = response.get("results")
    if not isinstance(results, list):
        return False
    for result in results:
        if isinstance(result, dict) and _citation_matches(
            result.get("citation"), record_id, version
        ):
            return True
    return False


def _has_initial_context(response: dict[str, object]) -> bool:
    results = response.get("results")
    if not isinstance(results, list):
        return False
    return any(_valid_initial_result(result) for result in results)


def _await_initial_context() -> dict[str, object]:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = _request("/v1/context:retrieve", _retrieval())
        if _has_initial_context(response):
            return response
        time.sleep(1)
    raise TimeoutError("initial hybrid retrieval lacked cited, ranked fresh context")


def _valid_initial_result(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    return all(
        (
            _target_citation(result.get("citation")),
            _typed_pricing_fact(result),
            _freshness_present(result.get("freshness")),
            _rank_present(result.get("component_ranks")),
        )
    )


def _target_citation(candidate: object) -> bool:
    return (
        isinstance(candidate, dict)
        and candidate.get("record_id") == TARGET_RECORD_ID
        and bool(candidate.get("source_uri"))
        and bool(candidate.get("source_version"))
    )


def _typed_pricing_fact(candidate: object) -> bool:
    if not isinstance(candidate, dict):
        return False
    citation = candidate.get("citation")
    facts = candidate.get("source_facts")
    return _catalog_citation(citation) and _valid_pricing_facts(facts, citation)


def _catalog_citation(value: object) -> bool:
    return isinstance(value, dict) and value.get("source_system") == "catalog"


def _valid_pricing_facts(facts: object, citation: object) -> bool:
    if not isinstance(facts, dict) or not isinstance(citation, dict):
        return False
    return _fact_identity_matches(facts) and _fact_version_matches(facts, citation)


def _fact_identity_matches(facts: dict[object, object]) -> bool:
    return (
        facts.get("kind"),
        facts.get("sku"),
        isinstance(facts.get("max_discount_percent"), int),
    ) == ("retail_pricing_rule.v1", TARGET_RECORD_ID, True)


def _fact_version_matches(facts: dict[object, object], citation: dict[object, object]) -> bool:
    return str(facts.get("source_version")) == str(citation.get("source_version"))


def _freshness_present(candidate: object) -> bool:
    return isinstance(candidate, dict) and isinstance(candidate.get("age_seconds"), int | float)


def _rank_present(candidate: object) -> bool:
    return isinstance(candidate, dict) and any(
        candidate.get(key) is not None for key in ("lexical", "semantic")
    )


def _citation_matches(candidate: object, record_id: str, version: str) -> bool:
    return (
        isinstance(candidate, dict)
        and candidate.get("record_id") == record_id
        and str(candidate.get("source_version")) == version
    )


def _deny_restricted() -> str:
    denied = _retrieval() | {"corpora": ["restricted-payroll"]}
    try:
        response = _request("/v1/context:retrieve", denied)
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.FORBIDDEN:
            return "explicitly denied"
        raise
    if "NORTHSTAR-SECRET" in json.dumps(response):
        raise RuntimeError("restricted candidate leaked into the filtered response")
    items = response.get("items", response.get("results", []))
    if items != []:
        raise RuntimeError("restricted-corpus request returned an unexpected candidate")
    return "filtered to an empty candidate set"


def _memory_namespace() -> dict[str, object]:
    return {
        "environment": "development",
        "workflow_id": "pricing-analysis",
        "workflow_revision": "v1",
        "user_id": DEMO_SUBJECT,
        "session_id": "sess_demo",
        "agent_id": "demo",
    }


def _memory() -> dict[str, object]:
    return _request(
        "/v1/memories",
        {
            "namespace": _memory_namespace(),
            "memory_type": "working",
            "text": MEMORY_TEXT,
            "source_evidence": [],
            "expires_at": "2026-09-20T00:00:00Z",
        },
    )


def _await_memory(memory_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = _request(
            "/v1/memories:search",
            {
                "query": MEMORY_TEXT,
                "memory_types": ["working"],
                "namespace": _memory_namespace(),
                "result_limit": 10,
            },
        )
        if _has_memory(response, memory_id):
            return response
        time.sleep(1)
    raise TimeoutError(f"created memory {memory_id} was not searchable within 30 seconds")


def _has_memory(response: dict[str, object], memory_id: str) -> bool:
    items = response.get("items")
    if not isinstance(items, list):
        return False
    return any(_valid_memory_item(item, memory_id) for item in items)


def _valid_memory_item(item: object, memory_id: str) -> bool:
    if not isinstance(item, dict) or item.get("document_id") != memory_id:
        return False
    source = item.get("source")
    content = source.get("content") if isinstance(source, dict) else None
    return isinstance(content, dict) and _untrusted_agent_memory(content)


def _untrusted_agent_memory(content: dict[object, object]) -> bool:
    return all(
        (
            content.get("origin") == "agent_derived",
            content.get("trust_class") == "untrusted",
            content.get("proposed") is True,
        )
    )


def main() -> int:
    try:
        _run_showcase()
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        subprocess.CalledProcessError,
        urllib.error.URLError,
    ) as exc:
        print(f"Demo failed: {exc}")
        return 1
    print("All eight showcase steps passed.")
    return 0


def _run_showcase() -> None:
    print("1/8 Stack: checking the public Context API")
    first = _await_initial_context()
    print(f"2/8 Hybrid cited context: {json.dumps(first, sort_keys=True)}")
    print("3/8 Source change: updating PostgreSQL only")
    print(f"4/8 CDC visibility: {json.dumps(_await_version(_update_source()), sort_keys=True)}")
    print(f"5/8 Authorization: restricted corpus {_deny_restricted()} before ranking")
    _create_and_verify_memory()
    print("7/8 Evidence: inspect Prometheus at http://localhost:9090 and collector logs")
    print("8/8 Evaluation: run `make eval` for the versioned report")


def _create_and_verify_memory() -> None:
    memory = _memory()
    memory_id = memory.get("id")
    if memory.get("status") != "created" or not isinstance(memory_id, str) or not memory_id:
        raise RuntimeError("memory creation did not return a created receipt")
    _await_memory(memory_id)
    print("6/8 Memory: created and retrieved as proposed, agent-derived, untrusted context")


if __name__ == "__main__":
    raise SystemExit(main())
