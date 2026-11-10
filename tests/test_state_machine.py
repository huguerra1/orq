from copy import deepcopy

import pytest

from orq_contracts.projections import ProjectionError, project_journal, recovery_actions
from orq_contracts.state_machine import (
    ATTEMPT_TRANSITIONS, RUN_TRANSITIONS, TASK_TRANSITIONS, TRANSITIONS,
    StateMachineError, retry_allowed, validate_edge, validate_transition,
)
from test_projections import History, NOW, happy, rechain, signed


def stopping(h):
    h.emit("cancel_requested", {"reason": "human"})
    h.transition("run", h.run_id, "running", "stopping")


def cancel_attempt(h, aid, *, source="running"):
    stopping(h)
    if source == "running":
        h.emit("cancel_requested", {"reason": "run_cancelled"}, "attempt", aid)
        h.emit("termination_confirmed", {"cause": "cancel", "observed_at": h.now}, "attempt", aid)
    h.transition("attempt", aid, source, "cancelled")


def technical_failure(h, *, retry=True, source="running"):
    aid = h.dispatch()
    if source == "running":
        h.start(aid)
    h.fail(aid, source=source, retryable=retry)
    h.transition("task", "T1", "running", "retry_wait" if retry else "failed")
    return aid


def attempt_case(id):
    h = History().prepare()
    aid = h.dispatch()
    if id == "AT01":
        return h
    if id == "AT02":
        h.start(aid)
    elif id == "AT03":
        h.fail(aid, source="queued")
    elif id == "AT04":
        cancel_attempt(h, aid, source="queued")
    elif id == "AT05":
        h.unknown(aid, "queued")
    else:
        h.start(aid)
        if id == "AT06":
            h.finish(aid)
        elif id == "AT07":
            h.fail(aid)
        elif id == "AT08":
            h.now = "2026-11-10T12:01:03Z"
            h.emit("deadline_exceeded", {"deadline_at": "2026-11-10T12:01:02Z", "observed_at": h.now}, "attempt", aid)
            h.emit("cancel_requested", {"reason": "timeout"}, "attempt", aid)
            h.emit("termination_confirmed", {"cause": "timeout", "observed_at": h.now}, "attempt", aid)
            h.transition("attempt", aid, "running", "timed_out")
        elif id == "AT09":
            cancel_attempt(h, aid)
        else:
            if id == "AT14":
                h.now = "2026-11-10T12:01:03Z"
                h.emit("deadline_exceeded", {"deadline_at": "2026-11-10T12:01:02Z", "observed_at": h.now}, "attempt", aid)
            if id == "AT15":
                stopping(h)
            h.unknown(aid)
            if id == "AT11":
                h.reconcile(aid, "found_running")
                h.transition("attempt", aid, "unknown", "running")
            elif id == "AT12":
                h.reconcile(aid, "found_finished")
                h.finish(aid, source="unknown")
            elif id == "AT13":
                h.reconcile(aid, "found_failed")
                h.error(aid)
                h.transition("attempt", aid, "unknown", "failed")
            elif id == "AT14":
                h.reconcile(aid, "found_stopped")
                h.transition("attempt", aid, "unknown", "timed_out")
            elif id == "AT15":
                h.reconcile(aid, "found_stopped")
                h.transition("attempt", aid, "unknown", "cancelled")
    return h


def task_case(id, source=None):
    h = History(tasks=("T1", "T2") if id == "TK02" else ("T1",),
                max_evaluations=1 if id == "TK20" else 2).prepare()
    if id == "TK01":
        h.ready()
    elif id == "TK02":
        technical_failure(h, retry=False)
        h.transition("run", h.run_id, "running", "stopping")
        h.transition("task", "T2", "pending", "blocked")
    elif id == "TK03":
        h.dispatch()
    elif id == "TK04":
        h.ready()
        error = h.error("T1", "no_eligible_target", "non_retryable", kind="task")
        h.emit("admission_rejected", {"reason": "no_eligible_target", "error_record_ids": [error["payload"]["error_id"]]}, "task", "T1")
        h.transition("task", "T1", "ready", "failed")
    elif id in {"TK06", "TK07", "TK23"}:
        technical_failure(h, retry=id != "TK07")
        if id == "TK23":
            h.transition("task", "T1", "retry_wait", "ready")
    elif id == "TK24":
        if source == "ready":
            h.ready()
        elif source == "retry_wait":
            technical_failure(h)
        elif source in {"evaluating", "waiting_evaluation"}:
            aid = h.dispatch()
            h.start(aid)
            h.finish(aid)
            h.evaluate(verdict="pass" if source == "evaluating" else "inconclusive")
            if source == "waiting_evaluation":
                h.transition("task", "T1", "evaluating", "waiting_evaluation")
        stopping(h)
        h.transition("task", "T1", source, "cancelled", "run_cancelled")
    elif id == "TK09":
        aid = h.dispatch()
        h.start(aid)
        cancel_attempt(h, aid)
        h.transition("task", "T1", "running", "cancelled")
    elif id in {"TK08", "TK10", "TK11", "TK12", "TK13", "TK14", "TK15"}:
        aid = h.dispatch()
        h.start(aid)
        if id == "TK14":
            stopping(h)
        h.unknown(aid)
        if id == "TK10":
            h.reconcile(aid, "found_running")
            h.transition("attempt", aid, "unknown", "running")
            h.transition("task", "T1", "reconciling", "running")
        elif id == "TK11":
            h.reconcile(aid, "found_finished")
            h.finish(aid, source="unknown")
        elif id in {"TK12", "TK13"}:
            h.reconcile(aid, "found_failed")
            h.error(aid, retryability="retryable" if id == "TK12" else "non_retryable")
            h.transition("attempt", aid, "unknown", "failed")
            h.transition("task", "T1", "reconciling", "retry_wait" if id == "TK12" else "failed")
        elif id == "TK14":
            h.reconcile(aid, "found_stopped")
            h.transition("attempt", aid, "unknown", "cancelled")
            h.transition("task", "T1", "reconciling", "cancelled")
        elif id == "TK15":
            h.reconcile(aid, "unknown")
            h.now = "2026-11-10T12:00:18Z"
            h.transition("task", "T1", "reconciling", "indeterminate")
    else:
        aid = h.dispatch()
        h.start(aid)
        h.finish(aid)
        if id == "TK05":
            return h
        if id == "TK21":
            h.evaluate(report=False)
            h.transition("task", "T1", "evaluating", "reconciling")
        else:
            if id == "TK18":
                # Retain the snapshot/digest; change the policy before replay/creation.
                manifest = next(e for e in h.events if e["event_type"] == "run_manifest_closed")
                manifest["payload"]["resolved_limits"]["retry_policy"]["on_evaluation_fail"] = False
                manifest["payload"] = signed(manifest["payload"])
                rechain(h.events)
            h.evaluate(verdict="pass" if id == "TK16" else "fail" if id in {"TK17", "TK18"} else "inconclusive")
            if id == "TK16":
                h.accept()
            elif id in {"TK17", "TK18"}:
                h.transition("task", "T1", "evaluating", "retry_wait" if id == "TK17" else "failed")
            elif id in {"TK19", "TK22"}:
                h.transition("task", "T1", "evaluating", "waiting_evaluation")
                if id == "TK22":
                    h.evaluate(number=2, report=False)
            elif id == "TK20":
                h.transition("task", "T1", "evaluating", "failed", "evaluation_inconclusive")
    return h


def run_case(id):
    if id in {"RN01", "RN02"}:
        h = History()
        if id == "RN01":
            h.events.pop()
        return h
    if id == "RN03":
        h = History().prepare("planning_failed")
        h.transition("run", h.run_id, "running", "failed")
    elif id == "RN04":
        h = History()
        h.emit("cancel_requested", {"reason": "human"})
        h.prepare("cancelled_before_ready")
        h.transition("run", h.run_id, "running", "cancelled")
    elif id == "RN05":
        h = happy()
    elif id == "RN06":
        h = History().prepare()
        aid = h.dispatch()
        h.start(aid)
        h.finish(aid)
        h.evaluate()
        h.accept()
        h.evaluate(None, "fail")
        h.transition("run", h.run_id, "running", "failed")
    elif id == "RN07":
        h = History().prepare()
        stopping(h)
    elif id == "RN08":
        h = History().prepare()
        technical_failure(h, retry=False)
        h.transition("run", h.run_id, "running", "stopping")
        h.transition("run", h.run_id, "stopping", "failed")
    elif id == "RN09":
        h = History().prepare()
        stopping(h)
        h.transition("task", "T1", "pending", "cancelled", "run_cancelled")
        h.transition("run", h.run_id, "stopping", "cancelled")
    else:
        h = task_case("TK15")
        h.transition("run", h.run_id, "running", "stopping")
        h.transition("run", h.run_id, "stopping", "indeterminate")
    return h


@pytest.mark.parametrize("row", TRANSITIONS, ids=lambda r: r.id)
def test_every_specification_row_is_accepted_with_real_prefix(row):
    h = attempt_case(row.id) if row.id.startswith("AT") else task_case(row.id, "pending") if row.id.startswith("TK") else run_case(row.id)
    s = h.project()
    if row.id == "AT01":
        assert s.attempts["attempt-T1-1"].state == "queued"
    elif row.id == "RN01":
        assert s.state == "created"
    else:
        assert row.id in s.transition_ids.values()
        event = next(e for e in h.events if s.transition_ids.get(e["event_id"]) == row.id)
        before = h.project(as_of_global_sequence=event["global_sequence"] - 1)
        snapshot = deepcopy(before)
        assert validate_transition(before, event).id == row.id
        assert before == snapshot


@pytest.mark.parametrize("source", ["pending", "ready", "retry_wait", "waiting_evaluation", "evaluating"])
def test_tk24_all_from_states(source):
    h = task_case("TK24", source)
    assert h.project().tasks["T1"].state == "cancelled"


STATES = [(kind, state) for kind in ("run", "task", "attempt") for state in sorted({
    s for row in TRANSITIONS if row.aggregate_type == kind for s in (*row.from_states, row.to_state) if s is not None})]


@pytest.mark.parametrize("kind,state", STATES)
def test_invalid_jump_for_every_aggregate_state(kind, state):
    targets = {target for aggregate, target in STATES if aggregate == kind}
    allowed = {r.to_state for r in TRANSITIONS if r.aggregate_type == kind and state in r.from_states}
    for target in targets - allowed:
        with pytest.raises(StateMachineError) as caught:
            validate_edge(kind, state, state, target)
        assert caught.value.code == "transition_not_allowed"
    with pytest.raises(StateMachineError) as caught:
        validate_edge(kind, state, "not-current", state)
    assert caught.value.code == "transition_from_mismatch"


def test_tables_match_all_normative_ids_and_implicit_creation():
    for prefix, table, count in (("AT", ATTEMPT_TRANSITIONS, 15), ("TK", TASK_TRANSITIONS, 24), ("RN", RUN_TRANSITIONS, 10)):
        assert [r.id for r in table] == [f"{prefix}{i:02}" for i in range(1, count + 1)]
    for kind, target, event in (("run", "created", "run_intent_recorded"), ("attempt", "queued", "dispatch_requested")):
        with pytest.raises(StateMachineError, match="transition_not_allowed"):
            validate_edge(kind, None, None, target)
        assert validate_edge(kind, None, None, target, event_type=event)


@pytest.mark.parametrize("authoritative", [False, True])
def test_s32_s33_s57_not_found_requires_verified_control_and_evidence(authoritative):
    h = History().prepare()
    aid = h.dispatch()
    h.unknown(aid, "queued")
    h.reconcile(aid, "not_found")
    h.error(aid, "dispatch_not_executed")
    h.transition("attempt", aid, "unknown", "failed")
    if authoritative:
        s = h.project(reconcile_not_found_authoritative=True)
        assert s.attempts[aid].state == "failed"
        h.transition("task", "T1", "reconciling", "retry_wait")
        h.transition("task", "T1", "retry_wait", "ready")
        second = h.dispatch(number=2, ready=False)
        s = h.project(reconcile_not_found_authoritative=True)
        assert s.tasks["T1"].attempt_ids == [aid, second]
        assert s.attempts[second].state == "queued"
    else:
        with pytest.raises(ProjectionError, match="transition_evidence_missing"):
            h.project()


def test_s23_no_reevaluation_after_valid_fail():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    h.evaluate(verdict="fail")
    # Build the second intent from the first, preserving bindings.
    first = next(e for e in h.events if e["event_type"] == "evaluation_requested")
    p = deepcopy(first["payload"])
    p.update(evaluation_number=2, evaluation_id="evaluation-two", budget_reservation_ids=[])
    h.emit("evaluation_requested", p, "evaluation", "evaluation-two")
    with pytest.raises(ProjectionError, match="reevaluation_not_allowed"):
        h.project()


def test_s19_max_attempts_cannot_be_consumed_again():
    h = History(max_attempts=1).prepare()
    aid = h.dispatch()
    h.start(aid)
    h.fail(aid)
    h.transition("task", "T1", "running", "retry_wait")
    with pytest.raises(ProjectionError, match="transition_guard_failed"):
        h.project()


def test_s31_found_running_after_deadline_cannot_reopen_execution():
    h = attempt_case("AT10")
    h.now = "2026-11-10T12:01:03Z"
    h.reconcile("attempt-T1-1", "found_running")
    h.transition("attempt", "attempt-T1-1", "unknown", "running")
    with pytest.raises(ProjectionError, match="transition_guard_failed"):
        h.project()


def test_recovery_blocks_retry_and_preserves_reservations_when_unknown():
    h = task_case("TK15")
    s = h.project()
    assert not retry_allowed(s, "T1", NOW)
    assert s.reservations["reservation-attempt-T1-1"].state == "open"
    assert recovery_actions(s)[0].kind == "stop_run"


def test_evidence_must_be_cited():
    h = attempt_case("AT02")
    h.events[-1]["payload"]["evidence_event_ids"] = []
    rechain(h.events)
    with pytest.raises(ProjectionError, match="transition_evidence_missing"):
        h.project()


def test_evidence_cannot_be_borrowed_from_another_attempt():
    h = History().prepare()
    first = technical_failure(h)
    h.transition("task", "T1", "retry_wait", "ready")
    second = h.dispatch(number=2, ready=False)
    h.start(second)
    h.events[-1]["payload"]["evidence_event_ids"] = [e["event_id"] for e in h.events if
        e["aggregate_id"] == first and e["event_type"] in {"dispatch_acknowledged", "execution_started"}]
    rechain(h.events)
    with pytest.raises(ProjectionError, match="transition_evidence_missing"):
        h.project()


@pytest.mark.parametrize("row", ["AT07", "AT13"])
@pytest.mark.parametrize("error_case", ["old_unknown", "old_confirmed", "new_unknown", "uncited"])
def test_failure_requires_cited_confirmed_error_after_termination_or_observation(row, error_case):
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    if error_case.startswith("old"):
        h.error(aid, certainty="unknown" if error_case == "old_unknown" else "confirmed")
    if row == "AT07":
        h.emit("termination_confirmed", {"cause": "failed", "observed_at": h.now}, "attempt", aid)
    else:
        h.unknown(aid)
        h.reconcile(aid, "found_failed")
    if error_case in {"new_unknown", "uncited"}:
        h.error(aid, certainty="unknown" if error_case == "new_unknown" else "confirmed")
    evidence = [e["event_id"] for e in h.events if error_case != "uncited" or e["event_type"] != "error_attached"]
    h.transition("attempt", aid, "running" if row == "AT07" else "unknown", "failed", evidence=evidence)
    with pytest.raises(ProjectionError, match="transition_evidence_missing"):
        h.project()


@pytest.mark.parametrize("code,accepted", [("output_missing", True), ("output_contract_mismatch", True),
    ("integrity_mismatch", True), ("execution_failed", False), ("timeout", False)])
def test_at07_finished_requires_output_error(code, accepted):
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.emit("termination_confirmed", {"cause": "finished", "observed_at": h.now}, "attempt", aid)
    h.error(aid, code)
    h.transition("attempt", aid, "running", "failed")
    if accepted:
        assert h.project().attempts[aid].state == "failed"
    else:
        with pytest.raises(ProjectionError, match="transition_guard_failed"):
            h.project()


def test_retry_uses_error_recorded_in_failure_transition():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.fail(aid, retryable=False)
    h.error(aid, retryability="retryable")
    assert not retry_allowed(h.project(), "T1", h.now)


@pytest.mark.parametrize("later_event", ["run_cancel", "attempt_cancel", "deadline"])
def test_at13_found_stopped_ignores_stop_requests_after_observation(later_event):
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.unknown(aid)
    h.reconcile(aid, "found_stopped")
    if later_event == "run_cancel":
        stopping(h)
    elif later_event == "attempt_cancel":
        h.emit("cancel_requested", {"reason": "timeout"}, "attempt", aid)
    else:
        h.now = "2026-11-10T12:01:03Z"
        h.emit("deadline_exceeded", {"deadline_at": "2026-11-10T12:01:02Z", "observed_at": h.now}, "attempt", aid)
    h.error(aid, retryability="unknown")
    h.transition("attempt", aid, "unknown", "failed")
    assert "AT13" in h.project().transition_ids.values()


@pytest.mark.parametrize("code,retryability,accepted", [("execution_failed", "unknown", True),
    ("timeout", "unknown", False), ("execution_failed", "retryable", False),
    ("execution_failed", "non_retryable", False)])
def test_at13_found_stopped_requires_execution_failed_with_unknown_retryability(code, retryability, accepted):
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.unknown(aid)
    h.reconcile(aid, "found_stopped")
    h.error(aid, code, retryability)
    h.transition("attempt", aid, "unknown", "failed")
    if accepted:
        assert not retry_allowed(h.project(), "T1", h.now)
    else:
        with pytest.raises(ProjectionError, match="transition_guard_failed"):
            h.project()


def test_at14_requires_deadline_before_observation():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.unknown(aid)
    h.reconcile(aid, "found_stopped")
    h.now = "2026-11-10T12:01:03Z"
    h.emit("deadline_exceeded", {"deadline_at": "2026-11-10T12:01:02Z", "observed_at": h.now}, "attempt", aid)
    h.transition("attempt", aid, "unknown", "timed_out")
    with pytest.raises(ProjectionError, match="transition_evidence_missing"):
        h.project()


@pytest.mark.parametrize("ack", [False, True])
def test_at04_requires_stopping_and_no_acknowledgement(ack):
    h = History().prepare()
    aid = h.dispatch()
    if ack:
        h.emit("dispatch_acknowledged", {"external_call_id": "call-" + aid}, "attempt", aid)
        stopping(h)
    else:
        h.emit("cancel_requested", {"reason": "human"})
    h.transition("attempt", aid, "queued", "cancelled")
    with pytest.raises(ProjectionError, match="transition_guard_failed"):
        h.project()


def test_at05_crash_cannot_ignore_existing_acknowledgement():
    h = History().prepare()
    aid = h.dispatch()
    h.emit("dispatch_acknowledged", {"external_call_id": "call-" + aid}, "attempt", aid)
    h.unknown(aid, "queued")
    with pytest.raises(ProjectionError, match="transition_guard_failed"):
        h.project()


def test_s31_ack_lost_queued_unknown_running_uses_same_intent():
    h = History().prepare()
    aid = h.dispatch()
    h.error(aid, "dispatch_failed", certainty="unknown")
    h.unknown(aid, "queued", reason="dispatch_uncertain")
    h.reconcile(aid, "found_running")
    h.transition("attempt", aid, "unknown", "running")
    h.transition("task", "T1", "reconciling", "running")
    s = h.project()
    assert s.attempts[aid].state == "running" and s.tasks["T1"].state == "running"
    assert list(s.attempts) == [aid]
    assert sum(e["event_type"] == "dispatch_requested" for e in h.events) == 1


@pytest.mark.parametrize("dependency_state", ["failed", "blocked", "indeterminate"])
def test_tk24_pending_with_failed_dependency_must_be_blocked(dependency_state):
    h = History(tasks=("T1", "T2", "T3")).prepare()
    if dependency_state == "indeterminate":
        aid = h.dispatch()
        h.unknown(aid, "queued")
        h.reconcile(aid, "unknown")
        h.now = "2026-11-10T12:00:18Z"
        h.transition("task", "T1", "reconciling", "indeterminate")
    else:
        technical_failure(h, retry=False)
    h.transition("run", h.run_id, "running", "stopping")
    task = "T2"
    if dependency_state == "blocked":
        h.transition("task", "T2", "pending", "blocked")
        task = "T3"
    h.transition("task", task, "pending", "cancelled", "workflow_failed")
    with pytest.raises(ProjectionError, match="transition_guard_failed"):
        h.project()
    h.events.pop()
    h.transition("task", task, "pending", "blocked")
    assert h.project().tasks[task].state == "blocked"


@pytest.mark.parametrize("cause", ["cancelled", "failed"])
@pytest.mark.parametrize("target", ["cancelled", "failed"])
def test_rn08_rn09_use_cause_at_rn07_despite_later_failure_or_cancel(cause, target):
    h = History().prepare()
    if cause == "cancelled":
        aid = h.dispatch()
        h.start(aid)
        stopping(h)
        h.fail(aid, retryable=False)
        h.transition("task", "T1", "running", "failed")
    else:
        technical_failure(h, retry=False)
        h.transition("run", h.run_id, "running", "stopping")
        h.emit("cancel_requested", {"reason": "human"})
    h.transition("run", h.run_id, "stopping", target)
    if target == cause:
        assert h.project().state == target and h.project().stopping_cause == cause
    else:
        with pytest.raises(ProjectionError, match="transition_guard_failed"):
            h.project()


def test_tk20_requires_evaluation_inconclusive_reason():
    h = task_case("TK20")
    h.events[-1]["payload"]["reason"] = "test"
    rechain(h.events)
    with pytest.raises(ProjectionError, match="transition_guard_failed"):
        h.project()


@pytest.mark.parametrize("missing", ["capacity", "time", "running"])
def test_tk19_requires_remaining_capacity_time_and_running_run(missing):
    h = History(max_evaluations=1 if missing == "capacity" else 2).prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    h.evaluate(verdict="inconclusive")
    if missing == "time":
        h.now = "2099-01-01T00:00:00Z"
    elif missing == "running":
        stopping(h)
    h.transition("task", "T1", "evaluating", "waiting_evaluation")
    with pytest.raises(ProjectionError, match="transition_guard_failed"):
        h.project()


@pytest.mark.parametrize("allowed", [False, True])
def test_confirmed_timeout_retry_follows_on_confirmed_timeout(allowed):
    h = attempt_case("AT08")
    manifest = next(e for e in h.events if e["event_type"] == "run_manifest_closed")
    manifest["payload"]["resolved_limits"]["retry_policy"]["on_confirmed_timeout"] = allowed
    manifest["payload"] = signed(manifest["payload"])
    rechain(h.events)
    assert retry_allowed(h.project(), "T1", h.now) is allowed
    h.transition("task", "T1", "running", "retry_wait" if allowed else "failed")
    assert h.project().tasks["T1"].state == ("retry_wait" if allowed else "failed")
    if allowed:
        h.transition("task", "T1", "retry_wait", "ready")
        second = h.dispatch(number=2, ready=False)
        assert h.project().attempts[second].state == "queued"
