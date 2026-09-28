from __future__ import annotations

from copy import deepcopy

from orq_contracts.validator import validate_task

from conftest import load_fixture


def codes(issues):
    return {issue.code for issue in issues}


def test_minimal_task_is_valid(valid_task) -> None:
    assert validate_task(valid_task) == []


def test_empty_objective_fixture_is_rejected() -> None:
    issues = validate_task(load_fixture("invalid/task-empty-objective.json"))
    assert "schema.minLength" in codes(issues)
    assert any(issue.path == "/objective" for issue in issues)


def test_duplicate_requirement_id_is_rejected(valid_task) -> None:
    task = deepcopy(valid_task)
    task["requirements"].append(deepcopy(task["requirements"][0]))

    issues = validate_task(task)
    assert "semantic.duplicate_id" in codes(issues)


def test_unknown_requirement_in_criterion_is_rejected(valid_task) -> None:
    task = deepcopy(valid_task)
    task["evaluation_criteria"][0]["requirement_ids"] = ["MISSING"]

    issues = validate_task(task)
    assert "semantic.unknown_requirement" in codes(issues)
    assert "semantic.required_requirement_uncovered" in codes(issues)


def test_required_requirement_needs_required_criterion(valid_task) -> None:
    task = deepcopy(valid_task)
    task["requirements"].append(
        {"requirement_id": "REQ2", "description": "Opcional", "required": False}
    )
    task["evaluation_criteria"][0]["required"] = False
    task["evaluation_criteria"].append(
        {
            "criterion_id": "CRIT2",
            "requirement_ids": ["REQ2"],
            "method": "schema_validation",
            "pass_rule": {"kind": "valid"},
            "required": True,
            "verifier_ref": None,
        }
    )

    issues = validate_task(task)
    assert "semantic.required_requirement_uncovered" in codes(issues)
