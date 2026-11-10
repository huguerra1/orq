"""Tabelas AT/TK/RN e validação pura sobre a projeção anterior ao evento."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .projections import RunProjection


class StateMachineError(ValueError):
    """Contrato violado; ``code`` é estável e independente da mensagem."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Transition:
    id: str
    aggregate_type: str
    from_states: tuple[str | None, ...]
    to_state: str
    event: str
    condition: str


def _table(kind: str, rows: tuple) -> tuple[Transition, ...]:
    return tuple(Transition(id, kind, (source,) if not isinstance(source, tuple) else source,
                            target, event, condition)
                 for id, source, target, event, condition in rows)


ATTEMPT_TRANSITIONS = _table("attempt", (
    ("AT01", None, "queued", "dispatch_requested", "admission_complete"),
    ("AT02", "queued", "running", "dispatch_acknowledged,execution_started", "started"),
    ("AT03", "queued", "failed", "error_attached", "dispatch_failed_confirmed"),
    ("AT04", "queued", "cancelled", "cancel_requested", "stopping_before_dispatch"),
    ("AT05", "queued", "unknown", "error_attached|crash", "dispatch_uncertain"),
    ("AT06", "running", "completed", "termination_confirmed,result_collected,artifact_ingested,output_bound", "outputs_complete"),
    ("AT07", "running", "failed", "termination_confirmed,error_attached", "execution_failed"),
    ("AT08", "running", "timed_out", "deadline_exceeded,cancel_requested,termination_confirmed", "timeout_confirmed"),
    ("AT09", "running", "cancelled", "cancel_requested,termination_confirmed", "cancel_confirmed"),
    ("AT10", "running", "unknown", "error_attached|crash", "termination_uncertain"),
    ("AT11", "unknown", "running", "reconciliation_observed", "found_running_in_time"),
    ("AT12", "unknown", "completed", "reconciliation_observed,result_collected,artifact_ingested,output_bound", "found_finished"),
    ("AT13", "unknown", "failed", "reconciliation_observed,error_attached", "found_failed_or_authoritative_absence"),
    ("AT14", "unknown", "timed_out", "reconciliation_observed,deadline_exceeded", "found_stopped_after_deadline"),
    ("AT15", "unknown", "cancelled", "reconciliation_observed,cancel_requested", "found_stopped_or_authoritative_absence"),
))

TASK_TRANSITIONS = _table("task", (
    ("TK01", "pending", "ready", "outputs_accepted", "dependencies_accepted"),
    ("TK02", "pending", "blocked", "state_transition", "dependency_failed_and_stopping"),
    ("TK03", "ready", "running", "dispatch_requested", "attempt_created"),
    ("TK04", "ready", "failed", "admission_rejected", "admission_rejected"),
    ("TK05", "running", "evaluating", "AT06", "attempt_completed"),
    ("TK06", "running", "retry_wait", "AT03|AT07|AT08", "retry_allowed"),
    ("TK07", "running", "failed", "AT03|AT07|AT08", "retry_forbidden"),
    ("TK08", "running", "reconciling", "AT05|AT10", "attempt_unknown"),
    ("TK09", "running", "cancelled", "AT04|AT09", "attempt_cancelled"),
    ("TK10", "reconciling", "running", "AT11", "attempt_running"),
    ("TK11", "reconciling", "evaluating", "AT12|evaluation_report_recorded", "result_recovered"),
    ("TK12", "reconciling", "retry_wait", "AT13|AT14", "retry_allowed"),
    ("TK13", "reconciling", "failed", "AT13|AT14", "retry_forbidden"),
    ("TK14", "reconciling", "cancelled", "AT15", "attempt_cancelled_and_stopping"),
    ("TK15", "reconciling", "indeterminate", "reconciliation_requested|TK21", "reconciliation_expired"),
    ("TK16", "evaluating", "succeeded", "evaluation_report_recorded,outputs_accepted", "pass_with_identical_bindings"),
    ("TK17", "evaluating", "retry_wait", "evaluation_report_recorded", "fail_and_retry_allowed"),
    ("TK18", "evaluating", "failed", "evaluation_report_recorded", "fail_and_retry_forbidden"),
    ("TK19", "evaluating", "waiting_evaluation", "evaluation_report_recorded", "inconclusive_with_capacity"),
    ("TK20", "evaluating", "failed", "evaluation_report_recorded", "inconclusive_without_capacity"),
    ("TK21", "evaluating", "reconciling", "evaluation_requested", "evaluator_uncertain"),
    ("TK22", "waiting_evaluation", "evaluating", "evaluation_requested", "same_subjects"),
    ("TK23", "retry_wait", "ready", "", "running_and_previous_terminated"),
    ("TK24", ("pending", "ready", "retry_wait", "waiting_evaluation", "evaluating"), "cancelled",
     "run:stopping,evaluation_report_recorded", "no_active_work_or_acceptance"),
))

RUN_TRANSITIONS = _table("run", (
    ("RN01", None, "created", "run_intent_recorded", "first_event"),
    ("RN02", "created", "running", "", "prepare"),
    ("RN03", "running", "failed", "run_manifest_closed", "planning_failed"),
    ("RN04", "running", "cancelled", "cancel_requested,run_manifest_closed", "cancelled_before_ready"),
    ("RN05", "running", "succeeded", "evaluation_report_recorded", "all_tasks_succeeded_and_global_pass"),
    ("RN06", "running", "failed", "evaluation_report_recorded", "global_non_pass"),
    ("RN07", "running", "stopping", "state_transition|deadline_exceeded|cancel_requested|error_attached", "stop_cause"),
    ("RN08", "stopping", "failed", "", "no_active_work_and_failure"),
    ("RN09", "stopping", "cancelled", "cancel_requested", "no_active_work_and_cancel"),
    ("RN10", "stopping", "indeterminate", "state_transition", "indeterminate_task"),
))
TRANSITIONS = ATTEMPT_TRANSITIONS + TASK_TRANSITIONS + RUN_TRANSITIONS
TASK_TERMINAL = frozenset({"succeeded", "failed", "blocked", "cancelled", "indeterminate"})
RUN_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "indeterminate"})
ATTEMPT_TERMINAL = frozenset({"completed", "failed", "timed_out", "cancelled"})


def require(condition: Any, code: str, message: str) -> None:
    if not condition:
        raise StateMachineError(code, message)


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_edge(aggregate_type: str, current_state: str | None, from_state: str | None,
                  to_state: str, *, event_type: str = "state_transition") -> Transition:
    """Confere a linha e o Invariante 10; guardas usam validate_transition."""
    require(from_state == current_state, "transition_from_mismatch", "from diverge da projeção anterior")
    matches = [row for row in TRANSITIONS if row.aggregate_type == aggregate_type
               and from_state in row.from_states and row.to_state == to_state]
    require(matches, "transition_not_allowed", f"salto {aggregate_type}: {from_state} → {to_state}")
    row = matches[0]
    require((from_state is None and event_type == row.event)
            or (from_state is not None and event_type == "state_transition"),
            "transition_not_allowed", "criação é implícita no evento inicial")
    return row


def retry_allowed(projection: RunProjection, task_id: str, now: str) -> bool:
    attempt = projection.latest_attempt(task_id)
    if not attempt or projection.state != "running" or not projection.time_remaining(now):
        return False
    if attempt.header["attempt_number"] >= projection.limit("max_attempts", task_id):
        return False
    if attempt.termination_certainty != "confirmed":
        return False
    policy = projection.manifest["resolved_limits"]["retry_policy"]
    if attempt.state == "timed_out":
        return policy["on_confirmed_timeout"]
    if attempt.state == "completed":
        report = projection.latest_report(task_id)
        return bool(report and report["verdict"] == "fail" and policy["on_evaluation_fail"])
    if attempt.state == "failed":
        error = projection.errors.get(attempt.failure_error_id)
        return bool(error and error["retryability"] == "retryable" and policy["on_retryable_error"])
    return False


def validate_transition(projection: RunProjection, event: dict[str, Any]) -> Transition:
    """Valida sem mutar a projeção, inclusive evidências e guardas disponíveis no journal.

    Bytes, saldo para futura admissão e prova volátil de AT04 pertencem ao runner.
    AT04 no replay exige a ordem de cancelamento e ausência de evidência de execução;
    a função de retomada nunca recomenda AT04 para intenção encontrada após crash.
    """
    kind, id, p = event["aggregate_type"], event["aggregate_id"], event["payload"]
    state = projection.aggregate_state(kind, id)
    row = validate_edge(kind, state, p["from"], p["to"])
    # Duas linhas podem compartilhar a aresta, mas têm evidências distintas.
    if kind == "task" and state == "evaluating" and p["to"] == "failed":
        report = projection.latest_report(id)
        if report and report["verdict"] in {"inconclusive", "not_run"}:
            row = next(r for r in TASK_TRANSITIONS if r.id == "TK20")
    if kind == "run" and state == "running" and p["to"] == "failed" and projection.global_report:
        row = next(r for r in RUN_TRANSITIONS if r.id == "RN06")
    ids = p["evidence_event_ids"]
    require(all(id in projection.events for id in ids), "transition_evidence_missing", "evidência deve ser anterior")
    # Somente evidências explicitamente citadas autorizam a mudança.
    evidence = sorted((projection.events[id] for id in ids), key=lambda e: e["global_sequence"])
    local = [e for e in evidence if e["aggregate_type"] == kind and e["aggregate_id"] == id]
    def has(type: str, **fields: Any) -> bool:
        return any(e["event_type"] == type and all(e["payload"].get(k) == v for k, v in fields.items()) for e in local)
    def guard(ok: Any) -> None:
        require(ok, "transition_guard_failed", f"{row.id}: {row.condition}")
    def proof(ok: Any) -> None:
        require(ok, "transition_evidence_missing", f"{row.id}: {row.event}")

    if kind == "attempt":
        a = projection.attempts[id]
        guard(projection.tasks[a.header["task_id"]].state != "indeterminate")
        confirmed_errors = [e for e in local if e["event_type"] == "error_attached" and
                            e["payload"]["termination_certainty"] == "confirmed"]
        failure_error = confirmed_errors[-1] if confirmed_errors else None
        observed = [e for e in local if e["event_type"] == "reconciliation_observed"]
        outcome = observed[-1]["payload"]["outcome"] if observed else None
        if row.id in {"AT11", "AT12", "AT13", "AT14", "AT15"} and observed:
            proof(observed[-1] == projection.history("attempt", id, "reconciliation_observed")[-1])
        authoritative = bool(observed and outcome == "not_found" and
                             projection.reconcile_not_found_authoritative and observed[-1]["payload"]["evidence_refs"])
        before_observation = observed[-1]["global_sequence"] if observed else 0
        deadline = any(e["global_sequence"] < before_observation for e in
                       projection.history("attempt", id, "deadline_exceeded"))
        cancel = any(e["global_sequence"] < before_observation for e in
                     projection.history("attempt", id, "cancel_requested") + projection.cancel_requested)
        if row.id == "AT02":
            proof(has("dispatch_acknowledged") and has("execution_started"))
        elif row.id == "AT03":
            proof(failure_error and failure_error["payload"]["code"] == "dispatch_failed")
        elif row.id == "AT04":
            guard(projection.state == "stopping" and not a.effect_proven and not a.error_record_ids and any(
                e["global_sequence"] > a.dispatch_sequence for e in projection.cancel_requested))
            guard(not projection.history("attempt", id, "dispatch_acknowledged"))
            proof(any(e in projection.cancel_requested for e in evidence))
        elif row.id in {"AT05", "AT10"}:
            guard(a.termination_certainty != "confirmed")
            if row.id == "AT05" and p["reason"] == "crash":
                guard(not projection.history("attempt", id, "dispatch_acknowledged"))
            proof(p["reason"] == "crash" or any(e["event_type"] == "error_attached" and
                  e["payload"]["termination_certainty"] == "unknown" for e in local))
        elif row.id in {"AT06", "AT12"}:
            terminations = projection.history("attempt", id, "termination_confirmed") if row.id == "AT06" else observed
            termination = terminations[-1] if terminations else None
            proof(termination and termination in local and (termination["payload"]["cause"] == "finished"
                  if row.id == "AT06" else outcome == "found_finished"))
            proof(has("result_collected") and has("artifact_ingested") and has("output_bound"))
            guard(projection.outputs_complete(a) and not any(e["global_sequence"] > termination["global_sequence"] and
                  e["payload"]["termination_certainty"] == "confirmed" for e in projection.history("attempt", id, "error_attached")))
            proof(all(any(e["event_type"] == "output_bound" and e["payload"] == binding for e in local) and
                      any(e["event_type"] == "artifact_ingested" and e["payload"]["artifact_id"] == binding["artifact"]["artifact_id"]
                          for e in local) for binding in a.output_bindings))
        elif row.id == "AT07":
            terminations = projection.history("attempt", id, "termination_confirmed")
            termination = terminations[-1] if terminations else None
            proof(termination and termination in local and termination["payload"]["cause"] in {"failed", "finished"})
            proof(failure_error and failure_error["global_sequence"] > termination["global_sequence"])
            guard(termination["payload"]["cause"] != "finished" or failure_error["payload"]["code"] in {
                "output_missing", "output_contract_mismatch", "integrity_mismatch"})
        elif row.id == "AT08":
            proof(has("deadline_exceeded") and has("cancel_requested") and has("termination_confirmed", cause="timeout"))
            proof(any(d["global_sequence"] < c["global_sequence"] < t["global_sequence"] for d in local if d["event_type"] == "deadline_exceeded"
                      for c in local if c["event_type"] == "cancel_requested" for t in local if t["event_type"] == "termination_confirmed"))
        elif row.id == "AT09":
            proof(has("cancel_requested") and has("termination_confirmed", cause="cancel"))
            proof(any(c["global_sequence"] < t["global_sequence"] for c in local if c["event_type"] == "cancel_requested"
                      for t in local if t["event_type"] == "termination_confirmed"))
            guard(projection.state == "stopping")
        elif row.id == "AT11":
            proof(outcome == "found_running")
            guard(projection.state == "running" and not projection.cancel_requested and not projection.run_deadlines and
                  timestamp(event["recorded_at"]) < timestamp(a.intent["deadline_at"]))
        elif row.id == "AT13":
            proof(outcome == "found_failed" or (authoritative and projection.state == "running") or
                  (outcome == "found_stopped" and not deadline and not cancel))
            proof(failure_error and failure_error["global_sequence"] > before_observation)
            error = failure_error["payload"]
            guard(not authoritative or (error["code"] == "dispatch_not_executed" and error["retryability"] == "retryable"))
            if outcome == "found_stopped":
                guard(error["code"] == "execution_failed" and error["retryability"] == "unknown")
        elif row.id == "AT14":
            proof(outcome == "found_stopped" and deadline)
            guard(any(e["global_sequence"] < observed[-1]["global_sequence"] for e in
                      projection.history("attempt", id, "deadline_exceeded")))
        elif row.id == "AT15":
            proof((outcome == "found_stopped" and cancel and not deadline) or authoritative)
            if not authoritative:
                proof(any(c["global_sequence"] < observed[-1]["global_sequence"] for c in
                          projection.history("attempt", id, "cancel_requested") + projection.cancel_requested))
            guard(projection.state == "stopping")
    elif kind == "task":
        task = projection.tasks[id]
        a = projection.latest_attempt(id)
        report = projection.latest_report(id)
        evaluations = projection.task_evaluations(id)
        active_eval = bool(evaluations and (evaluations[-1].report is None or evaluations[-1].report["execution_status"] == "indeterminate"))
        def at(*numbers: str) -> bool:
            return bool(a and any(e["aggregate_type"] == "attempt" and e["aggregate_id"] == a.id and
                       e["event_type"] == "state_transition" and projection.transition_ids.get(e["event_id"]) in numbers
                       for e in evidence))
        can_retry = retry_allowed(projection, id, event["recorded_at"])
        if row.id == "TK01":
            guard(projection.state == "running" and not projection.cancel_requested)
            proof(all(projection.tasks[d].accepted_event_id in ids for d in task.spec["dependencies"]))
        elif row.id == "TK02":
            guard(projection.state == "stopping" and projection.failed_dependency(id))
        elif row.id == "TK03":
            proof(a and any(e["event_type"] == "dispatch_requested" and e["aggregate_id"] == a.id for e in evidence))
        elif row.id == "TK04":
            proof(any(e["event_type"] == "admission_rejected" and e["payload"]["error_record_ids"] for e in local))
            guard(all(r.state == "released" for r in projection.unassigned_reservations.values()))
        elif row.id == "TK05":
            proof(at("AT06"))
        elif row.id in {"TK06", "TK07", "TK12", "TK13"}:
            proof(at(*(("AT03", "AT07", "AT08") if state == "running" else ("AT13", "AT14"))))
            guard(can_retry if row.id in {"TK06", "TK12"} else not can_retry)
        elif row.id == "TK08":
            proof(at("AT05", "AT10"))
        elif row.id == "TK09":
            proof(at("AT04", "AT09"))
        elif row.id == "TK10":
            proof(at("AT11"))
        elif row.id == "TK11":
            proof(at("AT12") or (report and report["execution_status"] != "indeterminate" and any(e["event_type"] == "evaluation_report_recorded" and
                  e["aggregate_id"] == report["evaluation_id"] for e in evidence)))
        elif row.id == "TK14":
            proof(at("AT15"))
            guard(projection.state == "stopping")
        elif row.id == "TK15":
            guard(projection.reconciliation_expired(id, event["recorded_at"]))
        elif row.id == "TK16":
            proof(report and report["verdict"] == "pass" and task.accepted_event_id in ids and
                  any(e["event_type"] == "evaluation_report_recorded" and e["aggregate_id"] == report["evaluation_id"] for e in evidence))
            guard(projection.state == "running" and not projection.cancel_requested)
        elif row.id in {"TK17", "TK18", "TK19", "TK20"}:
            proof(report and any(e["event_type"] == "evaluation_report_recorded" and e["aggregate_id"] == report["evaluation_id"] for e in evidence))
            if row.id in {"TK17", "TK18"}:
                guard(report["verdict"] == "fail" and (can_retry if row.id == "TK17" else not can_retry))
            else:
                capacity = len(evaluations) < projection.limit("max_evaluations_per_attempt", id)
                guard(report["execution_status"] != "indeterminate" and report["verdict"] in {"inconclusive", "not_run"}
                      and (capacity if row.id == "TK19" else not capacity))
                if row.id == "TK19":
                    guard(projection.state == "running" and projection.time_remaining(event["recorded_at"]))
                else:
                    guard(p["reason"] == "evaluation_inconclusive")
        elif row.id in {"TK21", "TK22"}:
            proof(active_eval and any(e["event_type"] == "evaluation_requested" and e["aggregate_id"] == evaluations[-1].id for e in evidence))
            if row.id == "TK22":
                guard(projection.state == "running")
        elif row.id == "TK23":
            guard(can_retry and not projection.cancel_requested)
        elif row.id == "TK24":
            guard(projection.state == "stopping" and not active_eval and not task.accepted_event_id and
                  (not a or a.state in ATTEMPT_TERMINAL) and p["reason"] in {"run_cancelled", "workflow_failed"})
            guard(state != "pending" or not projection.failed_dependency(id))
    else:
        global_report = projection.global_report
        failed = any(t.state in {"failed", "blocked", "indeterminate"} for t in projection.tasks.values())
        conflict = any(e["code"] == "idempotency_conflict" for e in projection.errors.values())
        failure = failed or bool(projection.run_deadlines) or conflict
        cancel = bool(projection.cancel_requested)
        if row.id == "RN03":
            proof(has("run_manifest_closed", manifest_status="planning_failed"))
        elif row.id == "RN04":
            proof(has("run_manifest_closed", manifest_status="cancelled_before_ready") and has("cancel_requested"))
            guard(not projection.attempts and projection.cancel_requested[0]["global_sequence"] < projection.manifest_sequence)
        elif row.id in {"RN05", "RN06"}:
            proof(global_report and any(e["event_type"] == "evaluation_report_recorded" and e["aggregate_id"] == global_report["evaluation_id"] for e in evidence))
            guard(all(t.state == "succeeded" for t in projection.tasks.values()) and
                  ((global_report["verdict"] == "pass") == (row.id == "RN05")) and not cancel)
        elif row.id == "RN07":
            guard(failure or cancel)
            proof(any(e["event_type"] in {"deadline_exceeded", "cancel_requested"} and e["aggregate_type"] == "run"
                      or e["event_type"] == "error_attached" and e["payload"]["code"] == "idempotency_conflict"
                      or e["event_type"] == "state_transition" and e["aggregate_type"] == "task" and
                      e["payload"]["to"] in {"failed", "blocked", "indeterminate"} for e in evidence))
        elif row.id in {"RN08", "RN09"}:
            guard(not projection.active_work and all(t.state in TASK_TERMINAL for t in projection.tasks.values()))
            guard(not any(t.state == "indeterminate" for t in projection.tasks.values()))
            guard(projection.stopping_cause == ("failed" if row.id == "RN08" else "cancelled"))
        elif row.id == "RN10":
            guard(any(t.state == "indeterminate" for t in projection.tasks.values()) and
                  all(t.state in TASK_TERMINAL for t in projection.tasks.values()))
    return row
