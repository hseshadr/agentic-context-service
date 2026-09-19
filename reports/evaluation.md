# Retrieval evaluation

Status: **PASS**

This is the reproducible **offline fixture evaluation**. It measures the deterministic golden
corpus and does not claim live OpenSearch, CDC, or model latency. Run the container-backed suite
and ten-minute demo for live-system evidence.

| Metric | Measured |
|---|---:|
| recall_at_k | 1.0 |
| ndcg_at_k | 1.0 |
| mrr | 1.0 |
| citation_completeness | 1.0 |
| unauthorized_result_count | 0 |
| freshness_pass_rate | 1.0 |
| p50_latency_ms | 46.0 |
| p95_latency_ms | 53.2 |
| lexical_contribution_count | 3 |
| semantic_contribution_count | 4 |
| estimated_model_cost_usd | 0.0 |

- Corpus: `examples/retail-pricing/evaluation-corpus.json`
- Corpus version: `evaluation-corpus.v1`
- Generated deterministically from checked-in fixtures (the timestamp is fixed by the corpus).
