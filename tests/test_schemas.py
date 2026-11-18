from __future__ import annotations

from orq_contracts.schemas import load_schemas, validator_for


def test_all_declared_schemas_load_and_validate_their_meta_schema() -> None:
    assert set(load_schemas()) == {
        "agent_family",
        "attempt_header",
        "audience_metadata",
        "calibration_report",
        "common",
        "context_manifest",
        "dispatch_intent",
        "error_record",
        "evaluation_report",
        "evaluator_routing_policy",
        "journal_event",
        "knowledge_catalog",
        "knowledge_selection",
        "knowledge_source",
        "planning_record",
        "planning_request",
        "post_hoc_rubric_audit_record",
        "quota_consumption_record",
        "quota_state_record",
        "routing_decision",
        "routing_policy",
        "routing_request",
        "routing_target_catalog",
        "rubric_evaluation_evidence",
        "rubric_spec",
        "run_intent",
        "run_manifest",
        "simulation_script",
        "simulated_output",
        "subscription_profile",
        "task",
        "usage_record",
        "workflow",
    }


def test_validators_use_draft_2020_12() -> None:
    assert validator_for("task").META_SCHEMA["$id"] == "https://json-schema.org/draft/2020-12/schema"
    assert validator_for("workflow").META_SCHEMA["$id"] == "https://json-schema.org/draft/2020-12/schema"
