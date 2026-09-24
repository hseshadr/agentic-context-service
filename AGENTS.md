# Repository engineering contract

Follow the executable contract in `docs/specification.md`. Preserve unrelated edits and never
publish, push, create cloud resources, or use real customer data without explicit authorization.

- Use Python 3.13, `uv`, Poe, strict typing, Ruff, pytest, Bandit, and pip-audit.
- Use red-green-refactor for behavior and BDD for visible product scenarios.
- Keep domain/application inward-facing and vendor-free. Use narrow Protocol ports and constructor
  injection. Compose only at process entrypoints.
- Keep FastAPI routes thin. Never expose OpenSearch DSL or allow filters to widen OPA constraints.
- Fail closed on policy uncertainty. Do not log bearer tokens, signatures, secrets, or raw
  sensitive queries.
- Make writes deterministic, idempotent, externally versioned, and checkpointed only after durable
  success. Exercise replay, ordering, deletion, and isolation directly.
- Maintain at least 90% line and branch coverage for non-generated Python. Treat warnings as
  failures unless a focused ADR records the exception.
- Run the complete canonical gate before completion. Report AC-01 through AC-20 honestly as PASS,
  FAIL, or BLOCKED with exact evidence; never imply the fixture evaluator proves live CDC.
