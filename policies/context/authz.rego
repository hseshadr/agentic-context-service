package agentic_context.authz

import rego.v1

# Missing, malformed, unverified, or unknown input never produces an allow decision.
default allow := false

valid_identity if {
    input.identity.subject != ""
    input.identity.tenant_id != ""
}

valid_workload if {
    input.workload.headers_verified == true
    input.workload.environment in {"development", "test", "staging", "production"}
    input.workload.workflow_id != ""
    input.workload.workflow_revision != ""
}

purpose_allowed if input.request.purpose in input.identity.entitlements

operation_allowed if input.request.operation in data.context.operations

source_allowed if input.request.operation != "freshness.read"

source_allowed if {
    input.request.operation == "freshness.read"
    input.request.source in data.context.sources_by_purpose[input.request.purpose]
}

allow if {
    valid_identity
    valid_workload
    purpose_allowed
    operation_allowed
    source_allowed
}

purpose_corpora := data.context.corpora_by_purpose[input.request.purpose] if allow

allowed_corpora := purpose_corpora if count(input.request.corpora) == 0

allowed_corpora := [] if not allow

allowed_corpora := [corpus |
    corpus := input.request.corpora[_]
    corpus in purpose_corpora
] if {
    allow
    count(input.request.corpora) > 0
}

persona_classifications := sort({classification |
    allow
    entitlement := input.identity.entitlements[_]
    classification := data.context.classifications_by_entitlement[entitlement][_]
})

allowed_classifications := persona_classifications if count(input.request.classifications) == 0

allowed_classifications := [] if not allow

allowed_classifications := [classification |
    classification := input.request.classifications[_]
    classification in persona_classifications
] if {
    allow
    count(input.request.classifications) > 0
}

valid_namespace if {
    parts := split(input.request.namespace, ":")
    count(parts) == 7
    parts[0] == input.identity.tenant_id
    parts[1] == input.workload.environment
    parts[2] == input.workload.workflow_id
    parts[3] == input.workload.workflow_revision
    parts[4] in {"_", input.identity.subject}
    parts[5] != ""
    parts[6] in {"_", input.workload.agent_id}
}

allowed_namespaces := [input.request.namespace] if {
    allow
    input.request.namespace != null
    valid_namespace
}

allowed_namespaces := [] if input.request.namespace == null

allowed_namespaces := [] if not allow

allowed_namespaces := [] if {
    input.request.namespace != null
    not valid_namespace
}

reason := "allowed" if allow
reason := "denied" if not allow

result_limit := 12 if allow
result_limit := 0 if not allow

allow_lexical_fallback := true if {
    allow
    input.workload.environment != "production"
}

allow_lexical_fallback := false if not allow

allow_lexical_fallback := false if input.workload.environment == "production"

decision := {
    "allow": allow,
    "decision_id": sprintf("opa:%s:%s:%s", [
        input.identity.tenant_id,
        input.workload.workflow_id,
        input.request.operation,
    ]),
    "reason": reason,
    "constraints": {
        "tenant_id": input.identity.tenant_id,
        "corpora": allowed_corpora,
        "classifications": allowed_classifications,
        "fields": object.get(data.context.response_fields_by_purpose, input.request.purpose, []),
        "namespaces": allowed_namespaces,
        "result_limit": result_limit,
        "allow_lexical_fallback": allow_lexical_fallback,
    },
}
