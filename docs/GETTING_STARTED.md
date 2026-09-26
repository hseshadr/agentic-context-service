# Getting started for developers

This takes you from a fresh clone to a green local build and your first change. Every command
here was run on macOS (Apple silicon) from a fresh clone on 2026-09-25; times are from that run.

## 1. Prerequisites

| Tool | Version | How to get it |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | 0.8 or later | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Python | 3.13 exactly (`>=3.13,<3.14`) | You do not need to install it. `uv` downloads 3.13 on first sync if your system Python is different. |
| `make` | any | Preinstalled on macOS and most Linux systems. Every target is a thin wrapper around `uv run poe <task>`. |
| Node.js + npm | Node 22 or later | Only for the browser test (`make ui`). |
| Docker with Compose v2 | recent | Only for the full local stack (`make up`). |
| [Dagger](https://docs.dagger.io/) | 0.21.8 | Optional. Only to run the exact CI container locally. |

Local traps we actually hit:

- **Plain `uv sync` is not enough for the full check.** It skips the optional `agent` extra, and
  one unit test then fails with `ModuleNotFoundError: No module named 'pydantic_ai'`. Use
  `make bootstrap` (which runs `uv sync --all-extras --all-groups`).
- **A `.env` file in the repo root breaks one test.** Settings are read from `.env`, so
  `test_factory_refuses_unconfigured_bearer_authentication` sees `ACS_DEMO_TOKEN` and fails with
  `DID NOT RAISE ValueError`, and coverage then drops just under 90%. Run `make verify` without a
  `.env` (rename it while you check), and only create one when you run the Docker stack.
- **Only one copy of the Docker stack can run at a time.** The Compose project is named after the
  folder and binds fixed ports (8080, 5432, 9200, 19092 and others). If another checkout's stack is
  up, stop it first with `make down` in that checkout.

## 2. Clone, install, and run it

```bash
git clone https://github.com/hseshadr/agentic-context-service.git
cd agentic-context-service
make bootstrap
uv run python examples/quickstart/governed_retrieval.py
```

`make bootstrap` took about 2 seconds with a warm `uv` cache; a first run downloads Python 3.13
and the packages and takes a few minutes. Success looks like this:

```text
published 3 records: a pricing rule, a finance-only note, another company's rule
pricing-analysis asks "What is the discount limit for NS-100?" -> 1 result(s)
  NS-100 may be discounted at most 20% without pricing-lead approval.
  source: postgres://catalog/NS-100 v7, 90s old, trust: verified
customer-support asks "What is the discount limit for NS-100?" -> 0 result(s)
```

The example needs no Docker, no API key, and no network.

## 3. The full check (the same thing CI runs)

```bash
make verify
```

CI runs this same task (`uv run poe verify`) inside a Dagger container. It runs, in order:
formatting and lint (Ruff), strict type checks (mypy), a complexity limit (xenon, grade A),
unit and contract tests with at least 90% line and branch coverage, integration tests, BDD
scenarios, security scans (Bandit and pip-audit), and the offline retrieval evaluation.

It took 39 to 57 seconds across our runs. The test lines in a passing run look like this:

```text
====================== 196 passed, 3 deselected in 8.23s =======================
...
Evaluation PASS: 3 queries -> .../reports/evaluation.json
```

The browser test is a separate check (CI runs it as its own workflow):

```bash
make ui-install   # once: npm ci + Playwright's Chromium
make ui
```

Both together took 15 seconds with the browser already downloaded, ending in `1 passed`.

## 4. Map of the code

| Path | What it is |
| --- | --- |
| `src/agentic_context_service/domain/models.py` | The core data types: documents, change events, queries, results, citations. No vendor imports. |
| `src/agentic_context_service/application/` | The behavior: `retrieval.py` (permission filtering, hybrid ranking, freshness), `ingestion.py` (applying change events), `memory.py` (assistant memory). |
| `src/agentic_context_service/ports/` | Small `Protocol` interfaces the application depends on. |
| `src/agentic_context_service/adapters/` | Real implementations: OpenSearch, OPA, embeddings, telemetry. |
| `src/agentic_context_service/api/` | The FastAPI routes (`app.py`) and the browser showcase. |
| `src/agentic_context_service/bootstrap.py`, `indexer.py` | Where the API and the indexer are wired together from adapters. |
| `src/agentic_context_service/testing/` | In-memory store and fixed clock used by tests and the quickstart. |
| `policies/` | The OPA policy that decides what each caller may see. |
| `deploy/compose/`, `connectors/` | The local Docker stack and the Debezium/OpenSearch configuration. |
| `tests/` | `unit/`, `integration/`, `contract/`, `security/`, `repository/`, `bdd/` (with `features/`), and `e2e/` (Playwright). |

## 5. Make your first change

A typical small change is a retrieval rule. Today a record whose age is exactly the caller's
`max_age_seconds` still counts as fresh (`age > max_age_seconds` drops it only after that).
Say you want the limit to be exclusive instead.

1. Create a branch: `git checkout -b fix/exclusive-freshness-limit`.
2. Write the failing test first, in `tests/unit/test_retrieval.py`. The file has a `doc(...)`
   helper that builds a document of a given `age_seconds`, a `seed(...)` helper that stores it, and
   a `query(...)` helper that builds the question.
   Follow `test_retriever_enforces_tenant_corpus_purpose_filters_and_freshness`: seed a document
   with `age_seconds=300`, query with `max_age_seconds=300`, and expect no result.
3. Run just that file and watch the new test fail:

   ```bash
   uv run pytest tests/unit/test_retrieval.py -q
   ```

   It runs 6 tests in under a second.
4. Change the rule in `src/agentic_context_service/application/retrieval.py`. Freshness is
   decided in `_too_old`, and the other visibility rules are in `_eligible` just above it.
5. Run the file again until it passes, then run `make verify` before you push.

If the change touches the API contract, also update
[`packages/contracts/openapi.yaml`](../packages/contracts/openapi.yaml). If it changes a
significant design decision, add an ADR under [`docs/adr/`](adr/).

## 6. Open a pull request

- **Branch names** follow the commit type: `feat/...`, `fix/...`, `docs/...`, `ci/...`,
  `chore/...`. Commit messages use the same prefixes (`fix: require deep agent tool calls ...`).
- **CI checks** on every pull request: the Dagger workflow (it checks the exact commit, runs
  `python -m scripts.validate_contracts`, then `make verify` in a container), the Playwright
  browser test, and a gitleaks secret scan.
- **Reviewers look for** a test that failed before the change, no drop below 90% coverage, thin
  API routes with logic in `application/`, no new way for a caller's filters to widen what policy
  allows, and no tokens, secrets, or raw sensitive queries in logs. Use synthetic data only.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the short version, and the
[specification](specification.md) for the full engineering contract.
