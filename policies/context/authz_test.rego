package agentic_context_test

import rego.v1

base_input := {
    "identity": {
        "subject": "analyst-42",
        "tenant_id": "demo-retail",
        "teams": ["pricing"],
        "entitlements": ["pricing-analysis", "memory-management"],
    },
    "workload": {
        "team_id": "pricing",
        "app_id": "demo-agent",
        "workflow_id": "pricing-analysis",
        "workflow_revision": "v1",
        "agent_id": "demo",
        "environment": "development",
        "cost_center": "oss",
        "headers_verified": true,
    },
    "request": {
        "operation": "retrieve",
        "purpose": "pricing-analysis",
        "corpora": ["pricing", "restricted-payroll"],
        "classifications": ["internal", "restricted"],
        "namespace": null,
    },
}

test_allows_verified_entitled_request if {
    decision := data.agentic_context.authz.decision with input as base_input
    decision.allow
    decision.constraints.corpora == ["pricing"]
    decision.constraints.classifications == ["internal"]
}

test_pricing_facts_are_only_returned_to_pricing_analysis if {
    pricing := data.agentic_context.authz.decision with input as base_input
    "source_facts" in pricing.constraints.fields

    support_identity := object.union(base_input.identity, {
        "entitlements": ["customer-support"],
    })
    support_request := object.union(base_input.request, {"purpose": "customer-support"})
    support_input := object.union(base_input, {
        "identity": support_identity,
        "request": support_request,
    })
    support := data.agentic_context.authz.decision with input as support_input
    not "source_facts" in support.constraints.fields
}

test_fails_closed_for_unverified_headers if {
    workload := object.union(base_input.workload, {"headers_verified": false})
    request := object.union(base_input, {"workload": workload})
    not data.agentic_context.authz.allow with input as request
}

test_unknown_purpose_is_denied if {
    resource := object.union(base_input.request, {"purpose": "curiosity"})
    request := object.union(base_input, {"request": resource})
    decision := data.agentic_context.authz.decision with input as request
    not decision.allow
    decision.reason == "denied"
    decision.constraints.corpora == []
    decision.constraints.classifications == []
    decision.constraints.namespaces == []
    decision.constraints.result_limit == 0
    decision.constraints.tenant_id == "demo-retail"
}

test_absent_filters_preserve_full_persona_constraints if {
    resource := object.union(base_input.request, {
        "corpora": [],
        "classifications": [],
    })
    request := object.union(base_input, {"request": resource})
    decision := data.agentic_context.authz.decision with input as request
    decision.constraints.corpora == ["pricing", "product", "approved-decisions"]
    decision.constraints.classifications == ["confidential", "internal", "memory", "public"]
    not "restricted" in decision.constraints.classifications
}

test_demo_entitlements_produce_unique_classifications if {
    identity := object.union(base_input.identity, {
        "entitlements": ["pricing-analysis", "customer-support", "memory-management"],
    })
    resource := object.union(base_input.request, {"classifications": []})
    request := object.union(base_input, {"identity": identity, "request": resource})
    decision := data.agentic_context.authz.decision with input as request
    decision.constraints.classifications == ["confidential", "internal", "memory", "public"]
}

test_cross_workflow_namespace_is_not_allowed if {
    resource := object.union(base_input.request, {
        "operation": "memory.search",
        "purpose": "memory-management",
        "namespace": "development/other-workflow/v1/analyst-42/sess/demo",
    })
    request := object.union(base_input, {"request": resource})
    decision := data.agentic_context.authz.decision with input as request
    decision.constraints.namespaces == []
}

test_canonical_memory_namespace_is_allowed if {
    resource := object.union(base_input.request, {
        "operation": "memory.write",
        "purpose": "memory-management",
        "corpora": [],
        "classifications": [],
        "namespace": "demo-retail:development:pricing-analysis:v1:analyst-42:sess:demo",
        "source": null,
    })
    request := object.union(base_input, {"request": resource})
    decision := data.agentic_context.authz.decision with input as request
    decision.allow
    decision.constraints.namespaces == [resource.namespace]
}

test_other_subject_memory_namespace_is_not_allowed if {
    resource := object.union(base_input.request, {
        "operation": "memory.search",
        "purpose": "memory-management",
        "corpora": [],
        "classifications": [],
        "namespace": "demo-retail:development:pricing-analysis:v1:other-user:sess:demo",
        "source": null,
    })
    request := object.union(base_input, {"request": resource})
    decision := data.agentic_context.authz.decision with input as request
    decision.allow
    decision.constraints.namespaces == []
}

test_pricing_persona_can_read_postgresql_freshness if {
    resource := object.union(base_input.request, {
        "operation": "freshness.read",
        "purpose": "pricing-analysis",
        "corpora": [],
        "classifications": [],
        "namespace": null,
        "source": "postgresql",
    })
    request := object.union(base_input, {"request": resource})
    decision := data.agentic_context.authz.decision with input as request
    decision.allow
}

test_unknown_source_freshness_is_denied if {
    resource := object.union(base_input.request, {
        "operation": "freshness.read",
        "purpose": "pricing-analysis",
        "corpora": [],
        "classifications": [],
        "namespace": null,
        "source": "payroll",
    })
    request := object.union(base_input, {"request": resource})
    decision := data.agentic_context.authz.decision with input as request
    not decision.allow
}
