"""Filtro determinístico, baselines e fronteira tipada do Model Router."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .canonical import canonical_digest, canonicalize
from .quota import QuotaStateSnapshot, subscription_category
from .validator import (
    validate_instance,
    validate_routing_decision,
    validate_routing_policy,
    validate_routing_target_catalog,
)


@dataclass(frozen=True, slots=True)
class RoutingError(Exception):
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


@dataclass(frozen=True, slots=True)
class DecisionEngineResponseError(Exception):
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


@dataclass(frozen=True, slots=True)
class DecisionEngineResult:
    target_id: str
    confidence: str | None
    scores: dict[str, str] = field(default_factory=dict)
    engine_call_id: str | None = None
    requested_model_id: str | None = None
    resolved_model_id: str | None = None
    usage_record_refs: tuple[dict[str, Any], ...] = ()
    provider_extension: dict[str, Any] | None = None


class DecisionEngine(Protocol):
    def decide(self, routing_input: dict[str, Any]) -> DecisionEngineResult: ...


class SimulatedDecisionEngine:
    """Transporte de teste que também preserva a última entrada recebida."""

    def __init__(self, result: DecisionEngineResult | Exception):
        self.result = result
        self.last_input: dict[str, Any] | None = None

    def decide(self, routing_input: dict[str, Any]) -> DecisionEngineResult:
        self.last_input = copy.deepcopy(routing_input)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _raise_first(issues: list[Any]) -> None:
    if issues:
        issue = issues[0]
        raise RoutingError(issue.code, issue.path, issue.message)


def _target_ref(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": target["target_id"],
        "version": target["version"],
        "content_digest": canonical_digest(target),
    }


def _reason(code: str, subject: str, evidence_refs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"code": code, "subject": subject, "evidence_refs": copy.deepcopy(evidence_refs or [])}


def _declared_support(target: dict[str, Any], field: str, requirement_id: str) -> tuple[str | None, list[dict[str, Any]]]:
    for declaration in target[field]:
        if declaration["id"] == requirement_id:
            return declaration["support"], declaration["evidence_refs"]
    return None, []


def _eligibility_reasons(request: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    task = request["task_snapshot"]
    reasons: list[dict[str, Any]] = []
    if target["status"] != "active":
        reasons.append(_reason("target_inactive", target["status"]))
    if task["agent_role"] not in target["supported_role_refs"]:
        reasons.append(_reason("role_unsupported", task["agent_role"]["id"]))
    if task["task_type"] not in target["supported_task_types"]:
        reasons.append(_reason("task_type_unsupported", task["task_type"]))
    for capability_id in task["required_capabilities"]:
        support, evidence = _declared_support(target, "capabilities", capability_id)
        if support != "supported":
            reasons.append(_reason("capability_unknown" if support == "unknown" else "capability_missing", capability_id, evidence))
    for permission_id in request["required_permission_ids"]:
        if permission_id not in target["permission_ids"]:
            reasons.append(_reason("permission_missing", permission_id))
    for control_id in request["required_control_ids"]:
        support, evidence = _declared_support(target, "controls", control_id)
        if support != "supported":
            reasons.append(_reason("control_unknown" if support == "unknown" else "control_missing", control_id, evidence))
    for output in task["expected_output"]:
        if output["content_contract_ref"] not in target["output_contract_refs"]:
            reasons.append(_reason("output_contract_unsupported", output["content_contract_ref"]["id"]))
    if request["required_classification"] == "unknown":
        reasons.append(_reason("classification_unknown", "required_classification"))
    elif request["required_classification"] not in target["accepted_classifications"]:
        reasons.append(_reason("classification_unsupported", request["required_classification"]))
    context_limit = target["context_limit_tokens"]
    required_total = request["estimated_context_input_tokens"] + request["required_output_tokens"]
    if context_limit is None:
        reasons.append(_reason("context_limit_unknown", "context_limit_tokens"))
    elif required_total > context_limit:
        reasons.append(_reason("context_limit_exceeded", str(required_total)))
    output_limit = target["max_output_tokens"]
    if output_limit is None:
        reasons.append(_reason("output_limit_unknown", "max_output_tokens"))
    elif request["required_output_tokens"] > output_limit:
        reasons.append(_reason("output_limit_exceeded", str(request["required_output_tokens"])))
    return reasons


def _fallback(action: str, eligible: list[dict[str, Any]], policy: dict[str, Any]) -> tuple[str | None, str | None]:
    if action == "rules_fallback":
        return (eligible[0]["target_id"], "rules_fallback") if eligible else (None, None)
    if action == "fixed_fallback":
        target_id = policy["fallback_target_id"]
        if any(target["target_id"] == target_id for target in eligible):
            return target_id, "fixed_fallback"
    return None, None


def _confidence_is_low(value: str | None, threshold: str) -> bool:
    if value is None:
        return True
    try:
        return Decimal(value) < Decimal(threshold)
    except InvalidOperation:
        return True


def _valid_probability(value: str | None) -> bool:
    if value is None:
        return True
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError):
        return False
    return parsed.is_finite() and Decimal(0) <= parsed <= Decimal(1)


def _validate_engine_result(result: DecisionEngineResult) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._:-]{0,127}", result.target_id):
        raise DecisionEngineResponseError("invalid_target_id", "/target_id", "identificador inválido")
    if not _valid_probability(result.confidence):
        raise DecisionEngineResponseError("invalid_confidence", "/confidence", "confiança fora de 0..1")
    if any(not _valid_probability(value) for value in result.scores.values()):
        raise DecisionEngineResponseError("invalid_score", "/scores", "pontuação fora de 0..1")


def route_and_finalize(
    request: dict[str, Any],
    catalog: dict[str, Any],
    policy: dict[str, Any],
    *,
    preflight_checks: list[dict[str, Any]],
    decision_engine: DecisionEngine | None = None,
    quota_snapshot: QuotaStateSnapshot | None = None,
    implementation_families: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Produz decisão final; não despacha nem consome tentativa."""

    _raise_first(validate_instance(request, "routing_request"))
    _raise_first(validate_routing_target_catalog(catalog))
    _raise_first(validate_routing_policy(policy))
    expected_catalog_ref = {
        "id": catalog["catalog_id"], "version": catalog["version"], "content_digest": catalog["catalog_hash"]
    }
    if request["catalog_ref"] != expected_catalog_ref:
        raise RoutingError("catalog_ref_mismatch", "/catalog_ref", "referência diverge do catálogo")
    expected_policy_ref = {
        "id": policy["routing_policy_id"], "version": policy["version"], "content_digest": canonical_digest(policy)
    }
    if request["routing_policy_ref"] != expected_policy_ref:
        raise RoutingError("routing_policy_ref_mismatch", "/routing_policy_ref", "referência diverge da política")

    candidates: list[dict[str, Any]] = []
    eligible_targets: list[dict[str, Any]] = []
    rules = policy.get("subscription_rules") if policy["mode"] == "rules" else None
    category = subscription_category(request["task_snapshot"]) if rules else None
    family_order = rules["family_order_by_task_type"].get(category, []) if rules else []
    otherwise_eligible = []
    for position, target in enumerate(catalog["targets"], 1):
        reasons = _eligibility_reasons(request, target)
        if family_order and target.get("family_id") not in family_order:
            reasons.append(_reason("family_not_in_task_order", str(category)))
        if (category == "review" and rules["review_requires_different_family"]
                and target.get("family_id") in implementation_families):
            reasons.append(_reason("review_family_conflict", target["family_id"]))
        if not reasons:
            otherwise_eligible.append(target)
        if quota_snapshot is not None:
            reasons.extend(quota_snapshot.reasons(target))
        candidate = {
            "target_ref": _target_ref(target),
            "input_position": position,
            "eligible": not reasons,
            "reasons": reasons,
            "rank": None,
        }
        candidates.append(candidate)
        if not reasons:
            eligible_targets.append(target)
    def priority(target):
        return ((family_order.index(target["family_id"]) if family_order else 0),
                target["priority"], target["target_id"])
    eligible_targets.sort(key=priority)
    otherwise_eligible.sort(key=priority)
    rank_by_id = {target["target_id"]: index for index, target in enumerate(eligible_targets, 1)}
    for candidate in candidates:
        candidate["rank"] = rank_by_id.get(candidate["target_ref"]["id"])

    routing_payload = {
        "routing_request_id": request["routing_request_id"],
        "task": {
            "task_id": request["task_snapshot"]["task_id"],
            "objective": request["task_snapshot"]["objective"],
            "task_type": request["task_snapshot"]["task_type"],
            "required_capabilities": request["task_snapshot"]["required_capabilities"],
        },
        "options": [
            {
                "target_id": target["target_id"],
                "model_ref": target["model_ref"],
                "executor_ref": target["executor_ref"],
                "priority": target["priority"],
                "routing_description": target["routing_description"],
                "specialization_tags": target["specialization_tags"],
            }
            for target in eligible_targets
        ],
    }
    recommendation = None
    selection = None
    usage_refs: list[dict[str, Any]] = []
    provider_extension = None
    status = "no_eligible_target"

    if eligible_targets:
        if policy["mode"] == "fixed":
            target_id = policy["fixed_target_id"]
            if target_id in rank_by_id:
                recommendation = _recommendation(target_id, None)
                selection = _selection(target_id, target_id, False, "fixed")
                status = "selected"
            else:
                status = "policy_rejected"
                primary = next((t for t in catalog["targets"] if t["target_id"] == target_id), None)
                replacement = next((t for t in eligible_targets if t["target_id"] == policy["fallback_target_id"]), None)
                if (primary and replacement and quota_snapshot and
                        any(r["code"] == "quota_exhausted" for r in quota_snapshot.reasons(primary)) and
                        primary.get("family_id") != replacement.get("family_id")):
                    recommendation = _recommendation(target_id, None)
                    selection = _selection(target_id, replacement["target_id"], True, "quota_fallback")
                    status = "selected"
        elif policy["mode"] == "rules":
            target_id = eligible_targets[0]["target_id"]
            recommendation = _recommendation(target_id, None)
            selection = _selection(target_id, target_id, False, "priority_then_id")
            original = otherwise_eligible[0] if otherwise_eligible else None
            if original and quota_snapshot and any(r["code"] == "quota_exhausted" for r in quota_snapshot.reasons(original)):
                # Mesmo pool/família nunca é fallback de uma recusa de assinatura.
                replacement = next((t for t in eligible_targets if t.get("family_id") != original.get("family_id")), None)
                if replacement:
                    target_id = replacement["target_id"]
                    recommendation = _recommendation(original["target_id"], None)
                    selection = _selection(original["target_id"], target_id, True, "quota_fallback")
                else:
                    selection = None
            status = "selected" if selection is not None else "no_eligible_target"
        else:
            if decision_engine is None:
                raise RoutingError("decision_engine_required", "/decision_engine", "motor tipado ausente")
            try:
                result = decision_engine.decide(copy.deepcopy(routing_payload))
                _validate_engine_result(result)
                recommendation = _recommendation(
                    result.target_id,
                    result.confidence,
                    result.scores,
                    result.engine_call_id,
                    result.requested_model_id,
                    result.resolved_model_id,
                )
                usage_refs = [copy.deepcopy(ref) for ref in result.usage_record_refs]
                provider_extension = copy.deepcopy(result.provider_extension)
                if result.target_id not in rank_by_id:
                    status = "invalid_response"
                    action = policy["invalid_response_action"]
                elif _confidence_is_low(result.confidence, policy["confidence_threshold"]):
                    status = "low_confidence"
                    action = policy["low_confidence_action"]
                else:
                    selection = _selection(result.target_id, result.target_id, False, "typed_decision")
                    status = "selected"
                    action = "no_dispatch"
            except DecisionEngineResponseError as error:
                status = "invalid_response"
                action = policy["invalid_response_action"]
                provider_extension = {
                    "orq": {"decision_engine_error": {"type": type(error).__name__, "code": error.code}}
                }
            except Exception as error:  # barreira para SDK/transporte externo
                status = "provider_error"
                action = policy["provider_error_action"]
                provider_extension = {"orq": {"decision_engine_error": {"type": type(error).__name__}}}
            if selection is None:
                fallback_id, fallback_rule = _fallback(action, eligible_targets, policy)
                if fallback_id is not None:
                    recommended_id = recommendation["target_id"] if recommendation else None
                    selection = _selection(recommended_id, fallback_id, True, fallback_rule)

    if selection is not None and (not preflight_checks or any(check["status"] != "passed" for check in preflight_checks)):
        selection = None
        status = "preflight_rejected"

    payload_bytes = canonicalize(routing_payload)
    decision = {
        "schema_version": "0.1",
        "routing_decision_id": f"routing-{request['routing_request_id']}",
        "run_id": request["run_id"],
        "workflow_ref": copy.deepcopy(request["workflow_ref"]),
        "task_id": request["task_snapshot"]["task_id"],
        "candidate_attempt_number": request["candidate_attempt_number"],
        "created_at": request["created_at"],
        "catalog_snapshot_ref": copy.deepcopy(request["catalog_ref"]),
        "execution_policy_snapshot_ref": copy.deepcopy(request["execution_policy_snapshot_ref"]),
        "routing_policy_snapshot": copy.deepcopy(policy),
        "routing_input": {
            "snapshot": routing_payload,
            "input_hash": canonical_digest(routing_payload),
            "size_bytes": len(payload_bytes),
            "eligible_target_ids": [target["target_id"] for target in eligible_targets],
        },
        "candidates": candidates,
        "recommendation": recommendation,
        "effective_selection": selection,
        "decision_status": status,
        "preflight_checks": copy.deepcopy(preflight_checks),
        "usage_record_refs": usage_refs,
        "provider_extension": provider_extension,
    }
    if not eligible_targets and otherwise_eligible and quota_snapshot and all(
            any(r["code"] == "quota_exhausted" for r in quota_snapshot.reasons(t)) for t in otherwise_eligible):
        decision["reason"] = _reason("all_eligible_families_quota_exhausted", "subscription_pools")
    decision["record_hash"] = canonical_digest(decision)
    _raise_first(validate_routing_decision(decision))
    return decision


def _recommendation(
    target_id: str,
    confidence: str | None,
    scores: dict[str, str] | None = None,
    engine_call_id: str | None = None,
    requested_model_id: str | None = None,
    resolved_model_id: str | None = None,
) -> dict[str, Any]:
    return {
        "target_id": target_id,
        "confidence": confidence,
        "scores": copy.deepcopy(scores or {}),
        "engine_call_id": engine_call_id,
        "requested_model_id": requested_model_id,
        "resolved_model_id": resolved_model_id,
    }


def _selection(recommended: str | None, selected: str, fallback: bool, rule: str) -> dict[str, Any]:
    return {
        "recommended_target_id": recommended,
        "selected_target_id": selected,
        "used_fallback": fallback,
        "rule": rule,
    }
