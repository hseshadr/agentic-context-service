# OpenSearch schema contract

`index-template.json` and `memory-index-template.json` are the canonical v1 mappings. Institutional
and memory content use separate aliases and trust domains:

| Family | Read alias | Write alias |
|---|---|---|
| Institutional | `context-institutional-read` | `context-institutional-write` |
| Memory | `context-memory-read` | `context-memory-write` |

`bootstrap.sh` safely installs both templates and creates initial indices/aliases only when absent.
Mapping or model changes require a new versioned shadow index, source replay, count/hash and
retrieval comparison, and one atomic alias update. Adapters and tests must consume these canonical
names; another prefix-derived family is contract drift.
