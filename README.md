# Agentic Context Service

A lookup service for your company's AI assistants: each one gets only the facts it may see, with the source of each.

**Try it after cloning: `uv sync && uv run python examples/quickstart/governed_retrieval.py`** (no servers, no API key).

[![CI](https://github.com/hseshadr/agentic-context-service/actions/workflows/dagger.yml/badge.svg)](https://github.com/hseshadr/agentic-context-service/actions/workflows/dagger.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Companies that run several AI assistants (programs that answer questions or propose actions
using a language model) usually connect each one to the company's databases on its own. Each
connection re-implements who may see what. When an assistant quotes a price or a policy, nobody
can tell which record it came from or how old it was.

Agentic Context Service is one HTTP service your assistants ask instead. Your databases send it
each change once. When an assistant asks a question, the service first removes everything that
assistant is not allowed to see, then searches what is left, and returns short passages. Each
passage names its source record, version, and age. You run it on your own machines.

**Technical docs:** [Architecture](docs/ARCHITECTURE.md) · [Getting started for developers](docs/GETTING_STARTED.md) · [API contract](packages/contracts/openapi.yaml) · [Threat model](docs/threat-model/README.md)

## Try it

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.13 for you if needed.

1. Clone and install:

   ```bash
   git clone https://github.com/hseshadr/agentic-context-service.git
   cd agentic-context-service
   uv sync
   ```

2. Run the example:

   ```bash
   uv run python examples/quickstart/governed_retrieval.py
   ```

The example stores three records for a made-up retailer: a pricing rule for pricing work, a note
only finance may read, and a rule that belongs to a different company. Then two of the
retailer's assistants ask the same question. This is the real output:

```text
published 3 records: a pricing rule, a finance-only note, another company's rule
pricing-analysis asks "What is the discount limit for NS-100?" -> 1 result(s)
  NS-100 may be discounted at most 20% without pricing-lead approval.
  source: postgres://catalog/NS-100 v7, 90s old, trust: verified
customer-support asks "What is the discount limit for NS-100?" -> 0 result(s)
```

The pricing assistant gets the pricing rule and where it came from: record `NS-100` in the
catalog database, version 7, updated 90 seconds ago. It never sees the finance note or the other
company's rule. The support assistant is not cleared for pricing, so it gets nothing rather than
a guess. The example runs in memory, with no network, in well under a second.

## How it works

Changes in PostgreSQL are picked up from the database's change log (Debezium) and passed through
a queue (Redpanda) to an indexer. The indexer splits each record into chunks and stores them in
OpenSearch with the record's version, so a late or replayed change never overwrites a newer one
and a deleted record stays deleted. An assistant sends one signed request that says which company
it works for and what job it is doing. A policy engine (Open Policy Agent) turns that into
filters that are applied before any ranking, and a caller's own filters can narrow them but never
widen them. OpenSearch then ranks what is left by exact words and by meaning, and the service
returns the top passages with their sources and ages.

## What it does not do

- **It is Beta.** Version 0.1.0 has no tagged release and is not published to PyPI. You install
  it from this repository.
- **It is not a chat app or a framework for building assistants.** It is the lookup your
  assistant calls.
- **It is not the source of truth.** Your databases are. The index is updated a moment after the
  database changes, not in the same transaction.
- **The local stack is for trying it, not for production.** It turns off TLS, private
  networking, and OpenSearch's per-document security so it runs on a laptop. There is no high
  availability, backup and restore, or load testing yet. See the [roadmap](ROADMAP.md).
- **The quickstart matches exact words only.** Search by meaning needs the full stack below.
- **It cannot protect you from a compromised server or operator**, or from what an AI provider
  does with data you choose to send it. Nothing leaves your machines unless you turn on the
  optional live AI mode (`ACS_SHOWCASE_AGENT_MODE=deep`) in the demo.

## When to use something else

| If you have | Use |
| --- | --- |
| One assistant and one data source | Let the assistant query that source directly |
| Documents only, and no per-user or per-team access rules | A vector database with a retrieval library |
| A need for a managed service, and you accept its data handling | A hosted enterprise search product |
| Several assistants, access rules, and a need to trace every answer to a record | Agentic Context Service |

## Run the full local stack

This runs PostgreSQL, Debezium, Redpanda, OpenSearch, OPA, the API, and the indexer in Docker.
You need Docker with Compose v2 and `make`.

```bash
cp .env.example .env
make bootstrap
make up
make seed
make demo
```

`make demo` walks through a pricing question with citations, a database update showing up in
search, an assistant being refused, and saved assistant memory. It exits with an error if any
step does not behave as expected. While the stack runs, the dashboard is at
http://localhost:8080/showcase/. Stop it with `make down`. Details are in
[operations](docs/operations.md).

## Develop

```bash
make bootstrap
make verify
```

`make verify` runs the same checks as CI: lint, strict type checks, tests with at least 90%
coverage, security scans, and a retrieval-quality evaluation. It takes about a minute. Run it
without a `.env` file in the folder. [Getting started for developers](docs/GETTING_STARTED.md)
covers prerequisites, a map of the code, and a walkthrough of a first change.

## More detail

- [Architecture](docs/ARCHITECTURE.md): the data path, API shape, security model, configuration,
  and what the tests prove.
- [Getting started for developers](docs/GETTING_STARTED.md): from clone to a green build and your
  first change.
- [Five-minute architecture](docs/architecture/README.md): the design rules on one page.
- [Interactive architecture map](docs/architecture/index.html): a guided visual tour.
- [Specification](docs/specification.md): the full engineering contract and acceptance criteria.
- [Operations](docs/operations.md): health checks and what to do when something breaks.
- [Threat model](docs/threat-model/README.md): what is protected and what is not.
- [Design decisions](docs/adr/): one short record per decision.
- [API contract](packages/contracts/openapi.yaml): the OpenAPI definition.
- [Examples](examples/): the quickstart and two AI agent examples where code, not the model,
  approves the action.
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md),
  and [ROADMAP.md](ROADMAP.md).

## License

MIT. See [LICENSE](LICENSE).
