from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from orq_contracts.canonical import canonical_digest
from orq_contracts.router import (
    DecisionEngineResponseError,
    DecisionEngineResult,
    SimulatedDecisionEngine,
    route_and_finalize,
)
from orq_contracts.validator import validate_routing_decision, validate_routing_target_catalog


ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> dict:
    return json.loads((ROOT / "fixtures" / "v0.1" / name).read_text())


def sync_catalog(catalog: dict, request: dict) -> None:
    catalog["catalog_hash"] = canonical_digest({key: value for key, value in catalog.items() if key != "catalog_hash"})
    request["catalog_ref"]["content_digest"] = catalog["catalog_hash"]


def sync_policy(policy: dict, request: dict) -> None:
    request["routing_policy_ref"] = {
        "id": policy["routing_policy_id"],
        "version": policy["version"],
        "content_digest": canonical_digest(policy),
    }


def passing_preflight() -> list[dict]:
    return [{"control": "limits", "status": "passed", "observed_at": "2026-09-29T14:00:02Z", "evidence_refs": []}]


def fixtures() -> tuple[dict, dict, dict]:
    return load("routing-request.json"), load("routing-target-catalog.json"), load("routing-policy-rules.json")


def typed_policy(request: dict, policy: dict, **changes) -> dict:
    policy.update(
        mode="typed_decision",
        decision_engine_ref={
            "id": "typesafe-jev",
            "version": "0.7.2",
            "content_digest": {"algorithm": "sha256", "value": "5" * 64},
        },
        confidence_threshold="0.8",
    )
    policy.update(changes)
    sync_policy(policy, request)
    return policy


def test_rules_selects_lowest_priority_then_id() -> None:
    request, catalog, policy = fixtures()

    decision = route_and_finalize(request, catalog, policy, preflight_checks=passing_preflight())

    assert decision["decision_status"] == "selected"
    assert decision["effective_selection"]["selected_target_id"] == "target-quality"
    assert decision["routing_input"]["eligible_target_ids"] == ["target-quality", "target-fast"]
    assert validate_routing_decision(decision) == []


def test_fixed_target_must_remain_eligible() -> None:
    request, catalog, policy = fixtures()
    policy.update(mode="fixed", fixed_target_id="target-fast")
    sync_policy(policy, request)

    selected = route_and_finalize(request, catalog, policy, preflight_checks=passing_preflight())
    catalog["targets"][0]["status"] = "disabled"
    sync_catalog(catalog, request)
    rejected = route_and_finalize(request, catalog, policy, preflight_checks=passing_preflight())

    assert selected["effective_selection"]["selected_target_id"] == "target-fast"
    assert rejected["effective_selection"] is None
    assert rejected["decision_status"] == "policy_rejected"


def test_unknown_required_capability_excludes_target() -> None:
    request, catalog, policy = fixtures()
    request["task_snapshot"]["required_capabilities"] = ["structured-output"]
    catalog["targets"][0]["capabilities"] = [
        {"id": "structured-output", "support": "unknown", "evidence_refs": []}
    ]
    catalog["targets"][1]["capabilities"] = [
        {"id": "structured-output", "support": "supported", "evidence_refs": []}
    ]
    sync_catalog(catalog, request)

    decision = route_and_finalize(request, catalog, policy, preflight_checks=passing_preflight())

    first = decision["candidates"][0]
    assert first["eligible"] is False
    assert first["reasons"][0]["code"] == "capability_unknown"
    assert decision["routing_input"]["eligible_target_ids"] == ["target-quality"]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("inactive", "target_inactive"),
        ("role", "role_unsupported"),
        ("task_type", "task_type_unsupported"),
        ("permission", "permission_missing"),
        ("control", "control_unknown"),
        ("output", "output_contract_unsupported"),
        ("classification", "classification_unsupported"),
        ("context_unknown", "context_limit_unknown"),
        ("context_exceeded", "context_limit_exceeded"),
        ("output_unknown", "output_limit_unknown"),
        ("output_exceeded", "output_limit_exceeded"),
    ],
)
def test_mandatory_filters_preserve_exclusion_reason(case: str, reason: str) -> None:
    request, catalog, policy = fixtures()
    target = catalog["targets"][0]
    if case == "inactive":
        target["status"] = "deprecated"
    elif case == "role":
        target["supported_role_refs"][0]["id"] = "backend_engineer"
    elif case == "task_type":
        target["supported_task_types"] = ["coding"]
    elif case == "permission":
        target["permission_ids"] = []
    elif case == "control":
        target["controls"][0]["support"] = "unknown"
    elif case == "output":
        target["output_contract_refs"][0]["id"] = "other-contract"
    elif case == "classification":
        target["accepted_classifications"] = ["public"]
    elif case == "context_unknown":
        target["context_limit_tokens"] = None
    elif case == "context_exceeded":
        target["context_limit_tokens"] = 1500
    elif case == "output_unknown":
        target["max_output_tokens"] = None
    elif case == "output_exceeded":
        target["max_output_tokens"] = 500
    sync_catalog(catalog, request)

    decision = route_and_finalize(request, catalog, policy, preflight_checks=passing_preflight())

    assert reason in {item["code"] for item in decision["candidates"][0]["reasons"]}


def test_unknown_classification_never_satisfies_admission() -> None:
    request, catalog, policy = fixtures()
    request["required_classification"] = "unknown"

    decision = route_and_finalize(request, catalog, policy, preflight_checks=passing_preflight())

    assert decision["decision_status"] == "no_eligible_target"
    assert all(candidate["reasons"][0]["code"] == "classification_unknown" for candidate in decision["candidates"])


def test_typed_engine_receives_only_eligible_ids() -> None:
    request, catalog, policy = fixtures()
    catalog["targets"][0]["status"] = "disabled"
    sync_catalog(catalog, request)
    typed_policy(request, policy)
    engine = SimulatedDecisionEngine(DecisionEngineResult("target-quality", "0.91"))

    decision = route_and_finalize(
        request, catalog, policy, preflight_checks=passing_preflight(), decision_engine=engine
    )

    assert [option["target_id"] for option in engine.last_input["options"]] == ["target-quality"]
    assert decision["effective_selection"]["selected_target_id"] == "target-quality"


def test_invalid_typed_target_uses_rules_fallback() -> None:
    request, catalog, policy = fixtures()
    typed_policy(request, policy)
    engine = SimulatedDecisionEngine(DecisionEngineResult("target-invented", "0.99"))

    decision = route_and_finalize(
        request, catalog, policy, preflight_checks=passing_preflight(), decision_engine=engine
    )

    assert decision["decision_status"] == "invalid_response"
    assert decision["effective_selection"] == {
        "recommended_target_id": "target-invented",
        "selected_target_id": "target-quality",
        "used_fallback": True,
        "rule": "rules_fallback",
    }


def test_low_confidence_and_provider_error_are_distinct() -> None:
    request, catalog, policy = fixtures()
    typed_policy(request, policy, low_confidence_action="no_dispatch", provider_error_action="rules_fallback")
    low = route_and_finalize(
        request,
        catalog,
        policy,
        preflight_checks=passing_preflight(),
        decision_engine=SimulatedDecisionEngine(DecisionEngineResult("target-quality", "0.2")),
    )
    failed = route_and_finalize(
        request,
        catalog,
        policy,
        preflight_checks=passing_preflight(),
        decision_engine=SimulatedDecisionEngine(TimeoutError("timeout")),
    )

    assert low["decision_status"] == "low_confidence" and low["effective_selection"] is None
    assert failed["decision_status"] == "provider_error"
    assert failed["effective_selection"]["selected_target_id"] == "target-quality"
    assert failed["provider_extension"] == {"orq": {"decision_engine_error": {"type": "TimeoutError"}}}


def test_malformed_engine_response_is_not_provider_error() -> None:
    request, catalog, policy = fixtures()
    typed_policy(request, policy)
    engine = SimulatedDecisionEngine(
        DecisionEngineResponseError("invalid_probability", "/confidence", "fora do intervalo")
    )

    decision = route_and_finalize(
        request, catalog, policy, preflight_checks=passing_preflight(), decision_engine=engine
    )

    assert decision["decision_status"] == "invalid_response"
    assert decision["provider_extension"]["orq"]["decision_engine_error"]["code"] == "invalid_probability"
    assert decision["effective_selection"]["selected_target_id"] == "target-quality"


def test_failed_preflight_clears_selection() -> None:
    request, catalog, policy = fixtures()
    checks = passing_preflight()
    checks[0]["status"] = "failed"

    decision = route_and_finalize(request, catalog, policy, preflight_checks=checks)

    assert decision["decision_status"] == "preflight_rejected"
    assert decision["effective_selection"] is None


def test_hashes_detect_catalog_and_decision_mutation() -> None:
    request, catalog, policy = fixtures()
    catalog["targets"][0]["priority"] = 0
    assert any(issue.code == "semantic.catalog_hash_mismatch" for issue in validate_routing_target_catalog(catalog))

    request, catalog, policy = fixtures()
    decision = route_and_finalize(request, catalog, policy, preflight_checks=passing_preflight())
    mutated = copy.deepcopy(decision)
    mutated["decision_status"] = "provider_error"
    assert any(issue.code == "semantic.record_hash_mismatch" for issue in validate_routing_decision(mutated))

    mutated = copy.deepcopy(decision)
    mutated["routing_input"]["snapshot"]["task"]["objective"] = "Objetivo adulterado"
    mutated["record_hash"] = canonical_digest({key: value for key, value in mutated.items() if key != "record_hash"})
    assert any(issue.code == "semantic.routing_input_hash_mismatch" for issue in validate_routing_decision(mutated))
