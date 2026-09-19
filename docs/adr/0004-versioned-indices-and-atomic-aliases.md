# ADR 0004: Versioned indices and atomic aliases

Status: accepted

Physical names encode family and schema version. Applications resolve only logical read/write
aliases. Bootstrap is idempotent. Mapping/model changes create a shadow index, replay and reconcile
fixtures/source data, then atomically switch the read alias. Rollback moves the alias back; no
workflow configuration changes.
