from __future__ import annotations

from copy import deepcopy

from orq_contracts.validator import validate_workflow


def codes(issues):
    return {issue.code for issue in issues}


def test_minimal_workflow_is_valid(valid_workflow) -> None:
    assert validate_workflow(valid_workflow) == []


def test_unknown_dependency_is_rejected(valid_workflow) -> None:
    workflow = deepcopy(valid_workflow)
    workflow["tasks"][0]["dependencies"] = ["MISSING"]

    assert "semantic.unknown_dependency" in codes(validate_workflow(workflow))


def test_self_dependency_is_rejected(valid_workflow) -> None:
    workflow = deepcopy(valid_workflow)
    workflow["tasks"][0]["dependencies"] = ["T1"]

    issues = validate_workflow(workflow)
    assert "semantic.self_dependency" in codes(issues)
    assert "semantic.dependency_cycle" in codes(issues)


def test_cycle_in_disconnected_component_is_rejected(valid_workflow) -> None:
    workflow = deepcopy(valid_workflow)
    task_two = deepcopy(workflow["tasks"][0])
    task_three = deepcopy(workflow["tasks"][0])
    task_two["task_id"] = "T2"
    task_two["dependencies"] = ["T3"]
    task_three["task_id"] = "T3"
    task_three["dependencies"] = ["T2"]
    workflow["tasks"].extend([task_two, task_three])

    assert "semantic.dependency_cycle" in codes(validate_workflow(workflow))


def test_task_output_requires_direct_dependency(valid_workflow) -> None:
    workflow = deepcopy(valid_workflow)
    task_two = deepcopy(workflow["tasks"][0])
    task_two["task_id"] = "T2"
    task_two["dependencies"] = []
    task_two["inputs"] = [
        {
            "input_id": "previous-report",
            "artifact_type": "report",
            "content_contract_ref": workflow["tasks"][0]["expected_output"][0]["content_contract_ref"],
            "source": {"kind": "task_output", "task_id": "T1", "output_id": "report"},
        }
    ]
    workflow["tasks"].append(task_two)

    assert "semantic.input_not_direct_dependency" in codes(validate_workflow(workflow))


def test_required_global_requirement_needs_local_coverage(valid_workflow) -> None:
    workflow = deepcopy(valid_workflow)
    workflow["requirement_coverage"] = []

    issues = validate_workflow(workflow)
    assert "schema.minItems" in codes(issues)


def test_required_global_requirement_needs_required_global_criterion(valid_workflow) -> None:
    workflow = deepcopy(valid_workflow)
    workflow["requirements"].append(
        {"requirement_id": "WREQ2", "description": "Opcional", "required": False}
    )
    workflow["evaluation_criteria"][0]["required"] = False
    workflow["evaluation_criteria"].append(
        {
            "criterion_id": "WCRIT2",
            "workflow_requirement_ids": ["WREQ2"],
            "output_ids": ["final-report"],
            "method": "schema_validation",
            "pass_rule": {"kind": "valid"},
            "required": True,
            "verifier_ref": None,
        }
    )

    issues = validate_workflow(workflow)
    assert "semantic.required_workflow_requirement_unevaluated" in codes(issues)


def test_final_output_must_match_declared_task_output(valid_workflow) -> None:
    workflow = deepcopy(valid_workflow)
    workflow["outputs"][0]["source_output_id"] = "missing"

    assert "semantic.unknown_final_output" in codes(validate_workflow(workflow))
