# Local deployment

The Compose stack is a development showcase, not a production topology. It pins every image,
uses named volumes, and intentionally disables the OpenSearch security plugin so a reviewer can
run it without certificates. Never copy that setting into production.

Production must provide TLS, workload identity, private networking, authenticated OpenSearch and
OPA, managed secrets, replicated storage, backup/restore, resource limits, and a signed policy
bundle. Kubernetes is deliberately not required for v1; see the roadmap for the deferred Helm
example.
