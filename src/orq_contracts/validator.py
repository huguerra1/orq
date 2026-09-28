"""Validação estrutural e semântica dos contratos iniciais."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .schemas import validator_for


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _pointer(parts: Iterable[Any]) -> str:
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(encoded) if encoded else ""


def _structural_issues(instance: Any, schema_name: str) -> list[ValidationIssue]:
    issues = []
    errors = sorted(
        validator_for(schema_name).iter_errors(instance),
        key=lambda error: (tuple(str(part) for part in error.absolute_path), error.message),
    )
    for error in errors:
        issues.append(
            ValidationIssue(
                code=f"schema.{error.validator}",
                path=_pointer(error.absolute_path),
                message=error.message,
            )
        )
    return issues


def _duplicate_issues(items: list[dict[str, Any]], key: str, path: str) -> list[ValidationIssue]:
    seen: dict[str, int] = {}
    issues = []
    for index, item in enumerate(items):
        value = item[key]
        if value in seen:
            issues.append(
                ValidationIssue(
                    code="semantic.duplicate_id",
                    path=f"{path}/{index}/{key}",
                    message=f"{value!r} duplica {path}/{seen[value]}/{key}",
                )
            )
        else:
            seen[value] = index
    return issues


def _task_semantic_issues(task: dict[str, Any], path: str = "") -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    collections = (
        ("requirements", "requirement_id"),
        ("inputs", "input_id"),
        ("expected_output", "output_id"),
        ("knowledge_requirements", "knowledge_requirement_id"),
        ("evaluation_criteria", "criterion_id"),
    )
    for field, key in collections:
        issues.extend(_duplicate_issues(task[field], key, f"{path}/{field}"))

    requirement_by_id = {item["requirement_id"]: item for item in task["requirements"]}
    required_coverage: set[str] = set()
    for criterion_index, criterion in enumerate(task["evaluation_criteria"]):
        for requirement_index, requirement_id in enumerate(criterion["requirement_ids"]):
            if requirement_id not in requirement_by_id:
                issues.append(
                    ValidationIssue(
                        code="semantic.unknown_requirement",
                        path=f"{path}/evaluation_criteria/{criterion_index}/requirement_ids/{requirement_index}",
                        message=f"requisito {requirement_id!r} não existe na tarefa",
                    )
                )
            elif criterion["required"]:
                required_coverage.add(requirement_id)

    for requirement_index, requirement in enumerate(task["requirements"]):
        if requirement["required"] and requirement["requirement_id"] not in required_coverage:
            issues.append(
                ValidationIssue(
                    code="semantic.required_requirement_uncovered",
                    path=f"{path}/requirements/{requirement_index}",
                    message="requisito obrigatório não possui critério obrigatório",
                )
            )
    return issues


def validate_task(task: Any) -> list[ValidationIssue]:
    issues = _structural_issues(task, "task")
    if issues:
        return issues
    return _task_semantic_issues(task)


def _cycle_issues(tasks: list[dict[str, Any]]) -> list[ValidationIssue]:
    dependencies = {task["task_id"]: task["dependencies"] for task in tasks}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(task_id: str) -> list[str] | None:
        state[task_id] = 1
        stack.append(task_id)
        for dependency in dependencies[task_id]:
            if dependency not in dependencies:
                continue
            if state.get(dependency, 0) == 0:
                cycle = visit(dependency)
                if cycle:
                    return cycle
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                return stack[start:] + [dependency]
        stack.pop()
        state[task_id] = 2
        return None

    for task_id in dependencies:
        if state.get(task_id, 0) == 0:
            cycle = visit(task_id)
            if cycle:
                return [
                    ValidationIssue(
                        code="semantic.dependency_cycle",
                        path="/tasks",
                        message="ciclo detectado: " + " -> ".join(cycle),
                    )
                ]
    return []


def _same_ref(left: Any, right: Any) -> bool:
    return left == right or left is None or right is None


def _workflow_semantic_issues(workflow: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for field, key in (
        ("requirements", "requirement_id"),
        ("external_inputs", "input_id"),
        ("tasks", "task_id"),
        ("outputs", "output_id"),
        ("evaluation_criteria", "criterion_id"),
    ):
        issues.extend(_duplicate_issues(workflow[field], key, f"/{field}"))

    task_by_id = {task["task_id"]: task for task in workflow["tasks"]}
    external_by_id = {item["input_id"]: item for item in workflow["external_inputs"]}
    workflow_requirement_by_id = {
        item["requirement_id"]: item for item in workflow["requirements"]
    }
    workflow_output_by_id = {item["output_id"]: item for item in workflow["outputs"]}

    for task_index, task in enumerate(workflow["tasks"]):
        issues.extend(_task_semantic_issues(task, f"/tasks/{task_index}"))
        for dependency_index, dependency_id in enumerate(task["dependencies"]):
            dependency_path = f"/tasks/{task_index}/dependencies/{dependency_index}"
            if dependency_id == task["task_id"]:
                issues.append(
                    ValidationIssue("semantic.self_dependency", dependency_path, "tarefa depende de si própria")
                )
            elif dependency_id not in task_by_id:
                issues.append(
                    ValidationIssue(
                        "semantic.unknown_dependency",
                        dependency_path,
                        f"dependência {dependency_id!r} não existe",
                    )
                )

        for input_index, input_spec in enumerate(task["inputs"]):
            source = input_spec["source"]
            source_path = f"/tasks/{task_index}/inputs/{input_index}/source"
            if source["kind"] == "external_input":
                external = external_by_id.get(source["input_id"])
                if external is None:
                    issues.append(
                        ValidationIssue(
                            "semantic.unknown_external_input",
                            source_path,
                            f"entrada externa {source['input_id']!r} não existe",
                        )
                    )
                elif input_spec["artifact_type"] != external["artifact_type"] or not _same_ref(
                    input_spec.get("content_contract_ref"), external.get("content_contract_ref")
                ):
                    issues.append(
                        ValidationIssue(
                            "semantic.input_type_mismatch",
                            source_path,
                            "tipo ou contrato difere da entrada externa",
                        )
                    )
            else:
                producer = task_by_id.get(source["task_id"])
                if producer is None:
                    issues.append(
                        ValidationIssue(
                            "semantic.unknown_input_task",
                            source_path,
                            f"tarefa produtora {source['task_id']!r} não existe",
                        )
                    )
                    continue
                if source["task_id"] not in task["dependencies"]:
                    issues.append(
                        ValidationIssue(
                            "semantic.input_not_direct_dependency",
                            source_path,
                            "task_output exige dependência direta",
                        )
                    )
                output = next(
                    (item for item in producer["expected_output"] if item["output_id"] == source["output_id"]),
                    None,
                )
                if output is None:
                    issues.append(
                        ValidationIssue(
                            "semantic.unknown_task_output",
                            source_path,
                            f"saída {source['output_id']!r} não existe em {source['task_id']!r}",
                        )
                    )
                elif input_spec["artifact_type"] != output["artifact_type"] or not _same_ref(
                    input_spec.get("content_contract_ref"), output.get("content_contract_ref")
                ):
                    issues.append(
                        ValidationIssue(
                            "semantic.input_type_mismatch",
                            source_path,
                            "tipo ou contrato difere da saída produtora",
                        )
                    )

    issues.extend(_cycle_issues(workflow["tasks"]))

    coverage_by_workflow_requirement: dict[str, list[dict[str, Any]]] = {}
    seen_coverage: set[tuple[str, str, str]] = set()
    for index, coverage in enumerate(workflow["requirement_coverage"]):
        key = (
            coverage["workflow_requirement_id"],
            coverage["task_id"],
            coverage["task_requirement_id"],
        )
        if key in seen_coverage:
            issues.append(
                ValidationIssue(
                    "semantic.duplicate_coverage",
                    f"/requirement_coverage/{index}",
                    "vínculo de cobertura duplicado",
                )
            )
        seen_coverage.add(key)
        workflow_requirement = workflow_requirement_by_id.get(coverage["workflow_requirement_id"])
        task = task_by_id.get(coverage["task_id"])
        if workflow_requirement is None:
            issues.append(
                ValidationIssue(
                    "semantic.unknown_workflow_requirement",
                    f"/requirement_coverage/{index}/workflow_requirement_id",
                    "requisito global não existe",
                )
            )
        if task is None:
            issues.append(
                ValidationIssue(
                    "semantic.unknown_coverage_task",
                    f"/requirement_coverage/{index}/task_id",
                    "tarefa de cobertura não existe",
                )
            )
            continue
        task_requirement = next(
            (
                item
                for item in task["requirements"]
                if item["requirement_id"] == coverage["task_requirement_id"]
            ),
            None,
        )
        if task_requirement is None:
            issues.append(
                ValidationIssue(
                    "semantic.unknown_task_requirement",
                    f"/requirement_coverage/{index}/task_requirement_id",
                    "requisito local não existe",
                )
            )
        elif workflow_requirement and workflow_requirement["required"] and not task_requirement["required"]:
            issues.append(
                ValidationIssue(
                    "semantic.required_coverage_is_optional",
                    f"/requirement_coverage/{index}",
                    "requisito global obrigatório exige requisito local obrigatório",
                )
            )
        coverage_by_workflow_requirement.setdefault(coverage["workflow_requirement_id"], []).append(coverage)

    for index, output in enumerate(workflow["outputs"]):
        task = task_by_id.get(output["source_task_id"])
        if task is None:
            issues.append(
                ValidationIssue(
                    "semantic.unknown_output_task",
                    f"/outputs/{index}/source_task_id",
                    "tarefa produtora não existe",
                )
            )
            continue
        source_output = next(
            (item for item in task["expected_output"] if item["output_id"] == output["source_output_id"]),
            None,
        )
        if source_output is None:
            issues.append(
                ValidationIssue(
                    "semantic.unknown_final_output",
                    f"/outputs/{index}/source_output_id",
                    "saída produtora não existe",
                )
            )
        elif source_output["artifact_type"] != output["artifact_type"]:
            issues.append(
                ValidationIssue(
                    "semantic.output_type_mismatch",
                    f"/outputs/{index}/artifact_type",
                    "tipo final difere da saída produtora",
                )
            )

    required_global_criteria: set[str] = set()
    for criterion_index, criterion in enumerate(workflow["evaluation_criteria"]):
        for requirement_index, requirement_id in enumerate(criterion["workflow_requirement_ids"]):
            if requirement_id not in workflow_requirement_by_id:
                issues.append(
                    ValidationIssue(
                        "semantic.unknown_workflow_requirement",
                        f"/evaluation_criteria/{criterion_index}/workflow_requirement_ids/{requirement_index}",
                        "requisito global não existe",
                    )
                )
            elif criterion["required"]:
                required_global_criteria.add(requirement_id)
        for output_index, output_id in enumerate(criterion["output_ids"]):
            if output_id not in workflow_output_by_id:
                issues.append(
                    ValidationIssue(
                        "semantic.unknown_workflow_output",
                        f"/evaluation_criteria/{criterion_index}/output_ids/{output_index}",
                        "saída final não existe",
                    )
                )

    for index, requirement in enumerate(workflow["requirements"]):
        if not requirement["required"]:
            continue
        requirement_id = requirement["requirement_id"]
        if not coverage_by_workflow_requirement.get(requirement_id):
            issues.append(
                ValidationIssue(
                    "semantic.required_workflow_requirement_uncovered",
                    f"/requirements/{index}",
                    "requisito global obrigatório não possui cobertura local",
                )
            )
        if requirement_id not in required_global_criteria:
            issues.append(
                ValidationIssue(
                    "semantic.required_workflow_requirement_unevaluated",
                    f"/requirements/{index}",
                    "requisito global obrigatório não possui critério global obrigatório",
                )
            )

    return issues


def validate_workflow(workflow: Any) -> list[ValidationIssue]:
    issues = _structural_issues(workflow, "workflow")
    if issues:
        return issues
    return _workflow_semantic_issues(workflow)
