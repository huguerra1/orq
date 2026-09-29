"""Interface do planner e baseline determinístico por template fixo."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Protocol

from .canonical import canonical_digest
from .validator import ValidationIssue, validate_instance, validate_planning_record, validate_workflow


@dataclass(frozen=True, slots=True)
class PlanningError(Exception):
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


@dataclass(frozen=True, slots=True)
class PlanningResult:
    record: dict[str, Any]
    workflow: dict[str, Any] | None


class Planner(Protocol):
    planner_ref: dict[str, Any]
    mode: str

    def propose(self, request: dict[str, Any]) -> dict[str, Any]: ...


def _workflow_ref(workflow: dict[str, Any]) -> dict[str, Any] | None:
    workflow_id = workflow.get("workflow_id")
    version = workflow.get("version")
    if not isinstance(workflow_id, str) or not workflow_id or not isinstance(version, str) or not version:
        return None
    return {
        "id": workflow_id,
        "version": version,
        "content_digest": canonical_digest(workflow),
    }


class FixedTemplatePlanner:
    mode = "fixed_template"

    def __init__(self, templates: list[dict[str, Any]], planner_ref: dict[str, Any]):
        self.planner_ref = copy.deepcopy(planner_ref)
        self._templates: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
        for template in templates:
            reference = _workflow_ref(template)
            if reference is None:
                raise PlanningError("invalid_template_identity", "/templates", "workflow_id/version ausente")
            key = reference["id"], reference["version"]
            if key in self._templates:
                raise PlanningError("duplicate_template", "/templates", f"template duplicado: {key}")
            self._templates[key] = (reference, copy.deepcopy(template))

    def propose(self, request: dict[str, Any]) -> dict[str, Any]:
        requested = request["workflow_template_ref"]
        resolved = self._templates.get((requested["id"], requested["version"]))
        if resolved is None:
            raise PlanningError("template_not_found", "/workflow_template_ref", "template ausente")
        reference, workflow = resolved
        if reference["content_digest"] != requested["content_digest"]:
            raise PlanningError(
                "template_digest_mismatch",
                "/workflow_template_ref/content_digest",
                "digest solicitado diverge do template publicado",
            )
        if workflow.get("project_id") != request["project_id"]:
            raise PlanningError("template_project_mismatch", "/project_id", "template pertence a outro projeto")
        if workflow.get("objective") != request["objective"]:
            raise PlanningError("template_objective_mismatch", "/objective", "objetivo diverge do template")
        return copy.deepcopy(workflow)


def _error_issue(error: PlanningError) -> dict[str, str]:
    return {"code": error.code, "path": error.path, "message": error.message}


def _validation_report(issues: list[ValidationIssue]) -> dict[str, Any]:
    return {
        "valid": not issues,
        "errors": [issue.as_dict() for issue in issues],
        "warnings": [],
    }


def plan_and_validate(
    planner: Planner,
    request: dict[str, Any],
    *,
    completed_at: str,
) -> PlanningResult:
    """Executa proposta e aplica o gate central, sem efeitos de execução."""

    request_issues = validate_instance(request, "planning_request")
    if request_issues:
        issue = request_issues[0]
        raise PlanningError(issue.code, issue.path, issue.message)

    workflow = None
    proposed_ref = None
    try:
        proposal = planner.propose(copy.deepcopy(request))
        proposed_ref = _workflow_ref(proposal)
        issues = validate_workflow(proposal)
        report = _validation_report(issues)
        status = "accepted" if not issues else "rejected"
        if status == "accepted":
            workflow = proposal
    except PlanningError as error:
        status = "not_applicable" if error.code in {
            "template_project_mismatch",
            "template_objective_mismatch",
        } else "error"
        report = {"valid": False, "errors": [_error_issue(error)], "warnings": []}
    except Exception as error:  # pragma: no cover - barreira contra adaptadores externos
        status = "error"
        report = {
            "valid": False,
            "errors": [
                {
                    "code": "planner_error",
                    "path": "",
                    "message": f"falha inesperada do adaptador: {type(error).__name__}",
                }
            ],
            "warnings": [],
        }

    record = {
        "schema_version": "0.1",
        "planning_record_id": f"planning-{request['planning_request_id']}",
        "run_id": request["run_id"],
        "planning_request_snapshot": copy.deepcopy(request),
        "request_hash": canonical_digest(request),
        "planner_ref": copy.deepcopy(planner.planner_ref),
        "mode": planner.mode,
        "template_ref": copy.deepcopy(request["workflow_template_ref"]),
        "proposed_workflow_ref": proposed_ref,
        "validation_report": report,
        "usage_record_refs": [],
        "status": status,
        "started_at": request["created_at"],
        "completed_at": completed_at,
    }
    record["record_hash"] = canonical_digest(record)
    record_issues = validate_planning_record(record)
    if record_issues:
        issue = record_issues[0]
        raise PlanningError(issue.code, issue.path, issue.message)
    return PlanningResult(record=record, workflow=workflow)
