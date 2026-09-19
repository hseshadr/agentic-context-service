# Policy bundle

`context/authz.rego` is the reference fail-closed policy. The API signs or verifies the canonical
workload headers before creating the OPA input; callers cannot self-assert entitlements. An allow
decision returns constraints that the retrieval adapter must only narrow, never widen.

Run policy tests with:

```bash
opa test policies --coverage --threshold 90
```

The local Compose service mounts this directory read-only. Production must use a signed policy
bundle, authenticated OPA, private networking, and TLS.
