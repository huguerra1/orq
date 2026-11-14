"""Cenários da Fase 6 sobre o runner real, somente com fixtures locais."""
from copy import deepcopy
import json

import pytest

from conftest import ROOT, load_fixture
from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.runner import SequentialRunner, reference
from orq_contracts.simulated_executor import HookController, SimulationScript, parse_utc, format_utc
from orq_contracts._storage import publish_bytes
from orq_contracts.executor import ExecutorError, bytes_digest
from orq_contracts.journal import JournalError
from orq_contracts.projections import ProjectionError, project_journal, project_event
from orq_contracts.router import DecisionEngineResult, SimulatedDecisionEngine
from orq_contracts.validator import validate_instance
from datetime import timedelta
import socket


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("acesso à rede")
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, forbidden)
    for name in ("connect", "connect_ex", "send", "sendall", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)


def inputs(*, full=False):
    workflow = load_fixture("valid/workflow-reference.json")
    document = load_fixture("valid/simulation-script-reference.json")
    if not full:
        workflow["tasks"] = workflow["tasks"][:1]
        workflow["outputs"][0]["source_task_id"] = "T1"
        document["attempts"] = {}
    return dict(workflow=workflow, request=load_fixture("run-request.json"),
                catalog=load_fixture("run-catalog.json"), policy=load_fixture("run-policy.json"),
                knowledge_catalog=load_fixture("vault/catalog-snapshot.json"),
                vault_root=ROOT / "fixtures/v0.1/vault", document=document)


def make_runner(root, data=None, *, resume=False, on_event=None, **kwargs):
    data = deepcopy(data or inputs())
    document = data.pop("document")
    data["request"]["workflow_template_ref"] = reference(data["workflow"], "workflow_id")
    script = SimulationScript(document, expected_digest=canonical_digest(document))
    return (SequentialRunner.open if resume else SequentialRunner.create)(root, **data, script=script,
        clock=script.make_clock(), on_event=on_event, **kwargs)


def events(runner, type, kind=None, id=None):
    return [e for e in runner.journal.events if e["event_type"] == type
            and (kind is None or e["aggregate_type"] == kind) and (id is None or e["aggregate_id"] == id)]


def edges(runner, kind, id):
    return [(e["payload"]["from"], e["payload"]["to"])
            for e in events(runner, "state_transition", kind, id)]


def assert_reservations_closed(runner):
    assert runner.projection.reservations
    assert all(r.state in {"released", "settled"} for r in runner.projection.reservations.values())


def test_S01_minimal(tmp_path):
    with make_runner(tmp_path) as r:
        assert r.run().state == "succeeded"
        names = [e["event_type"] for e in r.journal.events]
        expected = ["run_intent_recorded", "planning_recorded", "run_manifest_closed",
                    "knowledge_selection_recorded", "routing_decision_recorded", "context_manifest_recorded",
                    "budget_reserved", "dispatch_requested", "termination_confirmed", "output_bound",
                    "evaluation_report_recorded", "outputs_accepted", "evaluation_requested",
                    "evaluation_report_recorded", "run_closed"]
        position = -1
        for name in expected:
            position = names.index(name, position + 1)
        assert r.projection.published_summary["state"] == "succeeded"
        reports = [e["payload"] for e in events(r, "evaluation_report_recorded")]
        assert [p["scope"]["kind"] for p in reports] == ["task_attempt", "workflow"]
        assert [p["verdict"] for p in reports] == ["pass", "pass"]


def test_S02_reference_retry(tmp_path):
    with make_runner(tmp_path, inputs(full=True)) as r:
        s = r.run()
        assert s.state == "succeeded"
        assert len(s.tasks["T3"].attempt_ids) == 2
        for type in ("routing_decision_recorded", "context_manifest_recorded"):
            assert len(events(r, type, "task", "T3")) == 2
        a1, a2 = [s.attempts[id] for id in s.tasks["T3"].attempt_ids]
        assert a2.header["previous_attempt_id"] == a1.id
        assert a1.header["workspace_base_ref"] == a2.header["workspace_base_ref"]
        assert s.latest_attempt("T4").header["input_bindings"][0]["artifact"] == a2.output_bindings[0]["artifact"]
        assert a1.output_bindings != a2.output_bindings
        calls = [c for c in r.executor.export_state()["calls"].values() if c["intent"]["task_id"] == "T3"]
        assert len({c["workspace"]["workspace_id"] for c in calls}) == 2
        assert calls[0]["workspace"]["base_ref"] == calls[1]["workspace"]["base_ref"]


def test_S03_reproducible(tmp_path):
    results = []
    for name in ("first", "second"):
        with make_runner(tmp_path / name, inputs(full=True)) as r:
            assert r.run().state == "succeeded"
            results.append((r.journal.final_digest, r.journal.path.read_bytes(),
                            [e["payload"].get("record_hash") for e in r.journal.events]))
    assert results[0] == results[1]


class InjectedCrash(BaseException):
    pass


class Harness:
    """Hooks do 6D: escopo exato, disparo único persistido antes da interrupção."""

    def __init__(self, root, data):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        previous = sorted(root.glob("hook-*.json"))
        fired = json.loads(previous[-1].read_bytes()) if previous else []
        document = data["document"]
        script = SimulationScript(document, expected_digest=canonical_digest(document))
        self.controller = HookController(script, fired=fired)

    def __call__(self, runner, event):
        kind, id, p = event["aggregate_type"], event["aggregate_id"], event["payload"]
        scope = {"kind": "run"}
        s = runner.projection
        if kind == "evaluation":
            scope = deepcopy(s.evaluations[id].request["scope"])
            if scope["kind"] == "task_attempt":
                scope.pop("attempt_id")
                scope["attempt_number"] = s.latest_attempt(scope["task_id"]).header["attempt_number"]
        elif kind == "attempt":
            a = s.attempts[id]
            scope = {"kind": "task_attempt", "task_id": a.header["task_id"], "attempt_number": a.header["attempt_number"]}
        elif kind == "task":
            a = s.latest_attempt(id)
            scope = {"kind": "task_attempt", "task_id": id,
                     "attempt_number": p.get("candidate_attempt_number", a.header["attempt_number"] if a else 1)}
        elif event["event_type"] == "budget_reserved":
            for task in s.tasks.values():
                n = len(task.attempt_ids) + 1
                if p["scope_ref"] in {runner.ids("attempt", task.id, n), runner.ids("evaluation", task.id, n, 1)}:
                    scope = {"kind": "task_attempt", "task_id": task.id, "attempt_number": n}
        hook = self.controller.due(event["event_type"], scope)
        if hook:
            publish_bytes(self.root / f"hook-{len(self.controller.fired):04}.json", canonicalize(list(self.controller.fired)))
            if hook["action"] == "crash_after":
                raise InjectedCrash(hook["hook_id"])
            runner.cancel()


def hook(data, event, *, action="cancel_after", task="T1", number=1):
    scope = {"kind": "task_attempt", "task_id": task, "attempt_number": number} if task else {"kind": "run"}
    data["document"]["hooks"] = [{"hook_id": "hook-1", "action": action, "event_type": event, "scope": scope}]
    return data


def run_case(root, data, *, callback=None, **kwargs):
    harness = Harness(root / "harness", data)
    def on_event(r, e):
        harness(r, e)
        if callback:
            callback(r, e)
    r = make_runner(root, data, on_event=on_event, **kwargs)
    try:
        r.run()
        return r
    except BaseException:
        r.close()
        raise


def test_S04_interrupted_before_evaluation_projects_evaluating(tmp_path):
    """S04 (revisado em 2026-11-14): interrupção depois de TK05 e antes de qualquer avaliação."""
    def crash_at_evaluating(runner, event):
        if (event["event_type"] == "state_transition" and event["aggregate_type"] == "task"
                and event["aggregate_id"] == "T1" and event["payload"]["to"] == "evaluating"):
            raise InjectedCrash()
    with make_runner(tmp_path, inputs(full=True), on_event=crash_at_evaluating) as r:
        with pytest.raises(InjectedCrash):
            r.run()
        assert r.projection.tasks["T1"].state == "evaluating"
        assert r.projection.latest_attempt("T1").state == "completed"
        assert r.projection.tasks["T2"].state == "pending"
        assert not events(r, "evaluation_requested")
        assert not events(r, "evaluation_report_recorded")


def test_S04_crash_right_after_output_bound_resumes_without_redispatch(tmp_path):
    """Interrupção logo após o último output_bound: a retomada grava AT06/TK05 e conclui sem novo despacho."""
    data = hook(inputs(full=True), "output_bound", action="crash_after")
    with pytest.raises(InjectedCrash):
        run_case(tmp_path, data)
    with make_runner(tmp_path, data, resume=True, on_event=Harness(tmp_path / "harness", data)) as r:
        assert r.projection.tasks["T1"].state == "running"
        assert r.projection.tasks["T2"].state == "pending"
        assert r.run().state == "succeeded"
        assert len(events(r, "dispatch_requested", "attempt", "attempt-T1-1")) == 1
        assert "evaluating" in [e["payload"]["to"] for e in events(r, "state_transition", "task", "T1")]


def test_S05_null_resolved_model(tmp_path):
    data = inputs()
    data["document"]["default"]["resolved_model_id"] = None
    with run_case(tmp_path, data) as r:
        a = r.projection.latest_attempt("T1")
        assert a.header["execution_target_snapshot"]["model_ref"]["id"]
        assert a.resolved_model_id is None
        assert events(r, "result_collected")[0]["payload"]["resolved_model_id"] is None


def test_S06_planning_failed(tmp_path):
    data = inputs()
    data["request"]["objective"] = "Outro objetivo"
    with run_case(tmp_path, data) as r:
        assert r.projection.state == "failed"
        assert events(r, "planning_recorded")[0]["payload"]["status"] == "not_applicable"
        assert r.projection.manifest["manifest_status"] == "planning_failed"
        assert not r.projection.attempts
        assert r.projection.published_summary["cost_summary"]["currency"] is None


def knowledge(data):
    data["workflow"]["tasks"][0]["knowledge_requirements"] = load_fixture("vault/selection-request.json")["requirements"]
    return data


@pytest.mark.parametrize("scenario", ["S07", "S08", "S09", "S11", "S12", "S42", "S44", "S63"])
def test_admission_rejections(tmp_path, monkeypatch, scenario):
    data = inputs()
    expected = {"S07": "no_eligible_target", "S08": "knowledge_unsatisfied", "S09": "knowledge_revoked",
                "S11": "context_rejected", "S12": "context_rejected", "S42": "budget_unknown", "S44": "budget_exceeded", "S63": "low_confidence"}[scenario]
    engine = None
    if scenario == "S07":
        for t in data["catalog"]["targets"]:
            t["supported_task_types"] = ["unrelated_task_type"]
        data["catalog"]["catalog_hash"] = canonical_digest({k: v for k, v in data["catalog"].items() if k != "catalog_hash"})
    if scenario in {"S08", "S09"}:
        knowledge(data)
        if scenario == "S08":
            data["workflow"]["tasks"][0]["knowledge_requirements"][0]["allowed_kinds"] = ["operational"]
        else:
            a = deepcopy(data["knowledge_catalog"]["status_assertions"][0])
            a.update(assertion_id="revoked-after-cutoff", status="revoked", effective_at="2026-11-10T11:00:00Z")
            a["assertion_digest"] = canonical_digest({k: v for k, v in a.items() if k != "assertion_digest"})
            data["policy"]["knowledge_status_assertions"] = [a]
    if scenario in {"S11", "S12"}:
        from orq_contracts import runner as module
        original = module.materialize_knowledge_context
        from dataclasses import replace
        def corrupt(*args, **kwargs):
            result = original(*args, **kwargs)
            if scenario == "S11":
                return replace(result, bundle=result.bundle+b" ")
            result.manifest["routing_decision_ref"]["content_digest"] = canonical_digest("wrong")
            return result
        monkeypatch.setattr(module, "materialize_knowledge_context", corrupt)
    if scenario in {"S42", "S44"}:
        budget(data, attempt=None if scenario == "S42" else "0.30", evaluation="0.20", cap="0.40")
    if scenario == "S63":
        engine = typed_policy(data, "no_dispatch")
    with run_case(tmp_path, data, decision_engine=engine) as r:
        assert r.projection.state == "failed"
        assert not events(r, "dispatch_requested")
        assert events(r, "admission_rejected")[-1]["payload"]["reason"] == expected
        assert r.projection.tasks["T1"].state == "failed"
        assert not r.projection.attempts
        assert all(r.state == "released" for r in r.projection.unassigned_reservations.values())
        if scenario in {"S07", "S63"}:
            decision, = events(r, "routing_decision_recorded")
            assert decision["payload"]["decision_status"] == expected
            assert decision["payload"]["effective_selection"] is None
        if scenario == "S08":
            selection, = events(r, "knowledge_selection_recorded")
            assert selection["payload"]["status"] == "unsatisfied"
        if scenario in {"S09", "S11"}:
            assert r.projection.reservations
            assert len(events(r, "budget_reserved")) == len(events(r, "budget_released")) == 2
        if scenario in {"S11", "S12"}:
            assert "integrity_mismatch" in {e["code"] for e in r.projection.errors.values()}
            assert all(e["phase"] == "admission" for e in r.projection.errors.values())
        if scenario == "S11":
            assert events(r, "context_manifest_recorded")
        if scenario == "S12":
            assert events(r, "routing_decision_recorded") and not events(r, "context_manifest_recorded")
            assert not events(r, "budget_reserved")
        if scenario == "S09":
            assert "knowledge_revoked" in {e["code"] for e in r.projection.errors.values()}
            context, = events(r, "context_manifest_recorded")
            assert context["payload"]["items"]
        if scenario in {"S42", "S44"}:
            assert not events(r, "budget_reserved")
            assert expected in {e["code"] for e in r.projection.errors.values()}


def test_S09_future_revocation_passes(tmp_path):
    data = knowledge(inputs())
    a = deepcopy(data["knowledge_catalog"]["status_assertions"][0])
    a.update(assertion_id="future-revocation", status="revoked", effective_at="2027-01-01T00:00:00Z")
    a["assertion_digest"] = canonical_digest({k: v for k, v in a.items() if k != "assertion_digest"})
    data["policy"]["knowledge_status_assertions"] = [a]
    with run_case(tmp_path, data) as r:
        assert r.projection.state == "succeeded"


def test_S10_retry_catalog_is_fixed(tmp_path):
    data = inputs(full=True)
    data["workflow"]["tasks"][2]["knowledge_requirements"] = load_fixture("vault/selection-request.json")["requirements"]
    with run_case(tmp_path, data) as r:
        selections = events(r, "knowledge_selection_recorded", "task", "T3")
        assert len(selections) == 2
        assert all(e["payload"]["catalog_snapshot_ref"] == r.projection.manifest["knowledge_catalog_snapshot_ref"] for e in selections)
        assert all(e["payload"]["selected_units"] for e in selections)
        assert selections[0]["payload"]["selected_units"] == selections[1]["payload"]["selected_units"]
    changed = deepcopy(data)
    changed["knowledge_catalog"]["version"] = "outside-run"
    with pytest.raises(ExecutorError, match="snapshot_mismatch"):
        make_runner(tmp_path, changed, resume=True)


@pytest.mark.parametrize("scenario,mode,code", [("S13", "declared_digest_mismatch", "integrity_mismatch"),
    ("S20", "omit", "output_missing"), ("S21", "wrong_contract", "output_contract_mismatch")])
def test_invalid_outputs(tmp_path, scenario, mode, code):
    data = inputs()
    data["document"]["default"]["outputs"] = {"report": mode}
    with run_case(tmp_path, data) as r:
        assert r.projection.state == "failed"
        assert r.projection.latest_attempt("T1").state == "failed"
        assert code in {e["code"] for e in r.projection.errors.values()}
        assert not events(r, "outputs_accepted")
        assert not events(r, "output_bound")
        assert all(to != "completed" for _, to in edges(r, "attempt", r.projection.latest_attempt("T1").id))
        if scenario == "S13":
            artifact, = events(r, "artifact_ingested", "attempt")
            a = r.projection.latest_attempt("T1")
            file, = r.executor.collect(a.intent).produced_files
            assert artifact["payload"]["content_digest"] == bytes_digest(file.content)
            assert artifact["payload"]["content_digest"] != file.declared_digest


def test_S14_corrupted_artifact_fails_evaluation(tmp_path):
    data = inputs()
    data["policy"]["resolved_limits"]["retry_policy"]["on_evaluation_fail"] = False
    def corrupt(r, e):
        if e["event_type"] == "evaluation_requested" and e["payload"]["scope"]["kind"] == "task_attempt":
            artifact = e["payload"]["subject_bindings"][0]["artifact"]
            (r.journal.directory / "artifacts/sha256" / artifact["content_digest"]["value"]).write_bytes(b"changed")
    with run_case(tmp_path, data, callback=corrupt) as r:
        report = events(r, "evaluation_report_recorded")[0]["payload"]
        assert report["verdict"] == "fail"
        assert next(c for c in report["criterion_results"] if c["method"] == "digest_matches")["status"] == "fail"
        assert not events(r, "outputs_accepted")


def test_S15_acceptance_rejects_different_subjects(tmp_path):
    data = inputs(full=True)
    def reject(r, e):
        if e["event_type"] == "evaluation_report_recorded":
            binding = deepcopy(e["payload"]["subject_bindings"])
            binding[0]["artifact"]["content_digest"] = canonical_digest("different")
            before = r.journal.path.read_bytes()
            with pytest.raises(ProjectionError, match="evaluation_bindings_mismatch"):
                r.append("outputs_accepted", {"task_id": "T1", "evaluation_id": e["aggregate_id"], "output_bindings": binding}, "task", "T1")
            assert before == r.journal.path.read_bytes()
            assert r.projection.tasks["T2"].state == "pending"
            raise InjectedCrash()
    with pytest.raises(InjectedCrash):
        run_case(tmp_path, data, callback=reject)


def test_S16_journal_corruption_is_not_repaired(tmp_path):
    with run_case(tmp_path, inputs()) as r:
        path = r.journal.path
    lines = path.read_bytes().splitlines(keepends=True)
    lines[4] = lines[4].replace(b'"orq-runner"', b'"bad-runner"')
    corrupt = b"".join(lines)
    path.write_bytes(corrupt)
    with pytest.raises(JournalError, match="journal_integrity_error"):
        make_runner(tmp_path, resume=True)
    assert path.read_bytes() == corrupt


@pytest.mark.parametrize("scenario", ["S17", "S18", "S19", "S22", "S23", "S52", "S53"])
def test_failures_and_retry(tmp_path, scenario):
    full = scenario in {"S18", "S53"}
    data = inputs(full=full)
    data["document"]["attempts"] = {}
    task = "T2" if scenario == "S18" else "T1"
    override = {"dispatch": "reject_confirmed", "reconcile": "not_found"} if scenario in {"S52", "S53"} else (
        {"verifier": {"behavior-ok": "fail"}} if scenario in {"S22", "S23"} else {
        "execution": "fail_non_retryable" if scenario == "S18" else "fail_retryable", "reconcile": "found_failed"})
    data["document"]["attempts"][task] = {"1": override}
    if scenario == "S19":
        data["document"]["attempts"][task]["2"] = override
    if scenario in {"S22", "S23"}:
        data["policy"]["resolved_limits"]["retry_policy"]["on_evaluation_fail"] = False
    if scenario == "S53":
        data["workflow"]["constraints"]["max_attempts"] = data["policy"]["resolved_limits"]["max_attempts"] = 1
    with run_case(tmp_path, data) as r:
        s = r.projection
        retried = scenario in {"S17", "S19", "S52"}
        assert len(s.tasks[task].attempt_ids) == (2 if retried else 1)
        assert s.state == ("succeeded" if scenario in {"S17", "S52"} else "failed")
        if retried:
            a1, a2 = [s.attempts[id] for id in s.tasks[task].attempt_ids]
            assert a1.id != a2.id and a2.header["previous_attempt_id"] == a1.id
            calls = list(r.executor.export_state()["calls"].values())
            assert len({c["workspace"]["workspace_id"] for c in calls}) == len(calls)
        if full:
            assert all(t.state == "blocked" for t in list(s.tasks.values())[int(task[1:]):])
        if scenario in {"S18", "S19", "S22", "S53"}:
            assert s.tasks[task].state == "failed"
        if scenario in {"S17", "S19", "S52"}:
            assert a1.state == "failed"
            error = s.errors[a1.failure_error_id]
            assert error["retryability"] == "retryable" and error["termination_certainty"] == "confirmed"
            assert ("running", "retry_wait") in edges(r, "task", task)
            assert ("retry_wait", "ready") in edges(r, "task", task)
        if scenario == "S19":
            assert len(events(r, "dispatch_requested")) == len(events(r, "routing_decision_recorded")) == 2
            assert len(events(r, "knowledge_selection_recorded")) == 2
            assert not events(r, "admission_rejected")
        if scenario == "S22":
            report, = events(r, "evaluation_report_recorded")
            assert report["payload"]["verdict"] == "fail"
        if scenario == "S23":
            assert len(events(r, "evaluation_requested")) == 1
            assert events(r, "evaluation_report_recorded")[0]["payload"]["verdict"] == "fail"
        if scenario == "S52":
            assert r.executor.execution_count == 1
            assert ("queued", "failed") in edges(r, "attempt", a1.id)
            assert error["code"] == "dispatch_failed"
        if scenario == "S53":
            assert s.latest_attempt("T1").state == "failed"
            assert len(events(r, "routing_decision_recorded")) == 1


@pytest.mark.parametrize("scenario", ["S24", "S25", "S26", "S59", "S60"])
def test_evaluations(tmp_path, scenario):
    data = inputs()
    if scenario in {"S24", "S59", "S60"}:
        verdict = {"S24": ["inconclusive", "pass"], "S59": "unknown_termination", "S60": ["inconclusive", "inconclusive"]}[scenario]
        data["document"]["default"]["verifier"]["behavior-ok"] = verdict
    if scenario == "S25":
        data["workflow"]["tasks"][0]["evaluation_criteria"][0]["method"] = "unknown_method"
    if scenario == "S26":
        data["document"]["workflow_verifier"]["workflow-behavior-ok"] = "fail"
    with run_case(tmp_path, data) as r:
        assert r.projection.state == ("succeeded" if scenario == "S24" else "indeterminate" if scenario == "S59" else "failed")
        reports = [e["payload"] for e in events(r, "evaluation_report_recorded")]
        if scenario in {"S24", "S60"}:
            assert len(reports) == (3 if scenario == "S24" else 2)
            assert reports[0]["evaluation_id"] != reports[1]["evaluation_id"]
            assert reports[0]["subject_bindings"] == reports[1]["subject_bindings"]
            transitions = [e["payload"] for e in events(r, "state_transition", "task", "T1")]
            assert any(t["from"] == "evaluating" and t["to"] == "waiting_evaluation" for t in transitions)
            assert any(t["from"] == "waiting_evaluation" and t["to"] == "evaluating" for t in transitions)
            if scenario == "S24":
                assert [p["verdict"] for p in reports[:2]] == ["inconclusive", "pass"]
                assert r.projection.tasks["T1"].state == "succeeded"
                assert events(r, "outputs_accepted")[0]["payload"]["evaluation_id"] == reports[1]["evaluation_id"]
        if scenario == "S25":
            assert all(p["criterion_results"][0]["status"] == "not_run" and p["verdict"] == "inconclusive" for p in reports)
            assert not events(r, "outputs_accepted")
        if scenario == "S26":
            assert reports[-1]["scope"]["kind"] == "workflow" and reports[-1]["verdict"] == "fail"
            assert all(t.state == "succeeded" and t.accepted_event_id for t in r.projection.tasks.values())
        if scenario == "S59":
            assert not reports and not events(r, "outputs_accepted") and len(r.projection.attempts) == 1
            assert len(events(r, "evaluation_requested")) == 1
            assert ("evaluating", "reconciling") in edges(r, "task", "T1")
            assert ("reconciling", "indeterminate") in edges(r, "task", "T1")
            assert r.projection.tasks["T1"].state == "indeterminate"
        if scenario == "S60":
            assert [p["verdict"] for p in reports] == ["inconclusive", "inconclusive"]
            assert len(events(r, "evaluation_requested")) == 2
            last = events(r, "state_transition", "task", "T1")[-1]["payload"]
            assert last == {**last, "from": "evaluating", "to": "failed", "reason": "evaluation_inconclusive"}
            assert r.projection.tasks["T1"].state == "failed"


@pytest.mark.parametrize("scenario", ["S27", "S28", "S29", "S30", "S31", "S32", "S33", "S55", "S58"])
def test_timeout_and_reconciliation(tmp_path, scenario):
    data = inputs(full=scenario == "S58")
    data["document"]["attempts"] = {}
    override = {"execution": "hang", "on_cancel": "unconfirmed", "reconcile": "unknown"}
    if scenario == "S27":
        override["on_cancel"] = "confirmed"
    if scenario in {"S29", "S55"}:
        override["reconcile"] = "found_finished" if scenario == "S29" else "found_stopped"
    if scenario == "S31":
        override = {"dispatch": "ack_lost", "reconcile": "found_running"}
    if scenario in {"S32", "S33"}:
        override = {"dispatch": "not_executed", "reconcile": "not_found"}
        data["document"]["reconcile_not_found_authoritative"] = scenario == "S32"
    if scenario == "S58":
        override = {"dispatch": "ack_lost", "execution": "fail_non_retryable", "reconcile": "found_failed"}
    data["document"]["attempts"]["T1"] = {"1": override}
    with run_case(tmp_path, data) as r:
        s = r.projection
        first = s.attempts[s.tasks["T1"].attempt_ids[0]]
        if scenario in {"S28", "S30", "S33"}:
            assert s.state == "indeterminate" and first.state == "unknown"
            assert len(s.tasks["T1"].attempt_ids) == 1
            assert s.tasks["T1"].state == "indeterminate"
            assert ("running", "reconciling") in edges(r, "task", "T1")
            assert ("reconciling", "indeterminate") in edges(r, "task", "T1")
            assert len(r.executor.export_state()["calls"]) == 1
            assert parse_utc(r.clock.now()) >= parse_utc(first.reconciliation_deadline)
            assert ("queued" if scenario == "S33" else "running", "unknown") in edges(r, "attempt", first.id)
        elif scenario == "S58":
            assert s.state == "failed" and first.state == "failed"
            assert all(t.state == "blocked" for t in list(s.tasks.values())[1:])
        else:
            assert s.state == "succeeded"
        if scenario in {"S27", "S55"}:
            assert first.state == "timed_out" and len(s.tasks["T1"].attempt_ids) == 2
        if scenario == "S27":
            history = [e["event_type"] for e in r.journal.events if e["aggregate_id"] == first.id]
            assert history.index("deadline_exceeded") < history.index("cancel_requested") < history.index("termination_confirmed")
            assert events(r, "termination_confirmed", "attempt", first.id)[0]["payload"]["cause"] == "timeout"
        if scenario == "S55":
            assert ("running", "unknown") in edges(r, "attempt", first.id)
            assert ("unknown", "timed_out") in edges(r, "attempt", first.id)
            assert ("reconciling", "retry_wait") in edges(r, "task", "T1")
        if scenario in {"S29", "S31"}:
            assert first.state == "completed" and len(s.tasks["T1"].attempt_ids) == 1
            pairs = [(e["payload"]["from"], e["payload"]["to"]) for e in events(r, "state_transition", "attempt", first.id)]
            assert ("unknown", "completed" if scenario == "S29" else "running") in pairs
            if scenario == "S31":
                assert ("queued", "unknown") in pairs
            else:
                assert ("reconciling", "evaluating") in edges(r, "task", "T1")
        if scenario == "S32":
            assert first.state == "failed" and s.errors[first.failure_error_id]["code"] == "dispatch_not_executed"
            assert len(s.tasks["T1"].attempt_ids) == 2
            assert s.errors[first.failure_error_id]["retryability"] == "retryable"
        if scenario == "S33":
            assert events(r, "reconciliation_observed")[0]["payload"]["outcome"] == "unknown"
        for e in events(r, "reconciliation_requested"):
            assert e["payload"]["dispatch_idempotency_key"] == first.intent["dispatch_idempotency_key"]
            assert e["idempotency_key"] != first.intent["dispatch_idempotency_key"]
        assert len(events(r, "dispatch_requested", "attempt", first.id)) == 1


@pytest.mark.parametrize("scenario,event", [("S34", "dispatch_requested"), ("S35", "budget_reserved"), ("S40", "evaluation_requested")])
def test_crash_resume(tmp_path, scenario, event):
    data = hook(inputs(), event, action="crash_after")
    with pytest.raises(InjectedCrash):
        run_case(tmp_path, data)
    with make_runner(tmp_path, data, resume=True, on_event=Harness(tmp_path / "harness", data)) as r:
        assert r.run().state == "succeeded"
        assert len(Harness(tmp_path / "harness", data).controller.fired) == 1
        if scenario == "S34":
            first = r.projection.attempts["attempt-T1-1"]
            assert len(events(r, "dispatch_requested", "attempt", first.id)) == 1
            assert events(r, "reconciliation_requested", "attempt", first.id)
            assert r.executor.dispatch_count == r.executor.execution_count == 1
        if scenario == "S35":
            assert len(r.projection.attempts) == 1
            assert r.projection.latest_attempt("T1").header["attempt_number"] == 1
            assert r.executor.execution_count == 1
            assert events(r, "budget_released")[0]["payload"]["reason"] == "admission_aborted"
            assert events(r, "budget_released")[0]["global_sequence"] < events(r, "dispatch_requested")[0]["global_sequence"]
        if scenario == "S40":
            reports = [e["payload"] for e in events(r, "evaluation_report_recorded")]
            assert (reports[0]["execution_status"], reports[0]["verdict"]) == ("failed", "not_run")
            assert reports[1]["evaluation_number"] == 2
            assert reports[0]["subject_bindings"] == reports[1]["subject_bindings"]
            assert len([p for p in reports if p["scope"]["kind"] == "task_attempt"]) == 2


def test_S36_truncated_tail(tmp_path):
    data = hook(inputs(), "budget_reserved", action="crash_after")
    with pytest.raises(InjectedCrash):
        run_case(tmp_path, data)
    path = tmp_path / "runs/run-reference/journal.jsonl"
    tail = b'{"partial":'
    with path.open("ab") as f:
        f.write(tail)
    with make_runner(tmp_path, data, resume=True, on_event=Harness(tmp_path / "harness", data)) as r:
        assert r.run().state == "succeeded"
        event = events(r, "journal_tail_discarded")[0]
        from orq_contracts.executor import bytes_digest
        assert event["payload"]["discarded_digest"] == bytes_digest(tail)
        assert any(p.read_bytes() == tail for p in (r.journal.directory / "discarded").glob("*.tail"))


@pytest.mark.parametrize("scenario", ["S37", "S38", "S39"])
def test_persistence_guards(tmp_path, scenario):
    def check(r, e):
        if e["event_type"] != "execution_started":
            return
        before = r.journal.path.read_bytes()
        if scenario == "S37":
            assert r.append(e["event_type"], e["payload"], e["aggregate_type"], e["aggregate_id"], key=e["idempotency_key"], actor="executor") == e
            assert before == r.journal.path.read_bytes()
        elif scenario == "S38":
            with pytest.raises(JournalError, match="idempotency_conflict"):
                r.append(e["event_type"], {"observed_at": "2027-01-01T00:00:00Z"}, "attempt", e["aggregate_id"], key=e["idempotency_key"])
            assert r.projection.state == "stopping"
        else:
            with pytest.raises(ProjectionError, match="transition_from_mismatch"):
                r.append("state_transition", {"from": "pending", "to": "failed", "reason": "test", "evidence_event_ids": []}, "task", "T1")
            assert before == r.journal.path.read_bytes()
    with run_case(tmp_path, inputs(), callback=check) as r:
        assert r.projection.state == ("failed" if scenario == "S38" else "succeeded")
        if scenario == "S38":
            assert "idempotency_conflict" in {e["code"] for e in r.projection.errors.values()}


def budget(data, *, attempt="0.30", evaluation="0.10", cap="10.00"):
    b = data["policy"]["budget_snapshot"]
    b.update(max_cost=cap, cap_mandatory=True)
    b["cost_estimates"] = []
    subject = {"id": "evaluator-deterministic", "version": "0.1", "content_digest": canonical_digest({
        "kind": "deterministic", "version": "0.1", "supported_methods": ["output_binding_present", "digest_matches", "content_contract_valid", "simulated_verdict"]})}
    for scope, amount, subjects in [("attempt", attempt, [reference(t, "target_id") for t in data["catalog"]["targets"]]), ("evaluation", evaluation, [subject])]:
        if amount is not None:
            for ref in subjects:
                b["cost_estimates"].append({"scope": scope, "subject_ref": ref, "amount": amount, "currency": "USD", "method": "fixture_ceiling", "source_ref": data["workflow"]["execution_policy_ref"]})
    return data


def test_S41_unavailable_cost(tmp_path):
    with run_case(tmp_path, inputs(full=True)) as r:
        assert r.projection.state == "succeeded"
        summary = r.projection.published_summary["cost_summary"]
        assert summary["reported"] == "0.50" and summary["unavailable_count"] == 1 and not summary["total_cost_exact"]
        assert any(e["payload"]["amount"] is None and e["payload"]["status"] == "unavailable" for e in events(r, "budget_reserved"))
        assert all(res.state == "released" for res in r.projection.reservations.values() if res.reservation["scope_type"] == "evaluation")
        assert any(line["amount"] is None for u in r.projection.usages.values() for line in u["cost_lines"])


def test_S43_unavailable_keeps_estimate_against_cap(tmp_path):
    data = budget(inputs(full=True), cap="0.65")
    data["document"]["attempts"] = {"T1": {"1": {"usage": {"cost": {"status": "unavailable", "amount": None, "currency": "USD"}}}}}
    with run_case(tmp_path, data) as r:
        assert r.projection.state == "failed"
        assert len(r.projection.attempts) == 1  # 0.30 retido + 0.30 + 0.10 > 0.65.
        assert events(r, "admission_rejected")[-1]["payload"]["reason"] == "budget_exceeded"
        assert r.projection.published_summary["cost_summary"]["unavailable_count"] == 1
        first = r.projection.latest_attempt("T1")
        reservation = next(res for res in r.projection.reservations.values() if res.reservation["scope_ref"] == first.id)
        assert reservation.state == "settled" and reservation.reservation["amount"] == "0.30"
        assert all(line["status"] == "unavailable" and line["amount"] is None
                   for uid in reservation.usage_ids for line in r.projection.usages[uid]["cost_lines"])
        assert str(r.projection.budget_committed) == "0.30"


def test_S45_settled_cost_not_added_twice(tmp_path):
    data = budget(inputs())
    data["document"]["default"]["usage"]["cost"]["amount"] = "0.20"
    with run_case(tmp_path, data) as r:
        assert r.projection.published_summary["cost_summary"]["reported"] == "0.20"
        assert str(r.projection.budget_committed) == "0.20"
        assert next(res for res in r.projection.reservations.values() if res.reservation["scope_type"] == "attempt").state == "settled"


def test_S46_unavailable_zero_invalid():
    record = load_fixture("valid/usage-record.json")
    record["cost_lines"][0].update(status="unavailable", amount="0")
    assert validate_instance(record, "usage_record")


def test_S47_late_usage_preserves_published_cutoff(tmp_path):
    with run_case(tmp_path, inputs()) as r:
        before = deepcopy(r.projection.published_summary)
        cutoff = r.projection.published_cutoff
        usage = deepcopy(next(iter(r.projection.usages.values())))
        usage.update(usage_id="late-usage", supersedes_usage_id=usage["usage_id"])
        usage["cost_lines"][0]["amount"] = "0.90"
        usage["record_hash"] = canonical_digest({k: v for k, v in usage.items() if k != "record_hash"})
        r.append("usage_attached", usage, "attempt", usage["scope_ref"], key="late-usage", actor="executor")
        assert r.projection.published_summary == before and r.projection.published_cutoff == cutoff
        assert r.projection.cost_summary["reported"] == "0.90" and r.projection.state == "succeeded"
        assert events(r, "usage_attached")[-1]["global_sequence"] > events(r, "run_closed")[0]["global_sequence"] > cutoff


@pytest.mark.parametrize("scenario", ["S48", "S49", "S50", "S54", "S56", "S57", "S61"])
def test_cancellation(tmp_path, scenario):
    data = inputs(full=scenario in {"S49", "S54", "S61"})
    data["document"]["attempts"] = {}
    event = {"S48": "run_intent_recorded", "S49": "execution_started", "S50": "execution_started", "S54": "dispatch_requested",
             "S56": "execution_started", "S57": "reconciliation_requested", "S61": "evaluation_requested"}[scenario]
    if scenario in {"S49", "S50", "S56"}:
        data["document"]["default"].update(execution="hang", on_cancel="confirmed" if scenario == "S49" else "unconfirmed",
                                          reconcile="found_stopped" if scenario == "S56" else "unknown")
    if scenario == "S57":
        data["document"]["default"].update(dispatch="not_executed", reconcile="not_found")
    hook(data, event, task=None if scenario == "S48" else "T1")
    with run_case(tmp_path, data) as r:
        assert r.projection.state == ("indeterminate" if scenario == "S50" else "cancelled")
        assert not events(r, "outputs_accepted")
        if scenario == "S48":
            assert not r.projection.attempts and r.projection.manifest["manifest_status"] == "cancelled_before_ready"
            assert r.projection.published_summary["cost_summary"]["currency"] is None
        if scenario == "S54":
            assert r.executor.dispatch_count == 0
            assert r.projection.latest_attempt("T1").state == "cancelled"
            assert ("queued", "cancelled") in edges(r, "attempt", "attempt-T1-1")
            assert ("running", "cancelled") in edges(r, "task", "T1")
        if scenario == "S61":
            assert events(r, "evaluation_report_recorded")[0]["payload"]["verdict"] == "pass"
            assert ("evaluating", "cancelled") in edges(r, "task", "T1")
        if scenario in {"S49", "S54", "S61"}:
            assert all(t.state == "cancelled" for t in r.projection.tasks.values())
            assert all(events(r, "state_transition", "task", t.id)[-1]["payload"]["reason"] == "run_cancelled"
                       for t in list(r.projection.tasks.values())[1:])
        if scenario in {"S49", "S54", "S56", "S57"}:
            assert r.projection.latest_attempt("T1").state == "cancelled"
            assert r.projection.tasks["T1"].state == "cancelled"
            assert len(r.projection.attempts) == 1 and len(events(r, "routing_decision_recorded")) == 1
        if scenario in {"S56", "S57"}:
            assert ("unknown", "cancelled") in edges(r, "attempt", "attempt-T1-1")
            assert ("reconciling", "cancelled") in edges(r, "task", "T1")
            assert events(r, "reconciliation_observed")[-1]["payload"]["outcome"] == ("found_stopped" if scenario == "S56" else "not_found")
            assert_reservations_closed(r)
        if scenario == "S50":
            assert r.projection.tasks["T1"].state == "indeterminate"
            assert parse_utc(r.clock.now()) >= parse_utc(r.projection.latest_attempt("T1").reconciliation_deadline)


def test_S51_run_deadline_prevents_admission(tmp_path):
    def elapse(r, e):
        if e["event_type"] == "run_manifest_closed":
            r.clock.advance_to(r._end())
    with run_case(tmp_path, inputs(full=True), callback=elapse) as r:
        assert r.projection.state == "failed" and not events(r, "dispatch_requested")
        assert events(r, "deadline_exceeded", "run")
        assert ("running", "stopping") in edges(r, "run", r.projection.id)
        assert ("stopping", "failed") in edges(r, "run", r.projection.id)
        assert not events(r, "routing_decision_recorded") and not r.projection.attempts


def typed_policy(data, action):
    p = data["policy"]["routing_policy"]
    p.update(mode="typed_decision", confidence_threshold="0.8", low_confidence_action=action,
             decision_engine_ref={"id": "simulated-engine", "version": "1", "content_digest": canonical_digest("engine")})
    return SimulatedDecisionEngine(DecisionEngineResult("target-quality", "0.2"))


def test_S62_low_confidence_fallback_dispatches_selected_target(tmp_path):
    data = inputs()
    with run_case(tmp_path, data, decision_engine=typed_policy(data, "rules_fallback")) as r:
        assert r.projection.state == "succeeded"
        d = events(r, "routing_decision_recorded")[0]["payload"]
        assert d["decision_status"] == "low_confidence" and d["effective_selection"]["used_fallback"]
        target = d["effective_selection"]["selected_target_id"]
        assert r.projection.latest_attempt("T1").intent["target_ref"]["id"] == target
        assert events(r, "context_manifest_recorded")[0]["payload"]["target_ref"]["id"] == target


@pytest.mark.parametrize("event,task", [("run_intent_recorded", None), ("planning_recorded", None),
    ("run_manifest_closed", None), ("knowledge_selection_recorded", "T1"), ("routing_decision_recorded", "T1"),
    ("context_manifest_recorded", "T1"), ("dispatch_acknowledged", "T1"), ("execution_started", "T1"),
    ("termination_confirmed", "T1"), ("result_collected", "T1"), ("artifact_ingested", "T1"),
    ("output_bound", "T1"), ("evaluation_report_recorded", "T1"), ("outputs_accepted", "T1"), ("run_closed", None)])
def test_recovery_at_other_durable_boundaries(tmp_path, event, task):
    data = hook(inputs(), event, action="crash_after", task=task)
    with pytest.raises(InjectedCrash):
        run_case(tmp_path, data)
    with make_runner(tmp_path, data, resume=True, on_event=Harness(tmp_path / "harness", data)) as r:
        assert r.run().state == "succeeded"
        assert r.executor.execution_count == 1
        assert len(r.projection.attempts) == 1
        assert r.projection == project_journal(r.journal.events, workflow=r.workflow,
                                              reconcile_not_found_authoritative=True)
        times = [parse_utc(e["recorded_at"]) for e in r.journal.events]
        assert times == sorted(times)


@pytest.mark.parametrize("mode", ["failure", "timeout", "reconciled_failure", "reconciled_finish", "reconciled_stopped", "not_found"])
def test_recovery_after_confirmation_before_completion(tmp_path, mode):
    data = inputs()
    override = {"execution": "fail_retryable", "reconcile": "found_failed"}
    event = "termination_confirmed"
    if mode in {"timeout", "reconciled_stopped"}:
        override = {"execution": "hang", "on_cancel": "confirmed" if mode == "timeout" else "unconfirmed", "reconcile": "found_stopped"}
    if mode in {"reconciled_failure", "reconciled_finish"}:
        override.update(dispatch="ack_lost")
        if mode == "reconciled_finish":
            override.update(execution="finish", reconcile="found_finished")
    if mode == "not_found":
        override = {"dispatch": "not_executed", "reconcile": "not_found"}
    if mode.startswith("reconciled") or mode == "not_found":
        event = "reconciliation_observed"
    data["document"]["attempts"] = {"T1": {"1": override}}
    hook(data, event, action="crash_after")
    with pytest.raises(InjectedCrash):
        run_case(tmp_path, data)
    with make_runner(tmp_path, data, resume=True, on_event=Harness(tmp_path / "harness", data)) as r:
        assert r.run().state == "succeeded"
        assert len(events(r, "dispatch_requested", "attempt", "attempt-T1-1")) == 1


def test_global_unknown_termination_keeps_reservation(tmp_path):
    data = inputs()
    data["document"]["workflow_verifier"]["workflow-behavior-ok"] = "unknown_termination"
    with run_case(tmp_path, data) as r:
        assert r.projection.state == "indeterminate"
        assert r.projection.global_report is None
        assert any(res.state == "open" for res in r.projection.reservations.values() if res.reservation["scope_type"] == "evaluation")
        assert events(r, "run_closed")


def test_reevaluation_budget_exhaustion_does_not_start_more_work(tmp_path):
    data = budget(inputs(), attempt="0.30", evaluation="0.20", cap="0.50")
    data["document"]["default"]["usage"]["cost"]["amount"] = "0.40"
    data["document"]["default"]["verifier"]["behavior-ok"] = "inconclusive"
    with run_case(tmp_path, data) as r:
        assert r.projection.state == "failed"
        assert len(events(r, "evaluation_requested")) == 1
        assert not events(r, "dispatch_requested", "attempt", "attempt-T1-2")


def test_global_budget_rejection_has_financial_error(tmp_path):
    data = budget(inputs(), cap="0.50")
    data["document"]["default"]["usage"]["cost"]["amount"] = "0.50"
    with run_case(tmp_path, data) as r:
        assert r.projection.state == "failed"
        assert all(t.state == "succeeded" for t in r.projection.tasks.values())
        assert r.projection.global_report is None
        assert "budget_exceeded" in {e["code"] for e in r.projection.errors.values()}
        assert "idempotency_conflict" not in {e["code"] for e in r.projection.errors.values()}


@pytest.mark.parametrize("event", ["knowledge_selection_recorded", "routing_decision_recorded", "context_manifest_recorded", "budget_reserved"])
def test_cancel_during_admission_releases_without_attempt(tmp_path, event):
    with run_case(tmp_path, hook(inputs(), event)) as r:
        assert r.projection.state == "cancelled"
        assert not r.projection.attempts
        assert all(res.state == "released" for res in r.projection.reservations.values())


def test_injected_ids_and_clock_are_shared(tmp_path):
    from orq_contracts.runner import deterministic_id
    data = inputs()
    with make_runner(tmp_path, data, id_factory=lambda prefix, *parts: deterministic_id("custom-"+prefix, *parts)) as r:
        assert r.run().state == "succeeded"
        assert r.executor.clock is r.evaluator.clock is r.journal.clock is r.clock
        assert all(id.startswith("custom-") for id in r.projection.attempts)


@pytest.mark.parametrize("mode", ["omit", "wrong_contract", "declared_digest_mismatch"])
def test_invalid_outputs_recovered_after_ack_loss(tmp_path, mode):
    data = inputs()
    data["document"]["default"].update(dispatch="ack_lost", outputs={"report": mode})
    with run_case(tmp_path, data) as r:
        assert r.projection.state == "failed"
        assert r.projection.latest_attempt("T1").state == "failed"
        assert not events(r, "outputs_accepted")


def test_S15_report_with_different_digests_is_rejected_before_persistence(tmp_path, monkeypatch):
    from dataclasses import replace
    from orq_contracts.evaluator import DeterministicEvaluator
    original = DeterministicEvaluator.evaluate
    def mismatched(self, **kwargs):
        result = original(self, **kwargs)
        report = deepcopy(result.report)
        report["subject_bindings"][0]["artifact"]["content_digest"] = canonical_digest("different")
        report["record_hash"] = canonical_digest({k: v for k, v in report.items() if k != "record_hash"})
        return replace(result, report=report)
    monkeypatch.setattr(DeterministicEvaluator, "evaluate", mismatched)
    with make_runner(tmp_path, inputs(full=True)) as r:
        with pytest.raises(ProjectionError, match="evaluation_bindings_mismatch"):
            r.run()
        assert r.projection.tasks["T2"].state == "pending"
        assert not events(r, "outputs_accepted") and not events(r, "evaluation_report_recorded")


def test_crash_after_external_effect_before_ack_does_not_redispatch(tmp_path):
    with make_runner(tmp_path) as r:
        original = r.append
        def crash(event_type, *args, **kwargs):
            if event_type == "dispatch_acknowledged":
                raise InjectedCrash()
            return original(event_type, *args, **kwargs)
        r.append = crash
        with pytest.raises(InjectedCrash):
            r.run()
        assert r.executor.execution_count == 1
    with make_runner(tmp_path, resume=True) as r:
        assert r.run().state == "succeeded"
        assert r.executor.execution_count == r.executor.dispatch_count == 1
        assert len(events(r, "dispatch_requested")) == 1


def test_failed_intention_write_prevents_dispatch(tmp_path):
    with make_runner(tmp_path) as r:
        original = r.journal.append
        def fail(**kwargs):
            if kwargs["event_type"] == "dispatch_requested":
                raise OSError("falha injetada antes do append")
            return original(**kwargs)
        r.journal.append = fail
        with pytest.raises(OSError):
            r.run()
        assert r.executor.dispatch_count == 0
        assert not events(r, "dispatch_requested")


def test_S10_retry_rejects_revision_outside_frozen_catalog(tmp_path, monkeypatch):
    from orq_contracts import runner as module
    from orq_contracts.context import ContextError
    original = module.select_knowledge
    original_materialize = module.materialize_knowledge_context
    failures = []
    data = inputs(full=True)
    data['workflow']['tasks'][2]['knowledge_requirements'] = load_fixture('vault/selection-request.json')['requirements']
    def outside(*args, **kwargs):
        selection = original(*args, **kwargs)
        if kwargs['task_id'] == 'T3' and kwargs['candidate_attempt_number'] == 2:
            assert selection['selected_units']
            for item in [*selection['selected_units'], *selection['candidates']]:
                item['unit_ref']['revision'] = 'outside-catalog'
            selection['selection_hash'] = canonical_digest({k: v for k, v in selection.items() if k != 'selection_hash'})
        return selection
    monkeypatch.setattr(module, 'select_knowledge', outside)
    def materialize(*args, **kwargs):
        try:
            return original_materialize(*args, **kwargs)
        except ContextError as error:
            selection = args[2]
            failures.append((selection['task_id'], selection['candidate_attempt_number'], error.code))
            raise
    monkeypatch.setattr(module, 'materialize_knowledge_context', materialize)
    with run_case(tmp_path, data) as r:
        assert failures == [('T3', 2, 'selected_unit_missing')]
        selections = events(r, 'knowledge_selection_recorded', 'task', 'T3')
        assert len(selections) == 2
        assert all(e['payload']['catalog_snapshot_ref'] == r.projection.manifest['knowledge_catalog_snapshot_ref'] for e in selections)
        assert selections[-1]['payload']['selected_units'][0]['unit_ref']['revision'] == 'outside-catalog'
        assert r.projection.tasks['T3'].state == 'failed'
        assert len(r.projection.tasks['T3'].attempt_ids) == 1
        assert not events(r, 'dispatch_requested', 'attempt', 'attempt-T3-2')
        assert len(events(r, 'context_manifest_recorded', 'task', 'T3')) == 1
        assert events(r, 'admission_rejected', 'task', 'T3')[-1]['payload']['reason'] == 'context_rejected'


@pytest.mark.parametrize('boundary', ['dispatch_requested', 'execution_started', 'termination_confirmed', 'budget_reserved', 'reevaluation'])
def test_recovery_repairs_crash_before_stopping(tmp_path, boundary):
    data = inputs()
    if boundary == 'termination_confirmed':
        data['document']['default'].update(execution='hang', on_cancel='confirmed', reconcile='found_stopped')
    if boundary == 'execution_started':
        data['document']['default'].update(execution='hang', on_cancel='unconfirmed', reconcile='found_stopped')
    if boundary == 'reevaluation':
        data['document']['default']['verifier']['behavior-ok'] = ['inconclusive', 'pass']
    def crash(r, e):
        match = (e['event_type'] == 'evaluation_requested' and e['payload']['evaluation_number'] == 2) if boundary == 'reevaluation' else e['event_type'] == boundary
        if boundary == 'execution_started':
            match = e['event_type'] == 'state_transition' and e['aggregate_type'] == 'attempt' and e['payload']['to'] == 'running'
        if match:
            r.cancel()
            raise InjectedCrash()
    with make_runner(tmp_path, data, on_event=crash) as r:
        with pytest.raises(InjectedCrash):
            r.run()
    with make_runner(tmp_path, data, resume=True) as r:
        assert r.run().state == 'cancelled'
        stop = next(e['global_sequence'] for e in events(r, 'state_transition', 'run') if e['payload']['to'] == 'stopping')
        if boundary in {'dispatch_requested', 'execution_started'}:
            unknown = next(e for e in events(r, 'state_transition', 'attempt') if e['payload']['to'] == 'unknown')
            assert unknown['payload']['reason'] == 'crash' and unknown['global_sequence'] < stop
            assert len(events(r, 'dispatch_requested')) == 1
            assert events(r, 'reconciliation_requested')
        elif boundary == 'termination_confirmed':
            assert len(events(r, 'termination_confirmed')) == 1
            assert r.projection.latest_attempt('T1').state == 'timed_out'
            assert len(events(r, 'cancel_requested', 'attempt')) == 1
        elif boundary == 'reevaluation':
            request = events(r, 'evaluation_requested')[-1]
            report = events(r, 'evaluation_report_recorded')[-1]
            assert report['payload']['verdict'] == 'not_run'
            assert report['aggregate_id'] == request['aggregate_id']
            assert report['global_sequence'] < stop
            assert ('waiting_evaluation', 'evaluating') in edges(r, 'task', 'T1')
        else:
            assert not r.projection.attempts
            assert events(r, 'budget_released')[0]['global_sequence'] < stop
        assert 'idempotency_conflict' not in {e['code'] for e in r.projection.errors.values()}
        assert_reservations_closed(r)


@pytest.mark.parametrize('status', ['planning_failed', 'cancelled_before_ready'])
def test_recovery_finishes_non_ready_manifest_directly(tmp_path, status):
    data = inputs()
    if status == 'planning_failed':
        data['request']['objective'] = 'Outro objetivo'
    def crash(r, e):
        if e['event_type'] == 'run_intent_recorded' and status == 'cancelled_before_ready':
            r.cancel()
        if e['event_type'] == 'run_manifest_closed':
            raise InjectedCrash()
    with make_runner(tmp_path, data, on_event=crash) as r:
        with pytest.raises(InjectedCrash):
            r.run()
        assert r.projection.state == 'running'
    with make_runner(tmp_path, data, resume=True) as r:
        assert r.run().state == ('failed' if status == 'planning_failed' else 'cancelled')
        assert ('running', r.projection.state) in edges(r, 'run', r.projection.id)
        assert not any(to == 'stopping' for _, to in edges(r, 'run', r.projection.id))
        assert not r.projection.attempts
        assert r.projection.published_summary['cost_summary']['currency'] is None


@pytest.mark.parametrize('outcome', ['found_failed', 'found_stopped', 'not_found', 'found_finished'])
def test_recovery_uses_conclusive_observation_without_reconcile(tmp_path, outcome, monkeypatch):
    data = inputs()
    override = dict(dispatch='ack_lost', execution='fail_retryable', reconcile=outcome)
    if outcome == 'found_stopped':
        override = dict(execution='hang', on_cancel='unconfirmed', reconcile=outcome)
    elif outcome == 'not_found':
        override = dict(dispatch='not_executed', reconcile=outcome)
    elif outcome == 'found_finished':
        override['execution'] = 'finish'
    data['document']['attempts'] = {'T1': {'1': override}}
    hook(data, 'reconciliation_observed', action='crash_after')
    with pytest.raises(InjectedCrash):
        run_case(tmp_path, data)
    with make_runner(tmp_path, data, resume=True) as r:
        def forbidden(*args, **kwargs):
            raise AssertionError('observação conclusiva não pode ser repetida')
        monkeypatch.setattr(r.executor, 'reconcile', forbidden)
        assert r.run().state == 'succeeded'
        assert len(events(r, 'reconciliation_requested')) == len(events(r, 'reconciliation_observed')) == 1
        assert_reservations_closed(r)
        aid = 'attempt-T1-1'
        terminal = events(r, 'state_transition', 'attempt', aid)[-1]['global_sequence']
        releases = [e for e in events(r, 'budget_released') if r.projection.reservations[e['payload']['reservation_id']].reservation['scope_ref'] == aid]
        settlements = [e for e in events(r, 'budget_settled') if r.projection.reservations[e['payload']['reservation_id']].reservation['scope_ref'] == aid]
        assert len(releases + settlements) == 1
        assert (releases + settlements)[0]['global_sequence'] < terminal
        assert all(e['global_sequence'] < terminal for e in events(r, 'usage_attached', 'attempt', aid))


@pytest.mark.parametrize('mode', ['cancel', 'timeout', 'failed', 'not_found_cancel', 'reconciled_cancel'])
def test_terminal_attempt_has_usage_or_absence_settled_before_crash(tmp_path, mode):
    data = inputs()
    if mode in {'cancel', 'reconciled_cancel'}:
        data['document']['default'].update(execution='hang', on_cancel='confirmed' if mode == 'cancel' else 'unconfirmed', reconcile='found_stopped')
    elif mode == 'timeout':
        data['document']['attempts'] = {'T1': {'1': dict(execution='hang', on_cancel='confirmed', reconcile='found_stopped')}}
    elif mode == 'failed':
        data['document']['attempts'] = {'T1': {'1': dict(dispatch='ack_lost', execution='fail_retryable', reconcile='found_failed')}}
    else:
        data['document']['default'].update(dispatch='not_executed', reconcile='not_found')
    def crash(r, e):
        if e['event_type'] == ('reconciliation_requested' if mode == 'not_found_cancel' else 'execution_started') and mode in {'cancel', 'reconciled_cancel', 'not_found_cancel'}:
            r.cancel()
        if e['event_type'] == 'state_transition' and e['aggregate_type'] == 'attempt' and e['payload']['to'] in {'failed', 'timed_out', 'cancelled'}:
            a = r.projection.attempts[e['aggregate_id']]
            reservation = r.projection.reservations[a.intent['budget_reservation_ids'][0]]
            assert reservation.state == ('released' if mode == 'not_found_cancel' else 'settled')
            raise InjectedCrash()
    with make_runner(tmp_path, data, on_event=crash) as r:
        with pytest.raises(InjectedCrash):
            r.run()
    with make_runner(tmp_path, data, resume=True) as r:
        assert r.run().state == ('cancelled' if 'cancel' in mode else 'succeeded')
        assert_reservations_closed(r)


@pytest.mark.parametrize('scope', ['attempt', 'evaluation'])
def test_optional_budget_rejects_unknown_estimate(tmp_path, scope):
    data = budget(inputs(), attempt=None if scope == 'attempt' else '0.30', evaluation=None if scope == 'evaluation' else '0.10')
    data['policy']['budget_snapshot'].update(cap_mandatory=False, unknown_cost_action='reject')
    with run_case(tmp_path, data) as r:
        assert r.projection.state == 'failed'
        assert events(r, 'admission_rejected')[0]['payload']['reason'] == 'budget_unknown'
        assert not r.projection.attempts and not events(r, 'budget_reserved')


def test_optional_max_cost_checks_known_subtotal_with_unknown_gap(tmp_path):
    data = budget(inputs(full=True), attempt=None, evaluation='0.01', cap='0.15')
    data['policy']['budget_snapshot'].update(cap_mandatory=False, unknown_cost_action='admit_with_estimate')
    data['document']['attempts'] = {'T1': {'1': {'usage': {'cost': {'status': 'unavailable', 'amount': None, 'currency': 'USD'}}}},
                                    'T2': {'1': {'usage': {'cost': {'status': 'reported', 'amount': '0.20', 'currency': 'USD'}}}}}
    with run_case(tmp_path, data) as r:
        assert r.projection.state == 'failed'
        assert len(r.projection.attempts) == 2
        assert events(r, 'admission_rejected', 'task', 'T3')[0]['payload']['reason'] == 'budget_exceeded'
        assert r.projection.budget_committed is None and r.projection.budget_gaps
        assert r.projection.cost_summary['reported'] == '0.20'
        assert r.projection.cost_summary['unavailable_count'] == 1


@pytest.mark.parametrize('timeout_on_repeat', [False, True])
def test_repeated_cancel_has_fresh_durable_intention(tmp_path, monkeypatch, timeout_on_repeat):
    data = inputs()
    data['document']['default'].update(execution='hang', on_cancel='unconfirmed', reconcile='found_running')
    with make_runner(tmp_path, data) as r:
        r._prepare()
        r._transition('task', 'T1', 'ready', 'dependencies_accepted')
        r._admit('T1')
        aid = r.projection.latest_attempt('T1').id
        r.cancel()
        r._transition('run', r.projection.id, 'stopping', 'run_cancelled')
        original = r.executor.cancel
        seen = []
        def cancel(intent, reason):
            intentions = events(r, 'cancel_requested', 'attempt', aid)
            assert len(intentions) == len(seen) + 1
            seen.append(intentions[-1]['idempotency_key'])
            observation = original(intent, reason)
            if timeout_on_repeat:
                r.clock.advance_to(intent['deadline_at'])
            return observation
        monkeypatch.setattr(r.executor, 'cancel', cancel)
        r._cancel_attempt(aid)
        r._reconcile(aid)
        assert seen == [r.projection.attempts[aid].intent['dispatch_idempotency_key'] + '/cancel/1',
                        r.projection.attempts[aid].intent['dispatch_idempotency_key'] + '/cancel/2']
        reasons = [e['payload']['reason'] for e in events(r, 'cancel_requested', 'attempt', aid)]
        assert reasons == ['run_cancelled', 'attempt_timeout' if timeout_on_repeat else 'run_cancelled']


@pytest.mark.parametrize('late_cancel', [False, True])
def test_found_stopped_without_prior_cancel_fails_during_stopping(tmp_path, late_cancel):
    data = inputs()
    data['document']['default'].update(dispatch='ack_lost', execution='hang', reconcile='found_stopped')
    def stop(r, e):
        if e['event_type'] == 'reconciliation_requested':
            r._error('run', r.projection.id, 'idempotency_conflict', phase='persistence')
            r._transition('run', r.projection.id, 'stopping', 'idempotency_conflict')
        if late_cancel and e['event_type'] == 'reconciliation_observed':
            r.cancel()
    with run_case(tmp_path, data, callback=stop) as r:
        a = r.projection.latest_attempt('T1')
        assert r.projection.state == 'failed' and a.state == 'failed'
        assert ('unknown', 'failed') in edges(r, 'attempt', a.id)
        error = r.projection.errors[a.failure_error_id]
        assert error['code'] == 'execution_failed' and error['retryability'] == 'unknown'
        assert bool(events(r, 'cancel_requested')) is late_cancel
        if late_cancel:
            assert events(r, 'cancel_requested')[0]['global_sequence'] > events(r, 'reconciliation_observed')[0]['global_sequence']
        assert_reservations_closed(r)


@pytest.mark.parametrize('scope', ['task_attempt', 'workflow'])
def test_recovery_releases_reported_evaluation_reservation(tmp_path, scope):
    def crash(r, e):
        if e['event_type'] == 'evaluation_report_recorded' and e['payload']['scope']['kind'] == scope:
            if scope == 'task_attempt':
                r.cancel()
            raise InjectedCrash()
    with make_runner(tmp_path, on_event=crash) as r:
        with pytest.raises(InjectedCrash):
            r.run()
    with make_runner(tmp_path, resume=True) as r:
        assert r.run().state == ('cancelled' if scope == 'task_attempt' else 'succeeded')
        assert_reservations_closed(r)


def test_completed_during_stopping_records_not_run_before_TK24(tmp_path):
    data = inputs()
    data['document']['default'].update(dispatch='ack_lost', reconcile='found_finished')
    hook(data, 'reconciliation_requested')
    with run_case(tmp_path, data) as r:
        assert r.projection.state == 'cancelled'
        report, = events(r, 'evaluation_report_recorded')
        assert (report['payload']['execution_status'], report['payload']['verdict']) == ('failed', 'not_run')
        assert report['global_sequence'] < events(r, 'state_transition', 'task', 'T1')[-1]['global_sequence']
        assert not events(r, 'outputs_accepted')
        assert_reservations_closed(r)


@pytest.mark.parametrize('boundary', ['error_attached', 'stopping'])
def test_global_uncertainty_survives_crash(tmp_path, boundary):
    data = inputs()
    data['document']['workflow_verifier']['workflow-behavior-ok'] = 'unknown_termination'
    def crash(r, e):
        if boundary == 'error_attached':
            match = e['event_type'] == 'error_attached' and e['aggregate_type'] == 'evaluation' and r.projection.evaluations[e['aggregate_id']].request['scope']['kind'] == 'workflow'
        else:
            match = e['event_type'] == 'state_transition' and e['aggregate_type'] == 'run' and e['payload']['to'] == 'stopping'
        if match:
            raise InjectedCrash()
    with make_runner(tmp_path, data, on_event=crash) as r:
        with pytest.raises(InjectedCrash):
            r.run()
    with make_runner(tmp_path, data, resume=True) as r:
        assert r.run().state == 'indeterminate'
        assert r.projection.global_report is None
        global_ev, = [ev for ev in r.projection.evaluations.values() if ev.request['scope']['kind'] == 'workflow']
        assert all(r.projection.reservations[rid].state == 'open' for rid in global_ev.request['budget_reservation_ids'])
        assert len(events(r, 'evaluation_requested')) == 2
        assert len(events(r, 'evaluation_report_recorded')) == 1


def test_tail_audit_clock_includes_newer_executor_checkpoint(tmp_path):
    with make_runner(tmp_path) as r:
        original = r.append
        def crash(type, *args, **kwargs):
            if type == 'termination_confirmed':
                raise InjectedCrash()
            return original(type, *args, **kwargs)
        r.append = crash
        with pytest.raises(InjectedCrash):
            r.run()
        r.clock.advance_to(format_utc(parse_utc(r.clock.now()) + timedelta(seconds=10)))
        r._persist_executor(r.executor.export_state())
        latest = json.loads(sorted(r._state_directory.glob('state-*.json'))[-1].read_bytes())['clock']
        path = r.journal.path
    with path.open('ab') as f:
        f.write(b'{"partial":')
    with make_runner(tmp_path, resume=True) as r:
        audit, = events(r, 'journal_tail_discarded')
        assert parse_utc(audit['recorded_at']) > parse_utc(latest)
        assert r.run().state == 'succeeded'


def test_S11_context_token_budget_rejection_precedes_reservations(tmp_path, monkeypatch):
    from orq_contracts import runner as module
    from orq_contracts.context import ContextError
    original = module.materialize_knowledge_context
    failures = []
    def exceed(*args, **kwargs):
        kwargs['target_context_limit_tokens'] = kwargs['reserved_output_tokens'] + 1
        try:
            return original(*args, **kwargs)
        except ContextError as error:
            failures.append(error.code)
            raise
    monkeypatch.setattr(module, 'materialize_knowledge_context', exceed)
    with run_case(tmp_path, knowledge(inputs())) as r:
        assert failures == ['context_budget_exceeded']
        assert r.projection.state == 'failed'
        assert events(r, 'admission_rejected')[0]['payload']['reason'] == 'context_rejected'
        assert not events(r, 'context_manifest_recorded')
        assert not events(r, 'budget_reserved') and not r.projection.attempts


def test_S40_interrupted_evaluation_at_limit_cannot_start_another(tmp_path):
    data = inputs()
    data['policy']['resolved_limits']['max_evaluations_per_attempt'] = 1
    hook(data, 'evaluation_requested', action='crash_after')
    with pytest.raises(InjectedCrash):
        run_case(tmp_path, data)
    with make_runner(tmp_path, data, resume=True) as r:
        assert r.run().state == 'failed'
        assert len(events(r, 'evaluation_requested')) == 1
        report, = events(r, 'evaluation_report_recorded')
        assert (report['payload']['execution_status'], report['payload']['verdict']) == ('failed', 'not_run')
        assert r.projection.tasks['T1'].state == 'failed'
        assert_reservations_closed(r)


def test_recovery_reprojects_new_reservation_actions_before_stopping(tmp_path):
    data = inputs()
    data['document']['default'].update(dispatch='reject_confirmed', reconcile='not_found')
    def crash(r, e):
        if e['event_type'] == 'error_attached' and e['payload']['code'] == 'dispatch_failed':
            r.cancel()
            raise InjectedCrash()
    with make_runner(tmp_path, data, on_event=crash) as r:
        with pytest.raises(InjectedCrash):
            r.run()
        assert r.projection.latest_attempt('T1').state == 'queued'
    with make_runner(tmp_path, data, resume=True) as r:
        assert r.run().state == 'cancelled'
        assert r.projection.latest_attempt('T1').state == 'failed'
        stop = next(e['global_sequence'] for e in events(r, 'state_transition', 'run') if e['payload']['to'] == 'stopping')
        releases = events(r, 'budget_released')
        assert len(releases) == 2 and all(e['global_sequence'] < stop for e in releases)
        assert r.executor.execution_count == 0
        assert not events(r, 'reconciliation_requested')
        assert_reservations_closed(r)


def test_cancel_during_global_evaluation_reservation_releases_it(tmp_path):
    """Regressão (revisão de confirmação do 6E): cancelamento gravado logo após a reserva da avaliação global."""
    def cancel_on_global_reservation(runner, event):
        p = event["payload"]
        if (event["event_type"] == "budget_reserved" and p["scope_type"] == "evaluation"
                and p["scope_ref"] == "evaluation-workflow-1" and not runner.projection.cancel_requested):
            runner.cancel()
    with run_case(tmp_path, inputs(), callback=cancel_on_global_reservation) as r:
        assert r.projection.state == "cancelled"
        assert not [e for e in events(r, "evaluation_requested") if e["aggregate_id"] == "evaluation-workflow-1"]
        reserved = [e["payload"]["reservation_id"] for e in events(r, "budget_reserved")
                    if e["payload"]["scope_ref"] == "evaluation-workflow-1"]
        assert reserved and all(r.projection.reservations[rid].state == "released" for rid in reserved)
