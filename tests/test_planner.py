from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from orq_contracts.canonical import canonical_digest
from orq_contracts.planner import FixedTemplatePlanner, PlanningError, plan_and_validate
from orq_contracts.validator import validate_planning_record
from test_vault import versioned_ref


ROOT = Path(__file__).resolve().parents[1]
COMPLETED_AT = "2026-09-29T13:00:01Z"


@pytest.fixture
def workflow() -> dict:
    return json.loads((ROOT / "fixtures" / "v0.1" / "valid" / "workflow-minimal.json").read_text())


@pytest.fixture
def planning_request() -> dict:
    return json.loads((ROOT / "fixtures" / "v0.1" / "planning-request.json").read_text())


def planner(workflow: dict) -> FixedTemplatePlanner:
    return FixedTemplatePlanner([workflow], versioned_ref("fixed-template-planner"))


def test_accepts_matching_valid_template(workflow, planning_request) -> None:
    result = plan_and_validate(planner(workflow), planning_request, completed_at=COMPLETED_AT)

    assert result.workflow == workflow
    assert result.record["status"] == "accepted"
    assert result.record["validation_report"] == {"valid": True, "errors": [], "warnings": []}
    assert result.record["usage_record_refs"] == []
    assert validate_planning_record(result.record) == []


def test_rejects_invalid_workflow_even_when_adapter_proposes_it(workflow, planning_request) -> None:
    invalid = copy.deepcopy(workflow)
    invalid["tasks"][0]["dependencies"] = ["T1"]
    planning_request["workflow_template_ref"]["content_digest"] = canonical_digest(invalid)

    result = plan_and_validate(planner(invalid), planning_request, completed_at=COMPLETED_AT)

    assert result.workflow is None
    assert result.record["status"] == "rejected"
    assert result.record["proposed_workflow_ref"]["content_digest"] == canonical_digest(invalid)
    assert any(
        issue["code"] in {"semantic.self_dependency", "semantic.dependency_cycle"}
        for issue in result.record["validation_report"]["errors"]
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("project_id", "outro-projeto", "template_project_mismatch"),
        ("objective", "Outro objetivo", "template_objective_mismatch"),
    ],
)
def test_marks_mismatched_template_not_applicable(workflow, planning_request, field, value, code) -> None:
    planning_request[field] = value

    result = plan_and_validate(planner(workflow), planning_request, completed_at=COMPLETED_AT)

    assert result.workflow is None
    assert result.record["status"] == "not_applicable"
    assert result.record["validation_report"]["errors"][0]["code"] == code


def test_reports_missing_template(workflow, planning_request) -> None:
    planning_request["workflow_template_ref"]["id"] = "workflow-ausente"

    result = plan_and_validate(planner(workflow), planning_request, completed_at=COMPLETED_AT)

    assert result.workflow is None
    assert result.record["status"] == "error"
    assert result.record["validation_report"]["errors"][0]["code"] == "template_not_found"


def test_reports_template_digest_mismatch(workflow, planning_request) -> None:
    planning_request["workflow_template_ref"]["content_digest"]["value"] = "0" * 64

    result = plan_and_validate(planner(workflow), planning_request, completed_at=COMPLETED_AT)

    assert result.workflow is None
    assert result.record["status"] == "error"
    assert result.record["validation_report"]["errors"][0]["code"] == "template_digest_mismatch"


def test_same_inputs_produce_same_record(workflow, planning_request) -> None:
    fixed = planner(workflow)

    first = plan_and_validate(fixed, planning_request, completed_at=COMPLETED_AT)
    second = plan_and_validate(fixed, copy.deepcopy(planning_request), completed_at=COMPLETED_AT)

    assert first == second


def test_invalid_request_is_rejected_before_planner(workflow, planning_request) -> None:
    planning_request["objective"] = ""

    with pytest.raises(PlanningError, match="schema.minLength"):
        plan_and_validate(planner(workflow), planning_request, completed_at=COMPLETED_AT)


def test_record_hash_detects_mutation(workflow, planning_request) -> None:
    record = plan_and_validate(planner(workflow), planning_request, completed_at=COMPLETED_AT).record
    record["status"] = "rejected"

    issues = validate_planning_record(record)

    assert any(issue.code == "semantic.record_hash_mismatch" for issue in issues)
