"""Sequências de fatos locais; nenhum executor/avaliador é executado aqui."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from conftest import load_fixture
from orq_contracts.canonical import canonical_digest
from orq_contracts.journal import RunJournal
from orq_contracts import journal as journal_module
from orq_contracts.planner import FixedTemplatePlanner, plan_and_validate
from orq_contracts.projections import ProjectionError, _dispatch, project_journal, record_ref, recovery_actions
from orq_contracts.router import route_and_finalize
from orq_contracts.state_machine import StateMachineError, timestamp
from orq_contracts.validator import validate_instance


NOW = "2026-11-10T12:00:02Z"


def ref(id):
    return {"id": id, "version": "1", "content_digest": canonical_digest(id)}


def signed(record):
    result = deepcopy(record)
    result["record_hash"] = canonical_digest({k: v for k, v in result.items() if k != "record_hash"})
    return result


class History:
    """Monta eventos canônicos, sem validar a semântica que está sob teste."""

    def __init__(self, *, tasks=("T1",), max_attempts=2, max_evaluations=2, cap=False):
        self.events = []
        self.now = NOW
        self.workflow = load_fixture("valid/workflow-minimal.json")
        base = self.workflow["tasks"][0]
        base["evaluation_criteria"][0].update(method="output_binding_present", pass_rule={"output_ids": ["report"]})
        base["constraints"]["max_attempts"] = max_attempts
        self.workflow["tasks"] = [dict(deepcopy(base), task_id=t) for t in tasks]
        self.workflow["requirement_coverage"][0]["task_id"] = tasks[0]
        for n, task in enumerate(self.workflow["tasks"]):
            if n:
                task["dependencies"] = [tasks[n-1]]
                task["inputs"] = [{"input_id": "previous", "artifact_type": "report", "source": {
                    "kind": "task_output", "task_id": tasks[n-1], "output_id": "report"}}]
        self.workflow["outputs"][0]["source_task_id"] = tasks[-1]
        self.workflow["evaluation_criteria"][0].update(method="output_binding_present", pass_rule={"output_ids": ["final-report"]})
        self.wref = {"id": self.workflow["workflow_id"], "version": self.workflow["version"],
                     "content_digest": canonical_digest(self.workflow)}
        intent = load_fixture("valid/run-intent.json")
        intent["objective"] = self.workflow["objective"]
        self.intent = signed(intent)
        self.run_id = intent["run_id"]
        self.catalog = load_fixture("routing-target-catalog.json")
        self.policy = load_fixture("routing-policy-rules.json")
        self.manifest = load_fixture("valid/run-manifest.json")
        self.manifest.update(run_intent_ref=record_ref(self.intent, "run_id"), workflow_snapshot_ref=self.wref,
            objective_snapshot={"objective": intent["objective"], "objective_hash": canonical_digest(intent["objective"])},
            routing_catalog_ref={"id": self.catalog["catalog_id"], "version": self.catalog["version"],
                                 "content_digest": self.catalog["catalog_hash"]},
            routing_policy_ref={"id": self.policy["routing_policy_id"], "version": self.policy["version"],
                                "content_digest": canonical_digest(self.policy)})
        self.manifest["resolved_limits"].update(max_attempts=max_attempts, max_evaluations_per_attempt=max_evaluations)
        self.manifest["budget_snapshot"].update(cap_mandatory=cap, max_cost="10.00" if cap else None)
        self.manifest["executor_snapshot"]["executor_ref"] = self.catalog["targets"][1]["executor_ref"]
        target = self.catalog["targets"][1]
        self.manifest["budget_snapshot"]["cost_estimates"][0]["subject_ref"] = {
            "id": target["target_id"], "version": target["version"], "content_digest": canonical_digest(target)}
        self.emit("run_intent_recorded", self.intent)
        self.transition("run", self.run_id, "created", "running")

    def emit(self, type, payload, kind="run", id=None, key=None):
        id = id or self.run_id
        seq = len(self.events) + 1
        event = {"schema_version": "0.1", "event_id": f"event-{seq}", "run_id": self.run_id,
            "global_sequence": seq, "aggregate_type": kind, "aggregate_id": id,
            "aggregate_sequence": 1 + sum(e["aggregate_type"] == kind and e["aggregate_id"] == id for e in self.events),
            "event_type": type, "recorded_at": self.now, "occurred_at": None,
            "actor": {"kind": "orchestrator", "ref": "orq"}, "correlation_id": id,
            "causation_event_id": self.events[-1]["event_id"] if self.events else None,
            "idempotency_key": key, "payload": deepcopy(payload),
            "previous_event_hash": self.events[-1]["event_hash"] if self.events else None}
        event["event_hash"] = canonical_digest(event)
        self.events.append(event)
        return event

    def transition(self, kind, id, source, target, reason="test", evidence=None):
        return self.emit("state_transition", {"from": source, "to": target, "reason": reason,
            "evidence_event_ids": [e["event_id"] for e in self.events] if evidence is None else evidence}, kind, id)

    def prepare(self, status="ready"):
        request = load_fixture("planning-request.json")
        request.update(run_id=self.run_id, objective=self.workflow["objective"], workflow_template_ref=self.wref)
        if status == "planning_failed":
            request["objective"] = "Outro objetivo"
        plan = plan_and_validate(FixedTemplatePlanner([self.workflow], ref("planner")), request, completed_at=NOW).record
        self.emit("planning_recorded", plan)
        if status != "ready":
            self.manifest = load_fixture(f"valid/run-manifest-{status.replace('_', '-')}.json")
            self.manifest.update(run_intent_ref=record_ref(self.intent, "run_id"),
                objective_snapshot={"objective": self.intent["objective"], "objective_hash": canonical_digest(self.intent["objective"])})
        self.manifest["planning_record_ref"] = record_ref(plan, "planning_record_id")
        self.emit("run_manifest_closed", signed(self.manifest))
        return self

    def ready(self, task="T1"):
        return self.transition("task", task, "pending", "ready")

    def reserve(self, scope, id, amount=None):
        rid = "reservation-" + id
        self.emit("budget_reserved", {"reservation_id": rid, "scope_type": scope, "scope_ref": id,
            "amount": amount, "status": "estimated" if amount is not None else "unavailable",
            "currency": "USD", "method": "fixture"})
        return rid

    def dispatch(self, task="T1", number=1, *, ready=True):
        if ready:
            self.ready(task)
        spec = next(t for t in self.workflow["tasks"] if t["task_id"] == task)
        request = load_fixture("routing-request.json")
        request.update(run_id=self.run_id, workflow_ref=self.wref, task_snapshot=spec,
            candidate_attempt_number=number, created_at=self.now,
            execution_policy_snapshot_ref=self.manifest["execution_policy_snapshot_ref"])
        request["routing_request_id"] = f"routing-{task}-{number}"
        decision = route_and_finalize(request, self.catalog, self.policy,
            preflight_checks=[{"control": "limits", "status": "passed", "observed_at": self.now, "evidence_refs": []}])
        self.emit("routing_decision_recorded", decision, "task", task)
        selected = next(c["target_ref"] for c in decision["candidates"] if c["target_ref"]["id"] == decision["effective_selection"]["selected_target_id"])
        context = {"schema_version": "0.1", "context_manifest_id": f"context-{task}-{number}",
            "run_id": self.run_id, "workflow_ref": self.wref, "task_id": task, "candidate_attempt_number": number,
            "routing_decision_ref": record_ref(decision, "routing_decision_id"), "knowledge_selection_refs": [],
            "target_ref": selected, "created_at": self.now, "items": [], "omitted_items": [],
            "budget": {"target_context_limit_tokens": 1000, "reserved_output_tokens": 100,
                "available_input_tokens": 900, "materialized_input_tokens": 0, "tokenizer_ref": "fixture",
                "margin_tokens": 10, "status": "estimated"},
            "bundle_hash": canonical_digest([]), "provider_context_coverage": "platform_only"}
        self.emit("context_manifest_recorded", context, "task", task)
        aid = f"attempt-{task}-{number}"
        rid = self.reserve("attempt", aid, "0.30" if self.manifest["budget_snapshot"]["cap_mandatory"] else None)
        inputs = []
        if spec["dependencies"]:
            dep = spec["dependencies"][0]
            accepted = next(e for e in reversed(self.events) if e["event_type"] == "outputs_accepted" and e["aggregate_id"] == dep)
            inputs = [{"input_id": "previous", "artifact": accepted["payload"]["output_bindings"][0]["artifact"],
                       "basis": {"kind": "outputs_accepted", "event_id": accepted["event_id"]}}]
        h = load_fixture("valid/attempt-header.json")
        h.update(attempt_id=aid, task_id=task, attempt_number=number, previous_attempt_id=f"attempt-{task}-{number-1}" if number > 1 else None,
            workflow_ref=self.wref, routing_decision_ref=record_ref(decision, "routing_decision_id"),
            context_manifest_ref=record_ref(context, "context_manifest_id"), input_bindings=inputs,
            dispatch_intent_id=f"dispatch-{task}-{number}", dispatch_idempotency_key=f"{self.run_id}/dispatch-{task}-{number}",
            created_at=self.now, deadline_at=(timestamp(self.now) + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"),
            budget_reservation_ids=[rid])
        target = next(t for t in self.catalog["targets"] if t["target_id"] == selected["id"])
        h["execution_target_snapshot"] = {k: target[k] for k in ("model_ref", "provider_ref", "runtime_ref", "executor_ref", "execution_config_ref")}
        h["execution_target_snapshot"]["target_ref"] = selected
        d = load_fixture("valid/dispatch-intent.json")
        for k in d:
            if k in h:
                d[k] = deepcopy(h[k])
        d.update(target_ref=selected, executor_ref=target["executor_ref"], bundle_hash=context["bundle_hash"])
        d["admission_checks"] = [dict(c, observed_at=self.now) for c in d["admission_checks"]]
        self.emit("dispatch_requested", {"attempt_header": signed(h), "dispatch_intent": signed(d)}, "attempt", aid,
                  key=d["dispatch_idempotency_key"])
        self.transition("task", task, "ready", "running")
        return aid

    def start(self, aid):
        self.emit("dispatch_acknowledged", {"external_call_id": "call-" + aid}, "attempt", aid)
        self.emit("execution_started", {"observed_at": self.now}, "attempt", aid)
        self.transition("attempt", aid, "queued", "running")

    def finish(self, aid, *, source="running"):
        if source == "running":
            self.emit("termination_confirmed", {"cause": "finished", "observed_at": self.now}, "attempt", aid)
        self.emit("result_collected", {"requested_model_id": "requested", "resolved_model_id": None,
                  "provider_context_coverage": "unknown"}, "attempt", aid)
        task = next(e["payload"]["attempt_header"]["task_id"] for e in self.events if e["event_type"] == "dispatch_requested" and e["aggregate_id"] == aid)
        spec = next(t for t in self.workflow["tasks"] if t["task_id"] == task)["expected_output"][0]
        descriptor = {"schema_version": "0.1", "artifact_id": "artifact-" + aid, "artifact_type": "report",
            "media_type": "application/json", "content_contract_ref": spec["content_contract_ref"],
            "content_digest": canonical_digest(aid), "size_bytes": 5,
            "locator": "orq-store:sha256/" + canonical_digest(aid)["value"], "created_at": self.now,
            "producer": {"kind": "attempt_output", "run_id": self.run_id, "task_id": task, "attempt_id": aid},
            "derived_from": [], "data_classification": "internal"}
        self.emit("artifact_ingested", descriptor, "attempt", aid)
        self.emit("output_bound", {"output_id": "report", "artifact": {"artifact_id": descriptor["artifact_id"],
                  "content_digest": descriptor["content_digest"]}}, "attempt", aid)
        self.transition("attempt", aid, source, "completed")
        self.transition("task", task, "running" if source == "running" else "reconciling", "evaluating")

    def error(self, aid, code="execution_failed", retryability="retryable", certainty="confirmed", kind="attempt"):
        p = load_fixture("valid/error-record.json")
        p.update(error_id=f"error-{len(self.events)}", scope_type=kind, scope_ref=aid,
                 code=code, retryability=retryability, termination_certainty=certainty)
        return self.emit("error_attached", signed(p), kind, aid)

    def fail(self, aid, *, source="running", retryable=True):
        if source == "running":
            self.emit("termination_confirmed", {"cause": "failed", "observed_at": self.now}, "attempt", aid)
        self.error(aid, "dispatch_failed" if source == "queued" else "execution_failed",
                   "retryable" if retryable else "non_retryable")
        return self.transition("attempt", aid, source, "failed")

    def unknown(self, aid, source="running", reason="crash"):
        self.transition("attempt", aid, source, "unknown", reason)
        task = next(e["payload"]["attempt_header"]["task_id"] for e in self.events if e["event_type"] == "dispatch_requested" and e["aggregate_id"] == aid)
        self.transition("task", task, "running", "reconciling")

    def reconcile(self, aid, outcome):
        d = next(e["payload"]["dispatch_intent"] for e in self.events if e["event_type"] == "dispatch_requested" and e["aggregate_id"] == aid)
        call = "call-" + aid if any(e["event_type"] == "dispatch_acknowledged" and e["aggregate_id"] == aid for e in self.events) else None
        previous = next((e for e in self.events if e["event_type"] == "reconciliation_requested" and e["aggregate_id"] == aid), None)
        deadline = previous["payload"]["deadline_at"] if previous else min(
            timestamp(self.now) + timedelta(milliseconds=self.manifest["resolved_limits"]["reconciliation_timeout_ms"]),
            timestamp(self.intent["created_at"]) + timedelta(milliseconds=self.manifest["resolved_limits"]["max_run_duration_ms"])
        ).isoformat().replace("+00:00", "Z")
        self.emit("reconciliation_requested", {"dispatch_idempotency_key": d["dispatch_idempotency_key"],
                  "external_call_id": call, "deadline_at": deadline},
                  "attempt", aid, key=f"{d['dispatch_idempotency_key']}/reconcile/{len(self.events)}")
        return self.emit("reconciliation_observed", {"outcome": outcome, "external_call_id": call,
                         "evidence_refs": [ref("executor-observation")]}, "attempt", aid)

    def evaluate(self, task="T1", verdict="pass", number=1, *, report=True):
        s = self.project()
        if task is None:
            id, scope, bindings, criteria = "evaluation-workflow-1", {"kind": "workflow"}, s.output_bindings, self.workflow["evaluation_criteria"]
        else:
            a = s.latest_attempt(task)
            id = f"evaluation-{a.id}-{number}"
            scope, bindings, criteria = {"kind": "task_attempt", "task_id": task, "attempt_id": a.id}, a.output_bindings, s.tasks[task].spec["evaluation_criteria"]
        rid = self.reserve("evaluation", id)
        p = {"evaluation_id": id, "evaluation_number": number, "scope": scope, "subject_bindings": bindings,
            "criteria_snapshot": {"criteria": criteria, "criteria_hash": canonical_digest(criteria)},
            "evaluator_snapshot": self.manifest["evaluator_snapshot"], "deadline_at": self.now, "budget_reservation_ids": [rid]}
        self.emit("evaluation_requested", p, "evaluation", id)
        if task is not None and number > 1:
            self.transition("task", task, "waiting_evaluation", "evaluating")
        if not report:
            return id
        results = [{"criterion_id": c["criterion_id"], "required": c["required"],
            "requirement_ids": c.get("requirement_ids", c.get("workflow_requirement_ids")), "method": c["method"],
            "status": verdict, "evidence_refs": [], "observations": [], "error_record_ids": []} for c in criteria]
        payload = load_fixture("valid/evaluation-report.json")
        payload.update({k: p[k] for k in ("evaluation_id", "evaluation_number", "scope", "subject_bindings", "criteria_snapshot", "evaluator_snapshot")})
        payload.update(criterion_results=results, verdict=verdict, execution_status="failed" if verdict == "not_run" else "completed")
        self.emit("evaluation_report_recorded", signed(payload), "evaluation", id)
        self.emit("budget_released", {"reservation_id": rid, "reason": "evaluation_completed"})
        return id

    def accept(self, task="T1"):
        s = self.project()
        self.emit("outputs_accepted", {"task_id": task, "evaluation_id": s.latest_report(task)["evaluation_id"],
                  "output_bindings": s.latest_attempt(task).output_bindings}, "task", task)
        self.transition("task", task, "evaluating", "succeeded")

    def usage(self, aid, status="reported", amount="0.20", *, supersedes=None):
        p = load_fixture("valid/usage-record.json")
        p.update(usage_id=f"usage-{len(self.events)}", scope_ref=aid, supersedes_usage_id=supersedes)
        p["cost_lines"][0].update(status=status, amount=None if status == "unavailable" else amount)
        p["coverage"] = {s: ["cost"] if s == status else [] for s in ("reported", "estimated", "unavailable")}
        self.emit("usage_attached", signed(p), "attempt", aid)
        return p["usage_id"]

    def close(self):
        s = self.project()
        summary = {"state": s.state, "tasks": [{"task_id": t.id, "state": t.state} for t in s.tasks.values()],
            "output_bindings": s.output_bindings, "usage_record_ids": list(s.usages), "error_record_ids": list(s.errors),
            "cost_summary": s.cost_summary, "limitations": [], "unknowns": []}
        self.emit("run_closed", {"as_of_global_sequence": len(self.events), "event_hash": self.events[-1]["event_hash"],
                  "run_summary": summary, "summary_hash": canonical_digest(summary)})

    def project(self, **kwargs):
        return project_journal(self.events, workflow=self.workflow, **kwargs)


def rechain(events):
    """Recalcula envelopes após mutação sem mascarar o defeito semântico testado."""
    for i, event in enumerate(events):
        event["previous_event_hash"] = events[i-1]["event_hash"] if i else None
        event["event_hash"] = canonical_digest({k: v for k, v in event.items() if k != "event_hash"})


def happy(*, tasks=("T1",), cap=False):
    h = History(tasks=tasks, cap=cap).prepare()
    for t in tasks:
        aid = h.dispatch(t)
        h.start(aid)
        h.finish(aid)
        uid = h.usage(aid)
        h.emit("budget_settled", {"reservation_id": "reservation-" + aid, "usage_ids": [uid]})
        h.evaluate(t)
        h.accept(t)
    h.evaluate(None)
    h.transition("run", h.run_id, "running", "succeeded")
    h.close()
    return h


def test_s01_s04_s05_happy_prefix_and_pure_cutoff():
    h = happy()
    assert all(not validate_instance(e, "journal_event") for e in h.events)
    s = h.project()
    assert s.state == "succeeded" and s.published_summary["cost_summary"]["reported"] == "0.20"
    assert s.attempts["attempt-T1-1"].resolved_model_id is None
    assert s.tasks["T1"].accepted_bindings[0]["artifact"] == s.output_bindings[0]["artifact"]
    cutoff = next(e["global_sequence"] for e in h.events if e["aggregate_type"] == "task" and
                  e["event_type"] == "state_transition" and e["payload"]["to"] == "evaluating")
    prefix = h.project(as_of_global_sequence=cutoff)
    assert prefix.tasks["T1"].state == "evaluating" and not prefix.tasks["T1"].accepted_bindings
    before = deepcopy(h.events)
    prefix.tasks["T1"].spec["objective"] = "mutação"
    assert h.events == before
    assert recovery_actions(s)[0].kind == "closed"


@pytest.mark.parametrize("tasks", [("T1",), ("T1", "T2", "T3")])
def test_run_journal_round_trip(tmp_path, tasks):
    h = happy(tasks=tasks)
    with RunJournal.create(tmp_path, h.run_id, clock=lambda: h.now) as journal:
        for e in h.events:
            journal.append(**{k: e[k] for k in ("event_type", "aggregate_type", "aggregate_id", "payload", "actor",
                "correlation_id", "causation_event_id", "idempotency_key", "occurred_at")},
                expected_aggregate_sequence=e["aggregate_sequence"])
        assert h.project().published_summary == project_journal(journal.events, workflow=h.workflow).published_summary
    with RunJournal.open(tmp_path, h.run_id) as journal:
        assert project_journal(journal.events, workflow=h.workflow).state == "succeeded"


def test_s34_fsync_failure_intent_reappears_but_never_authorizes_dispatch(tmp_path, monkeypatch):
    h = History().prepare()
    aid = h.dispatch()
    journal = RunJournal.create(tmp_path, h.run_id, clock=lambda: h.now)
    for e in h.events:
        kwargs = {k: e[k] for k in ("event_type", "aggregate_type", "aggregate_id", "payload", "actor",
            "correlation_id", "causation_event_id", "idempotency_key", "occurred_at")}
        if e["event_type"] == "dispatch_requested":
            def fail_sync(_):
                raise OSError("falha injetada de fsync")
            with monkeypatch.context() as patch:
                patch.setattr(journal_module.os, "fsync", fail_sync)
                with pytest.raises(OSError, match="fsync"):
                    journal.append(**kwargs, expected_aggregate_sequence=e["aggregate_sequence"])
            break
        journal.append(**kwargs, expected_aggregate_sequence=e["aggregate_sequence"])
    journal.close()
    # O harness confirma o fim do escritor e simula a remoção manual do lock órfão.
    (journal.directory / "run.lock").unlink()
    with RunJournal.open(tmp_path, h.run_id) as reopened:
        s = project_journal(reopened.events, workflow=h.workflow)
        assert s.attempts[aid].state == "queued" and s.tasks["T1"].state == "ready"
        action, = recovery_actions(s)
        assert action.kind == "reconcile_dispatch" and action.details["task_transition"] == "ready_to_running"


def test_s02_s17_retry_t3_and_only_winning_bindings_feed_t4():
    h = History(tasks=("T3", "T4")).prepare()
    a1 = h.dispatch("T3")
    h.start(a1)
    h.finish(a1)
    h.evaluate("T3", "fail")
    h.transition("task", "T3", "evaluating", "retry_wait")
    h.transition("task", "T3", "retry_wait", "ready")
    a2 = h.dispatch("T3", 2, ready=False)
    h.start(a2)
    h.finish(a2)
    h.evaluate("T3")
    h.accept("T3")
    a4 = h.dispatch("T4")
    s = h.project()
    assert s.tasks["T3"].attempt_ids == [a1, a2]
    assert s.attempts[a2].header["previous_attempt_id"] == a1
    assert s.attempts[a4].intent["input_bindings"][0]["artifact"] == s.attempts[a2].output_bindings[0]["artifact"]
    assert s.tasks["T3"].accepted_bindings != s.attempts[a1].output_bindings


def test_s28_s29_timeout_reconciles_without_second_dispatch():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.now = "2026-11-10T12:01:03Z"
    h.emit("deadline_exceeded", {"deadline_at": "2026-11-10T12:01:02Z", "observed_at": h.now}, "attempt", aid)
    h.emit("cancel_requested", {"reason": "timeout"}, "attempt", aid)
    h.error(aid, "timeout", certainty="unknown")
    h.unknown(aid, reason="timeout")
    assert recovery_actions(h.project())[0].kind == "reconcile_dispatch"
    h.reconcile(aid, "found_finished")
    h.finish(aid, source="unknown")
    s = h.project()
    assert s.tasks["T1"].state == "evaluating"
    assert sum(e["event_type"] == "dispatch_requested" for e in h.events) == 1


@pytest.mark.parametrize("started", [False, True])
def test_s34_intention_without_proven_effect_always_reconciles(started):
    h = History().prepare()
    aid = h.dispatch()
    if started:
        h.start(aid)
    s = h.project()
    assert bool(s.pending_dispatch_intents) is not started
    actions = recovery_actions(s)
    assert actions[0].kind == "reconcile_dispatch"
    assert actions[0].details["dispatch_idempotency_key"] == s.attempts[aid].intent["dispatch_idempotency_key"]
    assert len(actions) == 1 and actions[0].aggregate_id == aid
    assert actions[0].details["transition_to"] == "unknown"
    assert sum(e["event_type"] == "dispatch_requested" for e in h.events) == 1


def test_s35_release_orphan_without_consuming_attempt():
    h = History().prepare()
    h.reserve("attempt", "attempt-T1-1")
    s = h.project()
    assert not s.attempts
    assert recovery_actions(s)[0].details == {"reason": "admission_aborted"}


def test_s40_interrupted_local_evaluation_gets_not_run_action():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    id = h.evaluate(report=False)
    actions = recovery_actions(h.project())
    assert len(actions) == 1 and actions[0].aggregate_id == id
    assert actions[0].details == {"execution_status": "failed", "verdict": "not_run"}


@pytest.mark.parametrize("cap", [False, True])
def test_s41_s43_s45_unknown_cost_and_reservation_not_double_counted(cap):
    h = History(cap=cap).prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    uid = h.usage(aid, "unavailable")
    h.emit("budget_settled", {"reservation_id": "reservation-" + aid, "usage_ids": [uid]})
    s = h.project()
    assert s.cost_summary == {"currency": "USD", "reported": "0", "estimated": "0",
        "unavailable_count": 1, "total_cost_exact": False}
    if cap:
        assert s.budget_committed == Decimal("0.30")
    else:
        assert s.budget_committed is None
        assert ("attempt", aid) in s.budget_gaps
    h.usage(aid, supersedes=uid)
    assert h.project().cost_summary["reported"] == "0.20"
    assert h.project().budget_committed == Decimal("0.20")


def test_s47_late_usage_preserves_published_summary_and_cutoff():
    h = happy()
    before = h.project()
    h.usage("attempt-T1-1", "unavailable", supersedes=next(iter(before.usages)))
    s = h.project()
    assert s.state == before.state and s.published_summary == before.published_summary
    assert s.published_cutoff == before.published_cutoff
    assert s.cost_summary["unavailable_count"] == 1
    assert h.project(as_of_global_sequence=s.published_cutoff).cost_summary["unavailable_count"] == 0


def test_s48_cancel_before_ready_and_s54_cancel_before_dispatch_effect():
    h = History()
    h.emit("cancel_requested", {"reason": "human"})
    h.prepare("cancelled_before_ready")
    h.transition("run", h.run_id, "running", "cancelled")
    h.close()
    assert h.project().published_summary["cost_summary"]["currency"] is None
    h = History().prepare()
    aid = h.dispatch()
    h.emit("cancel_requested", {"reason": "human"})
    assert recovery_actions(h.project())[0].kind == "stop_run"
    h.transition("run", h.run_id, "running", "stopping")
    assert recovery_actions(h.project())[0].kind == "reconcile_dispatch"
    h.transition("attempt", aid, "queued", "cancelled")
    h.transition("task", "T1", "running", "cancelled")
    h.transition("run", h.run_id, "stopping", "cancelled")
    h.close()
    assert h.project().state == "cancelled"


@pytest.mark.parametrize("mutation,code", [
    (lambda p: p.update(**{"from": "failed"}), "transition_from_mismatch"),
    (lambda p: p.update(to="succeeded"), "transition_not_allowed"),
    (lambda p: p.update(evidence_event_ids=["event-999"]), "transition_evidence_missing"),
])
def test_s39_transition_invariants_have_stable_codes(mutation, code):
    h = History().prepare()
    h.ready()
    mutation(h.events[-1]["payload"])
    rechain(h.events)
    with pytest.raises(ProjectionError) as caught:
        h.project()
    assert caught.value.code == code and caught.value.event_id == h.events[-1]["event_id"]


@pytest.mark.parametrize("field,code", [
    ("target", "target_mismatch"), ("bundle", "bundle_hash_mismatch"),
    ("number", "attempt_number_not_contiguous"),
    ("key", "dispatch_key_mismatch"), ("header", "dispatch_header_mismatch"),
    ("previous", "event_schema_invalid"),
])
def test_dispatch_invariants(field, code):
    h = History().prepare()
    aid = h.dispatch()
    e = next(e for e in h.events if e["event_type"] == "dispatch_requested")
    d, a = e["payload"]["dispatch_intent"], e["payload"]["attempt_header"]
    if field == "target":
        a["execution_target_snapshot"]["target_ref"] = ref("other")
    elif field == "bundle":
        d["bundle_hash"] = canonical_digest("wrong")
    elif field == "number":
        d["attempt_number"] = a["attempt_number"] = 3
        a["previous_attempt_id"] = "attempt-previous"
    elif field == "key":
        d["dispatch_idempotency_key"] = a["dispatch_idempotency_key"] = "wrong"
    elif field == "header":
        a["deadline_at"] = NOW
    else:
        a["previous_attempt_id"] = aid
    e["payload"] = {"dispatch_intent": signed(d), "attempt_header": signed(a)}
    rechain(h.events)
    with pytest.raises(ProjectionError) as caught:
        h.project()
    assert caught.value.code == code


def test_duplicate_dispatch_intent_rejected_even_with_new_attempt_id():
    h = History().prepare()
    h.dispatch()
    dispatch = next(e for e in h.events if e["event_type"] == "dispatch_requested")
    h.emit("dispatch_requested", dispatch["payload"], "attempt", "new-attempt", key="other-key")
    with pytest.raises(ProjectionError) as caught:
        h.project()
    assert caught.value.code == "dispatch_intent_reused"


@pytest.mark.parametrize("violation,code", [("report", "evaluation_bindings_mismatch"),
    ("accept", "evaluation_bindings_mismatch"), ("no_pass", "outputs_without_pass"),
    ("criterion", "evaluation_results_mismatch"), ("verdict", "evaluation_verdict_invalid")])
def test_s15_evaluation_and_acceptance_invariants(violation, code):
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    h.evaluate(verdict="fail" if violation == "no_pass" else "pass")
    if violation in {"accept", "no_pass"}:
        s = h.project()
        h.emit("outputs_accepted", {"task_id": "T1", "evaluation_id": s.latest_report("T1")["evaluation_id"],
                                  "output_bindings": deepcopy(s.latest_attempt("T1").output_bindings)}, "task", "T1")
        if violation == "accept":
            h.events[-1]["payload"]["output_bindings"][0]["artifact"]["content_digest"] = canonical_digest("different")
    else:
        e = next(e for e in h.events if e["event_type"] == "evaluation_report_recorded")
        if violation == "report":
            e["payload"]["subject_bindings"][0]["artifact"]["content_digest"] = canonical_digest("different")
        elif violation == "criterion":
            e["payload"]["criterion_results"][0]["criterion_id"] = "other"
        else:
            e["payload"]["verdict"] = "fail"
        e["payload"] = signed(e["payload"])
    rechain(h.events)
    with pytest.raises(ProjectionError) as caught:
        h.project()
    assert caught.value.code == code


@pytest.mark.parametrize("what,code", [("unknown_zero", "event_schema_invalid"),
    ("currency", "currency_mismatch"), ("scope", "record_scope_mismatch"),
    ("supersedes", "usage_supersedes_invalid"), ("summary", "cost_summary_mismatch")])
def test_cost_invariants(what, code):
    h = happy()
    e = next(e for e in h.events if e["event_type"] == "usage_attached")
    if what == "unknown_zero":
        e["payload"]["cost_lines"][0].update(status="unavailable", amount="0")
    elif what == "currency":
        e["payload"]["cost_lines"][0]["currency"] = "BRL"
    elif what == "scope":
        e["payload"]["scope_ref"] = "other-attempt"
    elif what == "supersedes":
        e["payload"]["supersedes_usage_id"] = "usage-missing"
    else:
        e = h.events[-1]
        e["payload"]["run_summary"]["cost_summary"]["reported"] = "0"
        e["payload"]["summary_hash"] = canonical_digest(e["payload"]["run_summary"])
    e["payload"] = signed(e["payload"]) if what != "summary" else e["payload"]
    rechain(h.events)
    with pytest.raises(ProjectionError) as caught:
        h.project()
    assert caught.value.code == code


def test_workflow_reference_required_and_checked():
    h = History().prepare()
    with pytest.raises(ProjectionError, match="workflow_snapshot_required"):
        project_journal(h.events)
    workflow = deepcopy(h.workflow)
    workflow["objective"] = "alterado"
    with pytest.raises(ProjectionError, match="workflow_snapshot_mismatch"):
        project_journal(h.events, workflow=workflow)


def test_duplicate_and_missing_journal_events_and_future_cutoff_rejected():
    h = History().prepare()
    for events in (h.events + [h.events[-1]], h.events[1:]):
        with pytest.raises(ProjectionError, match="journal_integrity_error|event_schema_invalid"):
            project_journal(events, workflow=h.workflow)
    with pytest.raises(ProjectionError, match="cutoff_invalid"):
        h.project(as_of_global_sequence=999)


@pytest.mark.parametrize("what,code", [("producer", "artifact_producer_mismatch"),
    ("locator", "artifact_digest_mismatch"), ("contract", "output_contract_mismatch")])
def test_artifact_bindings_must_match_producer_digest_and_contract(what, code):
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    p = next(e["payload"] for e in h.events if e["event_type"] == "artifact_ingested")
    if what == "producer":
        p["producer"]["attempt_id"] = "other-attempt"
    elif what == "locator":
        p["locator"] = "orq-store:sha256/" + "0" * 64
    else:
        p["content_contract_ref"] = ref("wrong-contract")
    rechain(h.events)
    with pytest.raises(ProjectionError) as caught:
        h.project()
    assert caught.value.code == code


@pytest.mark.parametrize("what,code", [("required_estimate", "budget_unknown"),
    ("over_cap", "budget_exceeded"), ("release_active", "reservation_release_invalid"),
    ("settle_without_usage", "reservation_usage_missing"), ("settle_scope", "reservation_scope_mismatch")])
def test_reservation_invariants(what, code):
    h = History(cap=True).prepare()
    aid = h.dispatch()
    if what in {"required_estimate", "over_cap"}:
        e = next(e for e in h.events if e["event_type"] == "budget_reserved")
        e["payload"].update(amount=None if what == "required_estimate" else "100.00",
                            status="unavailable" if what == "required_estimate" else "estimated")
        rechain(h.events)
    elif what == "release_active":
        h.emit("budget_released", {"reservation_id": "reservation-" + aid, "reason": "admission_aborted"})
    else:
        h.emit("budget_settled", {"reservation_id": "reservation-" + aid,
                                 "usage_ids": [] if what == "settle_without_usage" else ["nonexistent"]})
    with pytest.raises(ProjectionError) as caught:
        h.project()
    assert caught.value.code == code


def test_s38_idempotency_error_is_retained_and_requires_stopping():
    h = History().prepare()
    h.error(h.run_id, "idempotency_conflict", "non_retryable", kind="run")
    assert recovery_actions(h.project())[0].kind == "stop_run"
    h.transition("run", h.run_id, "running", "stopping")
    s = h.project()
    assert s.state == "stopping" and next(iter(s.errors.values()))["code"] == "idempotency_conflict"


def test_recovery_uses_confirmed_facts_before_reconciling():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.emit("termination_confirmed", {"cause": "finished", "observed_at": NOW}, "attempt", aid)
    assert recovery_actions(h.project())[0].kind == "resume_attempt_projection"


def test_recovery_no_eligible_task_and_global_evaluation():
    h = History().prepare()
    assert recovery_actions(h.project())[0].kind == "resume_admission"
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    h.evaluate()
    h.accept()
    assert recovery_actions(h.project())[0].kind == "request_global_evaluation"
    h.evaluate(None)
    assert recovery_actions(h.project())[0].kind == "finalize_global_evaluation"


def test_missing_usage_for_possible_execution_is_unknown_not_exact_zero():
    h = History().prepare()
    h.dispatch()
    cost = h.project().cost_summary
    assert cost["unavailable_count"] == 1 and cost["total_cost_exact"] is False


def test_cutoff_before_late_usage_and_before_run_closed_preserves_publication():
    h = happy()
    closed = h.events.pop()
    previous_usage = next(e["payload"]["usage_id"] for e in h.events if e["event_type"] == "usage_attached")
    h.usage("attempt-T1-1", "unavailable", supersedes=previous_usage)
    h.emit("run_closed", closed["payload"])
    s = h.project()
    assert s.published_summary["cost_summary"]["unavailable_count"] == 0
    assert s.cost_summary["unavailable_count"] == 1


def test_s24_s60_re_evaluation_same_subjects_has_new_id_and_finite_limit():
    h = History(max_evaluations=2).prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    first = h.evaluate(verdict="inconclusive")
    h.transition("task", "T1", "evaluating", "waiting_evaluation")
    second = h.evaluate(number=2)
    h.accept()
    s = h.project()
    assert first != second and s.evaluations[first].request["subject_bindings"] == s.evaluations[second].request["subject_bindings"]
    assert s.tasks["T1"].state == "succeeded"


def test_s59_evaluation_unknown_expiration_keeps_reservation():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    eid = h.evaluate(report=False)
    h.transition("task", "T1", "evaluating", "reconciling")
    h.now = "2026-11-10T12:00:18Z"
    h.transition("task", "T1", "reconciling", "indeterminate")
    h.transition("run", h.run_id, "running", "stopping")
    h.transition("run", h.run_id, "stopping", "indeterminate")
    h.close()
    s = h.project()
    assert s.evaluations[eid].report is None
    assert s.reservations["reservation-" + eid].state == "open"
    assert not s.tasks["T1"].accepted_bindings


def test_unavailable_cannot_be_erased_in_published_cost_summary():
    h = happy()
    closed = h.events.pop()
    uid = next(e["payload"]["usage_id"] for e in h.events if e["event_type"] == "usage_attached")
    h.usage("attempt-T1-1", "unavailable", supersedes=uid)
    h.close()
    p = h.events[-1]["payload"]
    p["run_summary"]["cost_summary"].update(unavailable_count=0, total_cost_exact=True)
    p["summary_hash"] = canonical_digest(p["run_summary"])
    rechain(h.events)
    with pytest.raises(ProjectionError, match="cost_summary_mismatch"):
        h.project()


@pytest.mark.parametrize("seconds", [-1, 1])
def test_first_reconciliation_deadline_cannot_be_shortened_or_extended(seconds):
    h = History().prepare()
    aid = h.dispatch()
    h.unknown(aid, "queued")
    h.reconcile(aid, "unknown")
    request = next(e for e in h.events if e["event_type"] == "reconciliation_requested")
    request["payload"]["deadline_at"] = (timestamp("2026-11-10T12:00:17Z") + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    rechain(h.events)
    with pytest.raises(ProjectionError, match="reconciliation_deadline_mismatch"):
        h.project()


def test_reconciliation_deadline_is_limited_by_run_end():
    h = History().prepare()
    aid = h.dispatch()
    h.unknown(aid, "queued")
    h.now = "2026-11-10T12:09:55Z"
    h.reconcile(aid, "unknown")
    assert h.project().attempts[aid].reconciliation_deadline == "2026-11-10T12:10:00Z"
    h.events[-2]["payload"]["deadline_at"] = "2026-11-10T12:10:10Z"
    rechain(h.events)
    with pytest.raises(ProjectionError, match="reconciliation_deadline_mismatch"):
        h.project()


@pytest.mark.parametrize("change", [None, -1, 1])
def test_later_reconciliation_requests_preserve_first_deadline(change):
    h = History().prepare()
    aid = h.dispatch()
    h.unknown(aid, "queued")
    h.reconcile(aid, "unknown")
    h.now = "2026-11-10T12:00:10Z"
    h.reconcile(aid, "unknown")
    if change is not None:
        h.events[-2]["payload"]["deadline_at"] = (timestamp("2026-11-10T12:00:17Z") + timedelta(seconds=change)).isoformat().replace("+00:00", "Z")
        rechain(h.events)
        with pytest.raises(ProjectionError, match="reconciliation_deadline_mismatch"):
            h.project()
    else:
        s = h.project()
        assert s.attempts[aid].reconciliation_deadline == "2026-11-10T12:00:17Z"
        assert not s.reconciliation_expired("T1", "2026-11-10T12:00:16Z")
        assert s.reconciliation_expired("T1", "2026-11-10T12:00:17Z")


def expired_evaluation():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    eid = h.evaluate(verdict="not_run")
    report = deepcopy(next(e["payload"] for e in h.events if e["event_type"] == "evaluation_report_recorded"))
    h.events = h.events[:-2]  # Simula crash antes do relatório e da liberação.
    h.transition("task", "T1", "evaluating", "reconciling")
    h.now = "2026-11-10T12:00:18Z"
    h.transition("task", "T1", "reconciling", "indeterminate")
    return h, eid, report


@pytest.mark.parametrize("stop", [False, True])
def test_s59_recovery_never_reports_evaluation_after_task_terminal(stop):
    h, eid, _ = expired_evaluation()
    if stop:
        h.transition("run", h.run_id, "running", "stopping")
    s = h.project()
    actions = recovery_actions(s)
    assert all(a.kind != "record_interrupted_evaluation" for a in actions)
    assert actions[0].kind == ("finish_stopping" if stop else "stop_run")
    assert s.evaluations[eid].report is None and s.reservations["reservation-" + eid].state == "open"


@pytest.mark.parametrize("late_event", ["report", "release"])
def test_s59_late_report_or_release_cannot_end_evaluation_reconciliation(late_event):
    h, eid, report = expired_evaluation()
    if late_event == "report":
        h.emit("evaluation_report_recorded", report, "evaluation", eid)
    else:
        h.emit("budget_released", {"reservation_id": "reservation-" + eid, "reason": "evaluation_completed"})
    with pytest.raises(ProjectionError, match="evaluation_reconciliation_closed" if late_event == "report" else "reservation_release_invalid"):
        h.project()


@pytest.mark.parametrize("max_cost", [None, "10.00"])
def test_optional_cap_accepts_unavailable_reservations_and_retains_budget_gaps(max_cost):
    h = History()
    h.manifest["budget_snapshot"]["max_cost"] = max_cost
    h.prepare()
    aid = h.dispatch()
    orphan = h.reserve("evaluation", "evaluation-next")
    s = h.project()
    assert s.budget_committed is None
    assert set(s.budget_gaps) == {("attempt", aid), ("evaluation", "evaluation-next")}
    assert s.reservations[orphan].reservation["amount"] is None
    assert not s.cost_summary["total_cost_exact"]


@pytest.mark.parametrize("mandatory", [False, True])
def test_budget_unknown_exception_requires_mandatory_cap(mandatory):
    h = History(cap=mandatory).prepare()
    aid = h.dispatch()
    h.start(aid)
    h.finish(aid)
    uid = h.usage(aid, "unavailable")
    h.emit("budget_settled", {"reservation_id": "reservation-" + aid, "usage_ids": [uid]})
    s = h.project()
    # Isola o contrato da propriedade, também protegido na criação da reserva.
    s.reservations["reservation-" + aid].reservation["amount"] = None
    assert s.budget_gaps == (("attempt", aid),)
    if mandatory:
        with pytest.raises(StateMachineError, match="budget_unknown"):
            _ = s.budget_committed
    else:
        assert s.budget_committed is None


def cancelled_before_dispatch():
    h = History(cap=True).prepare()
    aid = h.dispatch()
    h.emit("cancel_requested", {"reason": "human"})
    h.transition("run", h.run_id, "running", "stopping")
    h.transition("attempt", aid, "queued", "cancelled")
    return h, aid


def test_at04_alone_preserves_cost_gap_and_reservation():
    h, aid = cancelled_before_dispatch()
    s = h.project()
    assert not s.dispatch_proven_not_executed(s.attempts[aid])
    assert s.cost_summary["unavailable_count"] == 1 and not s.cost_summary["total_cost_exact"]
    assert s.budget_committed == Decimal("0.30")
    h.emit("budget_released", {"reservation_id": "reservation-" + aid, "reason": "admission_aborted"})
    with pytest.raises(ProjectionError, match="reservation_release_invalid"):
        h.project()


@pytest.mark.parametrize("source", ["queued", "running"])
@pytest.mark.parametrize("code", ["execution_failed", "output_missing", "timeout", "cancelled", "dispatch_not_executed"])
def test_unrelated_confirmed_error_cannot_replace_termination_evidence(source, code):
    h = History().prepare()
    aid = h.dispatch()
    if source == "running":
        h.start(aid)
    h.error(aid, code)
    s = h.project()
    assert s.attempts[aid].termination_certainty != "confirmed"
    action, = recovery_actions(s)
    assert action.kind == "reconcile_dispatch"
    assert action.aggregate_id == aid and action.details["dispatch_intent_id"] == s.attempts[aid].intent["dispatch_intent_id"]
    assert not s.dispatch_proven_not_executed(s.attempts[aid])
    h.unknown(aid, source)
    h.reconcile(aid, "found_finished")
    h.finish(aid, source="unknown")
    assert h.project().attempts[aid].state == "completed"


def test_dispatch_failed_after_execution_started_proves_neither_termination_nor_zero_cost():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.error(aid, "dispatch_failed")
    s = h.project()
    assert s.attempts[aid].termination_certainty != "confirmed"
    assert not s.dispatch_proven_not_executed(s.attempts[aid])
    assert s.cost_summary["unavailable_count"] == 1
    assert recovery_actions(s)[0].kind == "reconcile_dispatch"


def test_confirmed_dispatch_failed_resumes_at03():
    h = History().prepare()
    aid = h.dispatch()
    h.error(aid, "dispatch_failed")
    assert recovery_actions(h.project())[0].kind == "resume_attempt_projection"
    h.transition("attempt", aid, "queued", "failed")
    assert h.project().attempts[aid].state == "failed"


@pytest.mark.parametrize("confirmed_by", ["termination", "reconciliation"])
def test_found_running_cannot_downgrade_confirmed_termination(confirmed_by):
    h = History().prepare()
    aid = h.dispatch()
    h.unknown(aid, "queued")
    h.reconcile(aid, "unknown")
    if confirmed_by == "termination":
        h.emit("termination_confirmed", {"cause": "failed", "observed_at": h.now}, "attempt", aid)
    elif confirmed_by == "reconciliation":
        h.reconcile(aid, "found_failed")
    h.reconcile(aid, "found_running")
    with pytest.raises(ProjectionError, match="termination_evidence_conflict"):
        h.project()


def test_execution_started_cannot_downgrade_confirmed_dispatch_failure():
    h = History().prepare()
    aid = h.dispatch()
    h.error(aid, "dispatch_failed")
    h.emit("execution_started", {"observed_at": h.now}, "attempt", aid)
    with pytest.raises(ProjectionError, match="termination_evidence_conflict"):
        h.project()


def test_recovery_after_ack_before_execution_started_requires_explicit_uncertainty():
    h = History().prepare()
    aid = h.dispatch()
    h.emit("dispatch_acknowledged", {"external_call_id": "call-" + aid}, "attempt", aid)
    action, = recovery_actions(h.project())
    assert action.kind == "reconcile_dispatch" and action.details["reason"] == "dispatch_uncertain"
    error = action.details["required_error"]
    h.error(aid, error["code"], certainty=error["termination_certainty"])
    h.unknown(aid, "queued", action.details["reason"])
    h.reconcile(aid, "found_running")
    h.transition("attempt", aid, "unknown", "running")
    assert h.project().attempts[aid].state == "running"


@pytest.mark.parametrize("defect", ["no_error", "wrong_scope", "open_attempt_reserve", "open_evaluation_reserve"])
def test_admission_rejection_requires_task_error_and_released_step7_reservations(defect):
    h = History().prepare()
    h.ready()
    error = h.error("T1" if defect != "wrong_scope" else h.run_id, "no_eligible_target", "non_retryable",
                    kind="run" if defect == "wrong_scope" else "task")
    if defect.startswith("open_"):
        h.reserve("attempt" if defect == "open_attempt_reserve" else "evaluation", "candidate-next")
    h.emit("admission_rejected", {"reason": "no_eligible_target",
        "error_record_ids": [] if defect == "no_error" else [error["payload"]["error_id"]]}, "task", "T1")
    h.transition("task", "T1", "ready", "failed")
    with pytest.raises(ProjectionError, match="admission_reservations_open" if defect.startswith("open_") else "reference_mismatch"):
        h.project()


def test_admission_rejection_after_step7_releases_does_not_consume_attempt():
    h = History().prepare()
    h.ready()
    ids = [h.reserve("attempt", "candidate-attempt"), h.reserve("evaluation", "candidate-evaluation")]
    error = h.error("T1", "context_rejected", "non_retryable", kind="task")
    for rid in ids:
        h.emit("budget_released", {"reservation_id": rid, "reason": "admission_aborted"})
    h.emit("admission_rejected", {"reason": "context_rejected", "error_record_ids": [error["payload"]["error_id"]]}, "task", "T1")
    h.transition("task", "T1", "ready", "failed")
    s = h.project()
    assert not s.attempts and not s.tasks["T1"].attempt_ids
    assert all(s.reservations[rid].state == "released" for rid in ids)
    assert "TK04" in s.transition_ids.values()


def test_dispatch_defensively_rejects_attempts_exhausted():
    h = History().prepare()
    aid = h.dispatch()
    h.start(aid)
    h.fail(aid)
    h.transition("task", "T1", "running", "retry_wait")
    h.transition("task", "T1", "retry_wait", "ready")
    h.dispatch(number=2, ready=False)
    dispatch = next(e for e in h.events if e["event_type"] == "dispatch_requested" and e["payload"]["attempt_header"]["attempt_number"] == 2)
    s = h.project(as_of_global_sequence=dispatch["global_sequence"] - 1)
    # Isola a defesa da admissão: TK06/TK23 normalmente impedem chegar aqui.
    s.manifest["resolved_limits"]["max_attempts"] = 1
    with pytest.raises(StateMachineError, match="attempts_exhausted"):
        _dispatch(s, dispatch)
