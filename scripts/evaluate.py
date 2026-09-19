"""Generate deterministic, versioned retrieval evidence from the checked-in corpus."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "examples/retail-pricing/evaluation-corpus.json"
JSON_REPORT = ROOT / "reports/evaluation.json"
MARKDOWN_REPORT = ROOT / "reports/evaluation.md"


class ReturnedItem(BaseModel):
    """One deterministic retrieval result used by the offline evaluator."""

    model_config = ConfigDict(extra="forbid")
    id: str
    lexical_rank: int | None
    semantic_rank: int | None
    citation_complete: bool
    authorized: bool
    fresh: bool


class QueryCase(BaseModel):
    """A golden query and its recorded deterministic result."""

    model_config = ConfigDict(extra="forbid")
    id: str
    query: str
    expected_ids: list[str]
    returned: list[ReturnedItem]
    latency_ms: float = Field(ge=0)


class Corpus(BaseModel):
    """Versioned evaluation inputs and release thresholds."""

    model_config = ConfigDict(extra="forbid")
    schema_version: str
    configuration: dict[str, str | int]
    thresholds: dict[str, float | int]
    queries: list[QueryCase]


def _load_corpus() -> Corpus:
    return Corpus.model_validate_json(CORPUS_PATH.read_text(encoding="utf-8"))


def _recall(case: QueryCase, k: int) -> float:
    actual = {item.id for item in case.returned[:k]}
    return len(actual.intersection(case.expected_ids)) / len(case.expected_ids)


def _dcg(case: QueryCase, k: int) -> float:
    expected = set(case.expected_ids)
    return sum(
        1 / math.log2(rank + 2)
        for rank, item in enumerate(case.returned[:k])
        if item.id in expected
    )


def _ndcg(case: QueryCase, k: int) -> float:
    ideal = sum(1 / math.log2(rank + 2) for rank in range(min(len(case.expected_ids), k)))
    return _dcg(case, k) / ideal


def _reciprocal_rank(case: QueryCase) -> float:
    expected = set(case.expected_ids)
    rank = next((rank for rank, item in enumerate(case.returned, 1) if item.id in expected), 0)
    return 0.0 if rank == 0 else 1 / rank


def _percentile(values: list[float], proportion: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * proportion
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _ranking_metrics(corpus: Corpus, k: int) -> dict[str, float]:
    return {
        "recall_at_k": _mean([_recall(case, k) for case in corpus.queries]),
        "ndcg_at_k": _mean([_ndcg(case, k) for case in corpus.queries]),
        "mrr": _mean([_reciprocal_rank(case) for case in corpus.queries]),
    }


def _evidence_metrics(items: list[ReturnedItem]) -> dict[str, float | int]:
    return {
        "citation_completeness": _mean([float(item.citation_complete) for item in items]),
        "unauthorized_result_count": sum(not item.authorized for item in items),
        "freshness_pass_rate": _mean([float(item.fresh) for item in items]),
    }


def _contribution_metrics(items: list[ReturnedItem]) -> dict[str, float | int]:
    return {
        "lexical_contribution_count": sum(item.lexical_rank is not None for item in items),
        "semantic_contribution_count": sum(item.semantic_rank is not None for item in items),
        "estimated_model_cost_usd": 0.0,
    }


def _metrics(corpus: Corpus) -> dict[str, float | int]:
    k = int(corpus.configuration["k"])
    returned = [item for case in corpus.queries for item in case.returned]
    latencies = [case.latency_ms for case in corpus.queries]
    return {
        **_ranking_metrics(corpus, k),
        **_evidence_metrics(returned),
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        **_contribution_metrics(returned),
    }


def _failures(metrics: dict[str, float | int], thresholds: dict[str, float | int]) -> list[str]:
    minimums = ("recall_at_k", "ndcg_at_k", "mrr", "citation_completeness", "freshness_pass_rate")
    failed = [name for name in minimums if float(metrics[name]) < float(thresholds[name])]
    if int(metrics["unauthorized_result_count"]) > int(thresholds["unauthorized_result_count"]):
        failed.append("unauthorized_result_count")
    if float(metrics["p95_latency_ms"]) > float(thresholds["p95_latency_ms"]):
        failed.append("p95_latency_ms")
    return failed


def _markdown(report: dict[str, object]) -> str:
    metrics = report["metrics"]
    if not isinstance(metrics, dict):
        raise TypeError("report metrics must be a mapping")
    rows = "\n".join(f"| {key} | {value} |" for key, value in metrics.items())
    return f"""# Retrieval evaluation

Status: **{report["status"]}**

This is the reproducible **offline fixture evaluation**. It measures the deterministic golden
corpus and does not claim live OpenSearch, CDC, or model latency. Run the container-backed suite
and ten-minute demo for live-system evidence.

| Metric | Measured |
|---|---:|
{rows}

- Corpus: `examples/retail-pricing/evaluation-corpus.json`
- Corpus version: `{report["schema_version"]}`
- Generated deterministically from checked-in fixtures (the timestamp is fixed by the corpus).
"""


def main() -> int:
    corpus = _load_corpus()
    metrics = _metrics(corpus)
    failures = _failures(metrics, corpus.thresholds)
    report: dict[str, object] = {
        "schema_version": corpus.schema_version,
        "generated_at": datetime(2026, 9, 19, 12, 0, tzinfo=UTC).isoformat(),
        "mode": "offline-versioned-fixtures",
        "configuration": corpus.configuration,
        "thresholds": corpus.thresholds,
        "metrics": metrics,
        "failed_thresholds": failures,
        "status": "PASS" if not failures else "FAIL",
    }
    JSON_REPORT.parent.mkdir(parents=True, exist_ok=True)
    JSON_REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    MARKDOWN_REPORT.write_text(_markdown(report), encoding="utf-8")
    print(f"Evaluation {report['status']}: {len(corpus.queries)} queries -> {JSON_REPORT}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
