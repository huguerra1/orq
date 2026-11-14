from __future__ import annotations

import copy
import json
import socket
from dataclasses import replace

import pytest

from conftest import ROOT, load_fixture
from orq_contracts.artifact_store import ArtifactStore
from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.executor import ExecutorAdapter, ExecutorError, WorkspaceHandle, bytes_digest
from orq_contracts.journal import RunJournal
from orq_contracts.schemas import load_schemas, validator_for
from orq_contracts.simulated_executor import (
    AUTHORITATIVE_NOT_FOUND_CONTROL, DeterministicClock, HookController, ScriptError, SimulationScript,
    SimulatedExecutor, simulated_output_contract,
)
from orq_contracts.validator import validate_instance, validate_workflow


# Digests SHA-256 sobre JCS fixados como os GOLDEN_HASHES do 6A.
# Conferir o diff da fixture antes de atualizar qualquer valor.
PINNED_DIGESTS = {
    "fixtures/v0.1/valid/simulation-script.json": "4c4cd4468a2465241d86c2b368d372e453a83e4faabb0ce8cf3a6838b2af1062",
    "fixtures/v0.1/valid/simulation-script-recovery.json": "3dd30868eb5ea30a03904776d42ec4c9df2f94321e383016ef8dd38ad987129d",
    "schemas/v0.1/simulated-output.schema.json": "c922f3fad89336ebea4c47b536d9f87d627141d15df12909d4b113941a82be62",
}
DECLARATION = "https://orq.local/schemas/v0.1/routing-target-catalog.schema.json#/$defs/declaration"


def script_document():
    return load_fixture("valid/simulation-script.json")


def signed_script(document=None):
    """Assina documentos mutados pelo teste; a fixture versionada usa PINNED_DIGESTS."""
    document = document if document is not None else script_document()
    return SimulationScript(document, expected_digest=canonical_digest(document))


def resign(record, field="record_hash"):
    record[field] = canonical_digest({k: v for k, v in record.items() if k != field})
    return record


def assert_record(record, schema):
    assert not validate_instance(record, schema)
    assert record["record_hash"] == canonical_digest({k: v for k, v in record.items() if k != "record_hash"})


def output_spec(output_id="report"):
    """OutputSpec da TaskSpec com a referência real do envelope simulado."""
    spec = copy.deepcopy(load_fixture("valid/task-minimal.json")["expected_output"][0])
    spec["output_id"] = output_id
    spec["content_contract_ref"] = simulated_output_contract()[0]
    return spec


def block_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("acesso à rede")
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, forbidden)
    for name in ("connect", "connect_ex", "sendto", "sendall", "send"):
        monkeypatch.setattr(socket.socket, name, forbidden)


def build_simulation(root, document=None, *, clock=None, persist=None, state=None):
    script = signed_script(document)
    clock = clock if clock is not None else script.make_clock()
    journal = RunJournal.create(root, "run-demo-001", clock=clock)
    try:
        store = ArtifactStore(journal)
        if persist is not None and state is None:
            state = SimulatedExecutor.initial_state(script, journal.run_id)
        executor = SimulatedExecutor(script, store, clock=clock, persist=persist, state=state)
        bundle = b"bundle opaco: nao precisa ser JSON"
        intent = load_fixture("valid/dispatch-intent.json")
        intent.update(task_id="T1", attempt_number=1, attempt_id="attempt-T1-1",
                      dispatch_intent_id="dispatch-T1-1", dispatch_idempotency_key="run-demo-001/dispatch-T1-1",
                      bundle_hash=bytes_digest(bundle))
        resign(intent)
        workspace = WorkspaceHandle("workspace-T1-1", intent["workspace_base_ref"], (output_spec(),), "requested-model")
        return journal, executor, intent, bundle, workspace
    except BaseException:
        journal.close()
        raise


@pytest.fixture
def simulation(tmp_path):
    resources = []
    def create(document=None, **kwargs):
        value = build_simulation(tmp_path / str(len(resources)), document, **kwargs)
        resources.append(value[0])
        return value[1:]
    yield create
    for journal in resources:
        journal.close()


def finish(executor, intent, bundle, workspace):
    ack = executor.dispatch(intent, bundle, workspace)
    observation = executor.observe(intent, "2026-11-10T12:00:02Z")
    return ack, observation, executor.collect(intent)


def simulated_workflow():
    """workflow-minimal com T1–T3, envelope simulado e critérios simulated_verdict."""
    workflow = load_fixture("valid/workflow-minimal.json")
    task = workflow["tasks"][0]
    task["expected_output"][0]["content_contract_ref"] = simulated_output_contract()[0]
    task["evaluation_criteria"] = [{
        "criterion_id": "CRIT1", "requirement_ids": ["REQ1"], "method": "simulated_verdict",
        "pass_rule": {"script_key": "behavior-ok"}, "required": True, "verifier_ref": None,
    }]
    for task_id in ("T2", "T3"):
        workflow["tasks"].append({**copy.deepcopy(task), "task_id": task_id})
    workflow["evaluation_criteria"] = [{
        "criterion_id": "WCRIT1", "workflow_requirement_ids": ["WREQ1"], "output_ids": ["final-report"],
        "method": "simulated_verdict", "pass_rule": {"script_key": "workflow-behavior-ok"},
        "required": True, "verifier_ref": None,
    }]
    return workflow


@pytest.mark.parametrize("relative", PINNED_DIGESTS)
def test_fixture_and_contract_digests_are_pinned(relative):
    document = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    actual = canonical_digest(document)["value"]
    assert actual == PINNED_DIGESTS[relative], f"digest JCS de {relative}: {actual}"
    if relative.startswith("schemas/"):
        assert simulated_output_contract()[0]["content_digest"]["value"] == PINNED_DIGESTS[relative]
    else:
        pinned = {"algorithm": "sha256", "value": PINNED_DIGESTS[relative]}
        script = SimulationScript.from_bytes((ROOT / relative).read_bytes(), expected_digest=pinned)
        assert script.reference["content_digest"] == pinned


def test_versioned_fixture_and_protocol(simulation):
    assert validator_for("simulation_script").is_valid(script_document())
    executor, intent, bundle, workspace = simulation()
    assert isinstance(executor, ExecutorAdapter)
    before = executor.clock.now()
    description = executor.describe()
    assert description.adapter_version == "0.1"
    declaration = validator_for("routing_target_catalog").evolve(schema={"$ref": DECLARATION})
    assert all(declaration.is_valid(control) for control in description.controls)
    supported = {control["id"] for control in description.controls if control["support"] == "supported"}
    assert set(load_fixture("routing-request.json")["required_control_ids"]) <= supported
    assert all(control["evidence_refs"] for control in description.controls if control["support"] == "supported")
    assert executor.clock.now() == before
    ack, observation, result = finish(executor, intent, bundle, workspace)
    assert ack.acknowledged and observation.status == "finished"
    assert result.requested_model_id == "requested-model"
    assert result.resolved_model_id == "sim-model-1"
    assert result.provider_context_coverage == "platform_only"
    assert not executor.store.journal.events  # Não grava eventos; 6E faz isso.
    assert_record(result.usage[0], "usage_record")


@pytest.mark.parametrize("mode", ["ack", "ack_lost", "reject_confirmed", "not_executed"])
def test_dispatch_values(simulation, mode):
    document = script_document()
    document["default"]["dispatch"] = mode
    document["default"]["reconcile"] = "unknown"
    executor, intent, bundle, workspace = simulation(document)
    ack = executor.dispatch(intent, bundle, workspace)
    assert ack.acknowledged == (mode == "ack")
    assert bool(ack.external_call_id) == (mode == "ack")
    assert executor.dispatch_count == 1
    assert executor.execution_count == (mode in {"ack", "ack_lost"})
    assert bool(ack.errors) == (mode == "reject_confirmed")
    if ack.errors:
        error = ack.errors[0]
        assert (error["code"], error["retryability"], error["termination_certainty"]) == (
            "dispatch_failed", "retryable", "confirmed")
        assert_record(error, "error_record")
    if mode in {"reject_confirmed", "not_executed"}:
        assert executor.observe(intent, "2026-11-10T12:00:02Z").status == "unknown"
        with pytest.raises(ExecutorError, match="result_unavailable"):
            executor.collect(intent)


@pytest.mark.parametrize("mode", ["finish", "fail_retryable", "fail_non_retryable", "hang"])
def test_execution_values_and_duration(simulation, mode):
    document = script_document()
    document["default"]["execution"] = mode
    document["default"]["reconcile"] = "unknown"
    executor, intent, bundle, workspace = simulation(document)
    executor.dispatch(intent, bundle, workspace)
    early = executor.observe(intent, "2026-11-10T12:00:00.500Z")
    assert (early.status, early.termination_certainty) == ("running", "not_terminated")
    late = executor.observe(intent, "2026-11-10T12:00:02Z")
    assert late.status == {"finish": "finished", "fail_retryable": "failed", "fail_non_retryable": "failed", "hang": "running"}[mode]
    if mode.startswith("fail_"):
        assert late.errors[0]["retryability"] == mode.removeprefix("fail_")
        assert late.errors[0]["termination_certainty"] == "confirmed"
        assert_record(late.errors[0], "error_record")
        result = executor.collect(intent)
        assert not result.produced_files
        assert result.errors == late.errors
        assert_record(result.usage[0], "usage_record")
    if mode != "hang":
        assert late.observed_at == "2026-11-10T12:00:01.000000Z"
    else:
        with pytest.raises(ExecutorError, match="result_unavailable"):
            executor.collect(intent)


@pytest.mark.parametrize("on_cancel", ["confirmed", "unconfirmed"])
def test_cancel_values(simulation, on_cancel):
    document = script_document()
    document["default"].update(execution="hang", on_cancel=on_cancel, reconcile="found_running")
    executor, intent, bundle, workspace = simulation(document)
    executor.dispatch(intent, bundle, workspace)
    observation = executor.cancel(intent, "timeout")
    assert observation.termination_certainty == ("confirmed" if on_cancel == "confirmed" else "unknown")
    after = executor.observe(intent, "2026-11-10T12:00:02Z")
    # Parada confirmada tem status explícito, nunca unknown com certeza confirmed.
    assert (after.status, after.termination_certainty) == (
        ("stopped", "confirmed") if on_cancel == "confirmed" else ("running", "not_terminated"))
    if on_cancel == "confirmed":
        assert not executor.collect(intent).produced_files


@pytest.mark.parametrize("on_cancel", ["confirmed", "unconfirmed"])
@pytest.mark.parametrize("execution,status", [("finish", "finished"), ("fail_retryable", "failed")])
def test_cancel_after_elapsed_duration_keeps_scripted_termination(simulation, on_cancel, execution, status):
    document = script_document()
    document["default"].update(execution=execution, on_cancel=on_cancel, reconcile="found_running")
    executor, intent, bundle, workspace = simulation(document)
    executor.dispatch(intent, bundle, workspace)
    executor.clock.advance_to("2026-11-10T12:00:05Z")  # Duração de 1000 ms já passou, sem observe.
    assert executor.cancel(intent, "timeout").termination_certainty == "confirmed"
    observed = executor.observe(intent, executor.clock.now())
    assert (observed.status, observed.termination_certainty) == (status, "confirmed")
    result = executor.collect(intent)
    assert bool(result.produced_files) == (execution == "finish")
    reconciled = executor.reconcile(intent["dispatch_idempotency_key"], None)
    assert reconciled.outcome == "found_" + status


@pytest.mark.parametrize("outcome", ["found_running", "found_finished", "found_failed", "found_stopped", "not_found", "unknown"])
@pytest.mark.parametrize("authoritative", [True, False])
@pytest.mark.parametrize("durable", [True, False])
def test_reconciliation_values(simulation, outcome, authoritative, durable):
    document = script_document()
    document["reconcile_not_found_authoritative"] = authoritative
    document["default"].update(dispatch="not_executed" if outcome == "not_found" else "ack_lost",
                                execution="fail_non_retryable" if outcome == "found_failed" else "hang",
                                reconcile=outcome, on_cancel="unconfirmed")
    saved = []
    executor, intent, bundle, workspace = simulation(document, persist=saved.append if durable else None)
    ack = executor.dispatch(intent, bundle, workspace)
    executor.cancel(intent, "timeout")
    result = executor.reconcile(intent["dispatch_idempotency_key"], ack.external_call_id)
    assert result.outcome == outcome
    assert result.authoritative == (outcome == "not_found" and authoritative and durable)
    certainty = "confirmed" if outcome in {"found_finished", "found_failed", "found_stopped"} or result.authoritative else (
        "not_terminated" if outcome == "found_running" else "unknown")
    assert result.termination_certainty == certainty
    assert bool(result.evidence_refs) == (outcome != "unknown")
    assert executor.dispatch_count == 1
    assert bool(saved) == durable
    if outcome == "found_finished":
        assert executor.collect(intent).produced_files
    if outcome == "found_failed":
        assert result.errors[0]["retryability"] == "non_retryable"
        assert_record(result.errors[0], "error_record")
    if outcome == "found_stopped":
        assert not executor.collect(intent).produced_files
    control = next(c for c in executor.describe().controls if c["id"] == AUTHORITATIVE_NOT_FOUND_CONTROL)
    assert control["support"] == ("supported" if authoritative and durable else "unsupported")
    if outcome != "not_found":
        with pytest.raises(ExecutorError, match="external_call_mismatch"):
            executor.reconcile(intent["dispatch_idempotency_key"], "wrong-call")


@pytest.mark.parametrize("authoritative", [True, False])
@pytest.mark.parametrize("durable", [True, False])
def test_unknown_key_is_denied_only_with_durable_state(simulation, authoritative, durable):
    document = script_document()
    document["reconcile_not_found_authoritative"] = authoritative
    saved = []
    executor, intent, _, _ = simulation(document, persist=saved.append if durable else None)
    result = executor.reconcile(intent["dispatch_idempotency_key"], None)
    if authoritative and durable:
        assert (result.outcome, result.authoritative, result.termination_certainty) == ("not_found", True, "confirmed")
        assert result.evidence_refs == (executor.script.reference,)
    else:
        assert (result.outcome, result.authoritative, result.termination_certainty) == ("unknown", False, "unknown")
        assert not result.evidence_refs
    assert executor.dispatch_count == executor.execution_count == 0
    assert bool(saved) == durable
    if durable:
        restarted = SimulatedExecutor(executor.script, executor.store, clock=executor.clock,
                                      persist=saved.append, state=json.loads(json.dumps(saved[-1])))
        restored = restarted.reconcile(intent["dispatch_idempotency_key"], None)
        assert (restored.outcome, restored.authoritative) == (result.outcome, result.authoritative)


@pytest.mark.parametrize("outcome", ["unknown", "found_running", "found_stopped"])
@pytest.mark.parametrize("execution", ["finish", "fail_retryable"])
@pytest.mark.parametrize("cancel_first", [False, True])
def test_reconciliation_honors_script_after_duration(simulation, outcome, execution, cancel_first):
    document = script_document()
    document["clock"]["step_ms"] = 1000
    document["default"].update(execution=execution, reconcile=outcome, on_cancel="unconfirmed")
    if cancel_first:
        document["default"]["duration_ms"] = 3000
    executor, intent, bundle, workspace = simulation(document)
    executor.dispatch(intent, bundle, workspace)
    if cancel_first:
        # Cancela antes do término; a reconciliação ocorrerá depois da duração.
        assert executor.cancel(intent, "timeout").termination_certainty == "unknown"
    executor.clock.advance_to("2026-11-10T12:00:10Z")
    reconciled = executor.reconcile(intent["dispatch_idempotency_key"], None)
    assert reconciled.outcome == outcome
    terminal = next(iter(executor.export_state()["calls"].values()))["terminal"]
    assert terminal == ("stopped" if outcome == "found_stopped" else None)
    # Um término confirmado pela reconciliação é preservado na chamada seguinte.
    assert executor.reconcile(intent["dispatch_idempotency_key"], None).outcome == outcome


def test_initial_state_must_be_persisted_before_authoritative_absence(simulation):
    executor, *_ = simulation()
    def failing(record):
        raise OSError("disco cheio")
    with pytest.raises(ExecutorError, match="executor_state_unavailable"):
        SimulatedExecutor(executor.script, executor.store, clock=executor.clock, persist=failing,
                          state=SimulatedExecutor.initial_state(executor.script, executor.store.journal.run_id))


@pytest.mark.parametrize("operation", ["observe", "cancel", "reconcile", "collect"])
@pytest.mark.parametrize("execution", ["finish", "fail_retryable"])
def test_persist_failure_rolls_back_operation_and_drops_durability(simulation, operation, execution):
    saved, fail = [], False
    def persist(record):
        if fail:
            raise OSError("disco cheio")
        saved.append(record)
    document = script_document()
    document["default"].update(execution=execution,
                                reconcile="found_finished" if execution == "finish" else "found_failed")
    executor, intent, bundle, workspace = simulation(document, persist=persist)
    executor.dispatch(intent, bundle, workspace)
    before = executor.export_state()
    fail = True
    actions = {
        "observe": lambda: executor.observe(intent, "2026-11-10T12:00:10Z"),
        "cancel": lambda: executor.cancel(intent, "timeout"),
        "reconcile": lambda: executor.reconcile(intent["dispatch_idempotency_key"], None),
        "collect": lambda: executor.collect(intent),
    }
    executor.clock.advance_to("2026-11-10T12:00:10Z")
    with pytest.raises(ExecutorError, match="executor_state_unavailable"):
        actions[operation]()
    assert executor.export_state() == before == saved[-1]
    assert not executor.durable
    assert executor.reconcile("never-dispatched", None).outcome == "unknown"
    restarted = SimulatedExecutor(executor.script, executor.store, clock=executor.clock,
                                  persist=[].append, state=saved[-1])
    assert restarted.export_state() == before
    assert executor.reconcile(intent["dispatch_idempotency_key"], None).outcome == (
        restarted.reconcile(intent["dispatch_idempotency_key"], None).outcome)


@pytest.mark.parametrize("operation", ["observe", "cancel", "reconcile", "collect"])
def test_persist_failure_preserves_previously_confirmed_terminal(simulation, operation):
    saved, fail = [], False
    def persist(record):
        if fail:
            raise OSError("disco cheio")
        saved.append(record)
    executor, intent, bundle, workspace = simulation(persist=persist)
    executor.dispatch(intent, bundle, workspace)
    executor.observe(intent, "2026-11-10T12:00:02Z")
    before = executor.export_state()
    fail = True
    with pytest.raises(ExecutorError, match="executor_state_unavailable"):
        if operation == "observe":
            executor.observe(intent, executor.clock.now())
        elif operation == "cancel":
            executor.cancel(intent, "timeout")
        elif operation == "reconcile":
            executor.reconcile(intent["dispatch_idempotency_key"], None)
        else:
            executor.collect(intent)
    assert executor.export_state() == before


def test_new_instance_rebuilds_state_from_persisted_record(simulation):
    saved = []
    executor, intent, bundle, workspace = simulation(persist=saved.append)
    key = intent["dispatch_idempotency_key"]
    ack = executor.dispatch(intent, bundle, workspace)
    record = json.loads(json.dumps(saved[-1]))  # Registro serializável que o 6E grava.
    assert record == executor.export_state()
    assert record["state_hash"] == canonical_digest({k: v for k, v in record.items() if k != "state_hash"})
    # Reinício: nova instância sem memória, só com o registro persistido.
    restarted = SimulatedExecutor(executor.script, executor.store, clock=executor.clock,
                                  persist=saved.append, state=record)
    assert restarted.dispatch_count == restarted.execution_count == 1
    assert restarted.dispatch(intent, bundle, workspace) == ack
    assert restarted.execution_count == 1
    found = restarted.reconcile(key, ack.external_call_id)
    assert (found.outcome, found.authoritative, found.termination_certainty) == ("found_finished", False, "confirmed")
    result = restarted.collect(intent)
    assert result.produced_files
    again = SimulatedExecutor(executor.script, executor.store, clock=executor.clock,
                              persist=saved.append, state=json.loads(json.dumps(saved[-1])))
    assert again.collect(intent) == result
    absent = again.reconcile("run-demo-001/dispatch-other", None)
    assert (absent.outcome, absent.authoritative) == ("not_found", True)
    # Instância nova sem estado durável nunca nega uma chave possivelmente despachada.
    volatile = SimulatedExecutor(executor.script, executor.store, clock=executor.clock)
    unknown = volatile.reconcile(key, None)
    assert (unknown.outcome, unknown.authoritative, unknown.termination_certainty) == ("unknown", False, "unknown")
    assert volatile.dispatch_count == 0


@pytest.mark.parametrize("mutation,code", [
    (lambda s: s["calls"].clear(), "executor_state_digest_mismatch"),
    (lambda s: resign(s.update(run_id="other-run") or s, "state_hash"), "run_mismatch"),
    (lambda s: resign(s["script_ref"].update(version="2") or s, "state_hash"), "script_mismatch"),
    (lambda s: resign(s.update(extra=True) or s, "state_hash"), "invalid_executor_state"),
    (lambda s: resign(next(iter(s["calls"].values())).update(executed=False) or s, "state_hash"), "invalid_executor_state"),
    (lambda s: resign(next(iter(s["calls"].values())).update(terminal="lost") or s, "state_hash"), "invalid_executor_state"),
    (lambda s: resign(next(iter(s["calls"].values()))["workspace"].update(workspace_id="x") or s, "state_hash"),
     "invalid_executor_state"),
    (lambda s: resign(s.update(calls={"other": {}}) or s, "state_hash"), "invalid_executor_state"),
])
def test_invalid_persisted_state_is_rejected(simulation, mutation, code):
    saved = []
    executor, intent, bundle, workspace = simulation(persist=saved.append)
    executor.dispatch(intent, bundle, workspace)
    state = copy.deepcopy(saved[-1])
    mutation(state)
    with pytest.raises(ExecutorError, match=code):
        SimulatedExecutor(executor.script, executor.store, clock=executor.clock, persist=saved.append, state=state)


@pytest.mark.parametrize("dispatch,mutation", [
    ("ack", lambda c: c.update(started_at="ontem")),
    ("ack", lambda c: c.update(started_at=None)),
    ("ack", lambda c: c["ack"].update(acknowledged=False)),
    ("ack_lost", lambda c: c["ack"].update(acknowledged=True)),
    ("ack", lambda c: c["ack"].update(external_call_id="other")),
    ("ack", lambda c: c["ack"].update(observed_at="2026-11-10T00:00:00Z")),
    ("ack", lambda c: c.update(terminal="failed", failure=None)),
    ("ack", lambda c: c.update(terminal="finished", failure={})),
    ("not_executed", lambda c: c.update(terminal="finished")),
    ("reject_confirmed", lambda c: c.update(terminal="stopped")),
    ("ack", lambda c: c.update(result={})),
])
def test_rehashed_state_with_inconsistent_call_is_rejected(simulation, dispatch, mutation):
    document = script_document()
    document["default"].update(dispatch=dispatch, reconcile="unknown")
    saved = []
    executor, intent, bundle, workspace = simulation(document, persist=saved.append)
    executor.dispatch(intent, bundle, workspace)
    state = copy.deepcopy(saved[-1])
    mutation(next(iter(state["calls"].values())))
    resign(state, "state_hash")
    with pytest.raises(ExecutorError, match="invalid_executor_state"):
        SimulatedExecutor(executor.script, executor.store, clock=executor.clock, persist=saved.append, state=state)


@pytest.mark.parametrize("corruption", ["missing", "disk"])
def test_restore_does_not_read_collected_artifacts_and_evaluator_reports_integrity(simulation, corruption, monkeypatch):
    from orq_contracts.evaluator import DeterministicEvaluator
    from test_evaluator import ARTIFACT_ID, criterion, descriptor, validate_result

    saved = []
    executor, intent, bundle, workspace = simulation(persist=saved.append)
    _, _, result = finish(executor, intent, bundle, workspace)
    produced = result.produced_files[0]
    path = executor.store.directory / produced.storage_locator.split("/")[-1]
    if corruption == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrompido")
    record = json.loads(json.dumps(saved[-1]))
    read = executor.store.read
    def forbidden_read(locator):
        pytest.fail("a restauração não pode reler artefatos")
    with monkeypatch.context() as patch:
        patch.setattr(executor.store, "read", forbidden_read)
        restarted = SimulatedExecutor(executor.script, executor.store, clock=executor.clock,
                                      persist=saved.append, state=record)
        assert restarted.reconcile(intent["dispatch_idempotency_key"], None).outcome == "found_finished"
        other = copy.deepcopy(intent)
        other.update(dispatch_idempotency_key="other-dispatch", dispatch_intent_id="other-dispatch", task_id="T2")
        resign(other)
        assert restarted.dispatch(other, bundle, workspace).acknowledged
    assert executor.store.read == read
    with pytest.raises(ExecutorError, match="executor_state_artifact_unavailable"):
        restarted.collect(intent)
    evaluator = DeterministicEvaluator(restarted.store, restarted.script, clock=restarted.clock)
    report = evaluator.evaluate(
        run_id=intent["run_id"], evaluation_id="evaluation-integrity", evaluation_number=1,
        scope={"kind": "task_attempt", "task_id": intent["task_id"], "attempt_id": intent["attempt_id"]},
        subject_bindings=[{"output_id": produced.output_id, "artifact": {
            "artifact_id": ARTIFACT_ID, "content_digest": produced.declared_digest}}],
        criteria=[criterion("simulated_verdict")], attempt_number=1,
        artifacts={ARTIFACT_ID: descriptor(produced.content, produced.storage_locator, produced.content_contract_ref)},
    )
    validate_result(report)
    assert report.report["verdict"] == "fail"
    assert [error["code"] for error in report.errors] == ["integrity_mismatch"]


def test_state_and_persist_go_together_and_failure_drops_durability(simulation):
    executor, intent, bundle, workspace = simulation()
    with pytest.raises(ExecutorError, match="invalid_executor_state"):
        SimulatedExecutor(executor.script, executor.store, clock=executor.clock, persist=[].append)
    with pytest.raises(ExecutorError, match="invalid_executor_state"):
        SimulatedExecutor(executor.script, executor.store, clock=executor.clock,
                          state=SimulatedExecutor.initial_state(executor.script, "run-demo-001"))

    fail = False
    def persist(record):
        if fail:
            raise OSError("disco cheio")
    executor, intent, bundle, workspace = simulation(persist=persist)
    fail = True
    assert executor.durable
    with pytest.raises(ExecutorError, match="executor_state_unavailable"):
        executor.dispatch(intent, bundle, workspace)
    assert executor.dispatch_count == 0 and not executor.durable
    assert executor.reconcile(intent["dispatch_idempotency_key"], None).outcome == "unknown"


@pytest.mark.parametrize("terminal", ["finished", "failed", "stopped"])
def test_restores_confirmed_results_without_changing_state(simulation, terminal):
    document = script_document()
    document["default"].update(execution={"finished": "finish", "failed": "fail_retryable", "stopped": "hang"}[terminal],
                                reconcile="found_" + terminal)
    saved = []
    executor, intent, bundle, workspace = simulation(document, persist=saved.append)
    executor.dispatch(intent, bundle, workspace)
    executor.reconcile(intent["dispatch_idempotency_key"], None)
    result = executor.collect(intent)
    record = json.loads(json.dumps(saved[-1]))
    restarted = SimulatedExecutor(executor.script, executor.store, clock=executor.clock,
                                  persist=saved.append, state=record)
    before = restarted.clock.now()
    assert restarted.export_state() == record
    assert restarted.collect(intent) == result
    assert restarted.collect(intent) == result
    assert restarted.clock.now() == before
    assert restarted.export_state() == record


@pytest.mark.parametrize("mutation", [
    lambda c: c["failure"].update(occurred_at="ontem"),
    lambda c: c["failure"].update(scope_ref="other-attempt"),
    lambda c: c["result"].update(errors=[]),
    lambda c: c["result"].update(produced_files=[{}]),
    lambda c: c["result"]["usage"][0].update(scope_ref="other-attempt"),
])
def test_inconsistent_failure_or_result_is_rejected_even_with_rehashed_state(simulation, mutation):
    document = script_document()
    document["default"].update(execution="fail_retryable", reconcile="found_failed")
    saved = []
    executor, intent, bundle, workspace = simulation(document, persist=saved.append)
    finish(executor, intent, bundle, workspace)
    record = copy.deepcopy(saved[-1])
    mutation(next(iter(record["calls"].values())))
    resign(record, "state_hash")
    with pytest.raises(ExecutorError, match="invalid_executor_state"):
        SimulatedExecutor(executor.script, executor.store, clock=executor.clock, persist=saved.append, state=record)


@pytest.mark.parametrize("mode", ["from_output_spec", "omit", "wrong_contract", "declared_digest_mismatch"])
def test_output_values(simulation, mode):
    document = script_document()
    document["default"]["outputs"] = mode if mode == "from_output_spec" else {"report": mode}
    executor, intent, bundle, workspace = simulation(document)
    workspace = replace(workspace, expected_output=(*workspace.expected_output, output_spec("second")))
    _, _, result = finish(executor, intent, bundle, workspace)
    assert [f.output_id for f in result.produced_files] == (["second"] if mode == "omit" else ["report", "second"])
    for produced in result.produced_files:
        assert produced.content == canonicalize(json.loads(produced.content))
        assert executor.store.read(produced.storage_locator) == produced.content
        assert produced.storage_locator.endswith(bytes_digest(produced.content)["value"])
        assert (produced.declared_digest != bytes_digest(produced.content)) == (
            mode == "declared_digest_mismatch" and produced.output_id == "report")
        assert (produced.content_contract_ref != workspace.expected_output[0]["content_contract_ref"]) == (
            mode == "wrong_contract" and produced.output_id == "report")


def test_output_envelope_satisfies_declared_contract(simulation):
    executor, intent, bundle, workspace = simulation()
    _, _, result = finish(executor, intent, bundle, workspace)
    reference, schema = simulated_output_contract()
    assert schema == load_schemas()["simulated_output"]
    assert reference == {"id": "simulated-output", "version": "0.1", "content_digest": canonical_digest(schema)}
    produced = result.produced_files[0]
    assert produced.content_contract_ref == workspace.expected_output[0]["content_contract_ref"] == reference
    envelope = json.loads(produced.content)
    assert validator_for("simulated_output").is_valid(envelope)
    assert envelope["input_bindings"] == intent["input_bindings"]
    assert not validator_for("simulated_output").is_valid({**envelope, "extra": True})


def test_other_content_contracts_are_rejected_before_dispatch(simulation):
    executor, intent, bundle, workspace = simulation()
    spec = output_spec()
    spec["content_contract_ref"] = load_fixture("valid/task-minimal.json")["expected_output"][0]["content_contract_ref"]
    with pytest.raises(ExecutorError, match="unsupported_content_contract"):
        executor.dispatch(intent, bundle, replace(workspace, expected_output=(spec,)))
    assert executor.dispatch_count == 0


@pytest.mark.parametrize("status", ["reported", "estimated", "unavailable"])
@pytest.mark.parametrize("resolved_model_id", [None, "sim-resolved-2"])
def test_usage_and_model_values(simulation, status, resolved_model_id):
    document = script_document()
    document["default"].update(resolved_model_id=resolved_model_id,
        usage={"cost": {"status": status, "amount": None if status == "unavailable" else "0.25", "currency": "BRL"}})
    executor, intent, bundle, workspace = simulation(document)
    _, _, result = finish(executor, intent, bundle, workspace)
    cost = result.usage[0]["cost_lines"][0]
    assert cost["status"] == status
    assert cost["amount"] == (None if status == "unavailable" else "0.25")
    assert cost["currency"] == "BRL"
    assert result.usage[0]["coverage"][status] == ["cost"]
    assert result.resolved_model_id == resolved_model_id
    assert_record(result.usage[0], "usage_record")


def test_hooks_fire_once_with_explicit_scope():
    script = signed_script(load_fixture("valid/simulation-script-recovery.json"))
    controller = HookController(script)
    run = {"kind": "run"}
    attempt = {"kind": "task_attempt", "task_id": "T1", "attempt_number": 1}
    assert controller.due("budget_reserved", attempt) is None  # Escopo diferente não dispara.
    assert controller.due("budget_reserved", run)["action"] == "crash_after"
    assert controller.due("budget_reserved", run) is None  # Disparo único.
    # Retomada: o harness repassa os disparos persistidos; S35 não entra em laço.
    resumed = HookController(script, fired=json.loads(json.dumps(controller.fired)))
    assert resumed.due("budget_reserved", run) is None
    assert resumed.due("dispatch_requested", {**attempt, "attempt_number": 2}) is None
    assert resumed.due("dispatch_requested", attempt)["action"] == "cancel_after"
    assert resumed.due("dispatch_requested", attempt) is None
    assert resumed.due("evaluation_requested", attempt) is None
    assert resumed.due("evaluation_requested", {"kind": "workflow"})["hook_id"] == "crash-after-global-evaluation-request"
    assert resumed.fired == (
        "crash-after-first-reservation", "cancel-after-dispatch-T1-1", "crash-after-global-evaluation-request")
    with pytest.raises(ScriptError, match="unknown_hook_id"):
        HookController(script, fired=["missing"])
    with pytest.raises(ScriptError, match="unknown_hook_id"):
        HookController(script, fired=["crash-after-first-reservation"] * 2)
    with pytest.raises(ScriptError, match="invalid_hook_scope"):
        controller.due("budget_reserved", {"kind": "task", "task_id": "T1"})


@pytest.mark.parametrize("event_type", ["budget_reservd", "", None, 1, [], {}])
def test_hook_event_is_validated_before_dispatch(event_type):
    controller = HookController(signed_script(load_fixture("valid/simulation-script-recovery.json")))
    with pytest.raises(ScriptError, match="invalid_hook_event"):
        controller.due(event_type, {"kind": "run"})
    assert controller.fired == ()


@pytest.mark.parametrize("fired", ["", "crash-after-first-reservation", b"hook", None, 1,
                                   {"hook": True}, [[]], [None], iter([])])
def test_invalid_fired_input_is_typed(fired):
    script = signed_script(load_fixture("valid/simulation-script-recovery.json"))
    with pytest.raises(ScriptError, match="unknown_hook_id"):
        HookController(script, fired=fired)


@pytest.mark.parametrize("container", [list, tuple, set, frozenset])
def test_fired_accepts_sequences_and_sets(container):
    script = signed_script(load_fixture("valid/simulation-script-recovery.json"))
    hook_id = "crash-after-first-reservation"
    controller = HookController(script, fired=container([hook_id]))
    assert controller.fired == (hook_id,)
    assert controller.due("budget_reserved", {"kind": "run"}) is None


def test_executor_never_consults_hooks(simulation):
    document = script_document()
    document["hooks"] = [{"hook_id": "crash-dispatch", "action": "crash_after", "event_type": "dispatch_requested",
                          "scope": {"kind": "task_attempt", "task_id": "T1", "attempt_number": 1}}]
    executor, intent, bundle, workspace = simulation(document)
    assert finish(executor, intent, bundle, workspace)[1].status == "finished"
    assert not executor.store.journal.events


@pytest.mark.parametrize("hooks", [
    [{"hook_id": "a", "action": "crash_after", "event_type": "budget_reserved", "scope": {"kind": "run"}},
     {"hook_id": "a", "action": "cancel_after", "event_type": "run_closed", "scope": {"kind": "run"}}],
    [{"hook_id": "a", "action": "crash_after", "event_type": "budget_reserved", "scope": {"kind": "run"}},
     {"hook_id": "b", "action": "cancel_after", "event_type": "budget_reserved", "scope": {"kind": "run"}}],
    [{"hook_id": "a", "action": "crash_after", "event_type": "budget_reserved", "scope": {"kind": "task_attempt"}}],
    [{"hook_id": "a", "action": "explode", "event_type": "budget_reserved", "scope": {"kind": "run"}}],
    [{"hook_id": "a", "action": "crash_after", "event_type": "invalid", "scope": {"kind": "run"}}],
])
def test_invalid_hooks_are_rejected_on_load(hooks):
    document = script_document()
    document["hooks"] = hooks
    with pytest.raises(ScriptError, match="invalid_script"):
        signed_script(document)


def test_default_overrides_and_input_isolation():
    document = script_document()
    document["attempts"]["T1"] = {"1": {"duration_ms": 0, "verifier": {"extra": "fail"}}}
    script = signed_script(document)
    document["default"]["execution"] = "hang"
    attempt = script.for_attempt("T1", 1)
    assert attempt["duration_ms"] == 0 and attempt["execution"] == "finish"
    assert attempt["verifier"] == {"behavior-ok": "pass", "extra": "fail"}
    attempt["verifier"]["behavior-ok"] = "fail"
    assert script.verdict("behavior-ok", 1, task_id="T1", attempt_number=1) == "pass"
    assert script.for_attempt("T1", 2)["duration_ms"] == 1000
    assert script.for_attempt("T3", 2)["usage"]["cost"]["amount"] is None
    reference = script.reference
    reference["id"] = "changed"
    assert script.reference["id"] == "sim-script-6d"


@pytest.mark.parametrize("field,value", [
    ("dispatch", "invalid"), ("execution", "invalid"), ("duration_ms", -1), ("duration_ms", True),
    ("on_cancel", "unknown"), ("reconcile", "invalid"), ("resolved_model_id", 12),
    ("outputs", "invalid"), ("outputs", {"report": "invalid"}), ("outputs", {"bad/id": "omit"}),
    ("usage", {"cost": {"status": "unavailable", "amount": "0", "currency": "USD"}}),
    ("usage", {"cost": {"status": "reported", "amount": None, "currency": "USD"}}),
    ("usage", {"cost": {"status": "estimated", "amount": "-1", "currency": "USD"}}),
    ("usage", {"cost": {"status": "reported", "amount": "1e1", "currency": "USD"}}),
    ("usage", {"cost": {"status": "reported", "amount": "1", "currency": "usd"}}),
    ("usage", {"cost": {"status": "other", "amount": "1", "currency": "USD"}}),
    ("verifier", {"key": []}), ("verifier", {"key": "invalid"}),
    ("verifier", {"key": ["pass", "invalid"]}),
    # Hooks por comportamento foram substituídos por hooks com escopo explícito.
    ("crash_after", "dispatch_requested"), ("cancel_after", "dispatch_requested"), ("extra", 1),
])
def test_invalid_behavior_is_typed(field, value):
    document = script_document()
    document["default"][field] = value
    with pytest.raises(ScriptError, match="invalid_script"):
        signed_script(document)


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(schema_version="1"), lambda d: d.update(extra=True),
    lambda d: d["default"].pop("execution"), lambda d: d.update(reconcile_not_found_authoritative="true"),
    lambda d: d["clock"].update(step_ms=0), lambda d: d["clock"].update(start="2026-02-30T00:00:00Z"),
    lambda d: d["clock"].update(extra=1), lambda d: d["attempts"].update({"T1": {"0": {}}}),
    lambda d: d["attempts"].update({"T1": {"01": {}}}), lambda d: d.update(workflow_verifier={"key": ["invalid"]}),
    lambda d: d.pop("hooks"), lambda d: d.update(crash_after="dispatch_requested"),
])
def test_invalid_script_structure(mutation):
    document = script_document()
    mutation(document)
    with pytest.raises(ScriptError, match="invalid_script"):
        signed_script(document)


@pytest.mark.parametrize("override", [
    {"dispatch": "not_executed", "reconcile": "found_running"},
    {"dispatch": "reject_confirmed", "reconcile": "found_finished"},
    {"dispatch": "ack", "reconcile": "not_found"},
    {"dispatch": "ack_lost", "reconcile": "not_found"},
    {"execution": "fail_retryable", "reconcile": "found_finished"},
    {"execution": "finish", "reconcile": "found_failed"},
])
@pytest.mark.parametrize("location", ["default", "attempt"])
def test_contradictory_script_is_rejected_on_load(override, location):
    document = script_document()
    if location == "default":
        document["default"].update(override)
    else:
        document["attempts"]["T1"] = {"2": override}
    with pytest.raises(ScriptError, match="invalid_script"):
        signed_script(document)


@pytest.mark.parametrize("name", ["simulation-script", "simulation-script-recovery"])
def test_representative_scripts_validate_against_workflow(name):
    script = signed_script(load_fixture(f"valid/{name}.json"))
    assert script.validate_against_workflow(simulated_workflow()) is None


@pytest.mark.parametrize("mutation", [
    lambda w: w["tasks"].append(copy.deepcopy(w["tasks"][0])),
    lambda w: w["tasks"][0].update(dependencies=["T1"]),
    lambda w: w["outputs"][0].update(source_task_id="missing"),
])
def test_preflight_rejects_semantically_invalid_workflow(mutation):
    workflow = simulated_workflow()
    mutation(workflow)
    assert validator_for("workflow").is_valid(workflow)
    assert validate_workflow(workflow)
    with pytest.raises(ScriptError, match="invalid_workflow"):
        signed_script().validate_against_workflow(workflow)


@pytest.mark.parametrize("workflow_limit,task_limit", [(1, 3), (3, 1), (2, None), (None, 2)])
@pytest.mark.parametrize("target", ["attempt", "hook"])
def test_preflight_uses_minimum_attempt_limit(workflow_limit, task_limit, target):
    workflow = simulated_workflow()
    workflow["constraints"].pop("max_attempts", None)
    if workflow_limit is not None:
        workflow["constraints"]["max_attempts"] = workflow_limit
    for task in workflow["tasks"]:
        task["constraints"].pop("max_attempts", None)
        if task_limit is not None:
            task["constraints"]["max_attempts"] = task_limit
    effective = min(v for v in (workflow_limit, task_limit) if v is not None)
    document = script_document()
    document["attempts"] = {}
    if target == "attempt":
        document["attempts"] = {"T1": {str(effective): {}}}
    else:
        document["hooks"] = [{"hook_id": "boundary", "action": "crash_after", "event_type": "dispatch_requested",
                               "scope": {"kind": "task_attempt", "task_id": "T1", "attempt_number": effective}}]
    assert signed_script(document).validate_against_workflow(workflow) is None
    if target == "attempt":
        document["attempts"]["T1"][str(effective + 1)] = {}
    else:
        document["hooks"][0]["scope"]["attempt_number"] += 1
    with pytest.raises(ScriptError, match="attempt_out_of_range" if target == "attempt" else "unknown_hook_scope"):
        signed_script(document).validate_against_workflow(workflow)


@pytest.mark.parametrize("workflow_limit,task_limit", [(1, 3), (3, 1), (2, None), (None, 2)])
def test_preflight_verifier_covers_effective_evaluation_limit(workflow_limit, task_limit):
    workflow = simulated_workflow()
    workflow["constraints"].pop("max_evaluations_per_attempt", None)
    if workflow_limit is not None:
        workflow["constraints"]["max_evaluations_per_attempt"] = workflow_limit
    for task in workflow["tasks"]:
        if task_limit is not None:
            task["constraints"]["max_evaluations_per_attempt"] = task_limit
    effective = min(v for v in (workflow_limit, task_limit) if v is not None)
    document = script_document()
    document["attempts"] = {}
    document["default"]["verifier"]["behavior-ok"] = ["pass"] * effective
    document["workflow_verifier"]["workflow-behavior-ok"] = ["pass"]
    assert signed_script(document).validate_against_workflow(workflow) is None
    if effective > 1:
        document["default"]["verifier"]["behavior-ok"].pop()
        with pytest.raises(ScriptError, match="evaluation_script_incomplete"):
            signed_script(document).validate_against_workflow(workflow)


@pytest.mark.parametrize("limit", [None, 2, 5])
def test_workflow_verifier_needs_only_one_verdict(limit):
    workflow = simulated_workflow()
    workflow["constraints"].pop("max_evaluations_per_attempt", None)
    if limit is not None:
        workflow["constraints"]["max_evaluations_per_attempt"] = limit
    document = script_document()
    document["attempts"] = {}
    document["workflow_verifier"]["workflow-behavior-ok"] = ["pass"]
    assert signed_script(document).validate_against_workflow(workflow) is None
    document["workflow_verifier"]["workflow-behavior-ok"] = []
    with pytest.raises(ScriptError, match="invalid_script"):
        signed_script(document)


def _drop_max_attempts(document, workflow):
    for task in workflow["tasks"]:
        task["constraints"].pop("max_attempts", None)
    workflow["constraints"].pop("max_attempts")


@pytest.mark.parametrize("mutate,code", [
    (lambda d, w: d["attempts"].update({"T9": {"1": {}}}), "unknown_task_id"),
    (lambda d, w: d["attempts"]["T3"].update({"3": {}}), "attempt_out_of_range"),
    (lambda d, w: d["default"]["verifier"].pop("behavior-ok"), "script_key_missing"),
    (lambda d, w: d["attempts"]["T3"]["1"]["verifier"].update({"behavior-ok": ["inconclusive"]}),
     "evaluation_script_incomplete"),
    (lambda d, w: d["workflow_verifier"].clear(), "script_key_missing"),
    (lambda d, w: d["attempts"].update({"T1": {"2": {"outputs": {"missing": "omit"}}}}), "unknown_output_id"),
    (lambda d, w: d["hooks"].append({"hook_id": "late", "action": "crash_after", "event_type": "dispatch_requested",
                                     "scope": {"kind": "task_attempt", "task_id": "T1", "attempt_number": 3}}),
     "unknown_hook_scope"),
    (lambda d, w: w["tasks"][0]["expected_output"][0].update(
        content_contract_ref=load_fixture("valid/task-minimal.json")["expected_output"][0]["content_contract_ref"]),
     "unsupported_content_contract"),
    (lambda d, w: w["tasks"][1]["evaluation_criteria"][0].update(pass_rule={"kind": "valid"}), "invalid_pass_rule"),
    (_drop_max_attempts, "script_limits_unknown"),
    (lambda d, w: w.pop("tasks"), "invalid_workflow"),
])
def test_script_is_validated_against_workflow_before_admission(mutate, code):
    document = script_document()
    workflow = simulated_workflow()
    mutate(document, workflow)
    script = signed_script(document)
    with pytest.raises(ScriptError, match=code):
        script.validate_against_workflow(workflow)


def test_script_digest_and_strict_json():
    document = script_document()
    digest = canonical_digest(document)
    script = SimulationScript.from_bytes(canonicalize(document), expected_digest=digest)
    assert script.reference["content_digest"] == digest
    document["version"] = "2"
    with pytest.raises(ScriptError, match="script_digest_mismatch"):
        SimulationScript(document, expected_digest=digest)
    for content in (b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff', b'[]', b'null'):
        with pytest.raises(ScriptError, match="invalid_script"):
            SimulationScript.from_bytes(content, expected_digest=digest)


def test_bundle_and_dispatch_integrity_before_effects(simulation):
    executor, intent, bundle, workspace = simulation()
    with pytest.raises(ExecutorError, match="bundle_digest_mismatch"):
        executor.dispatch(intent, bundle + b"tampered", workspace)
    changed = copy.deepcopy(intent)
    changed["attempt_number"] = 4
    with pytest.raises(ExecutorError, match="invalid_dispatch_intent"):
        executor.dispatch(changed, bundle, workspace)
    with pytest.raises(ExecutorError, match="workspace_mismatch"):
        executor.dispatch(intent, bundle, replace(workspace, base_ref={}))
    assert executor.dispatch_count == 0
    assert not list(executor.store.directory.iterdir())


def test_dispatch_and_collection_are_idempotent(simulation):
    executor, intent, bundle, workspace = simulation()
    ack = executor.dispatch(intent, bundle, workspace)
    now = executor.clock.now()
    assert executor.dispatch(copy.deepcopy(intent), bundle, copy.deepcopy(workspace)) == ack
    assert executor.clock.now() == now
    assert executor.dispatch_count == executor.execution_count == 1
    with pytest.raises(ExecutorError, match="idempotency_conflict"):
        executor.dispatch(intent, bundle, replace(workspace, workspace_id="different"))
    executor.observe(intent, "2026-11-10T12:00:02Z")
    result = executor.collect(intent)
    now = executor.clock.now()
    assert executor.collect(intent) == result
    assert executor.clock.now() == now
    result.usage[0]["cost_lines"][0]["amount"] = "999"
    assert executor.collect(intent).usage[0]["cost_lines"][0]["amount"] == "0.10"


def test_unknown_output_id_invalidates_script_before_dispatch(simulation):
    document = script_document()
    document["default"]["outputs"] = {"missing": "omit"}
    executor, intent, bundle, workspace = simulation(document)
    with pytest.raises(ScriptError, match="unknown_output_id"):
        executor.dispatch(intent, bundle, workspace)
    assert executor.dispatch_count == 0


def test_injected_clock_and_zero_duration(simulation):
    document = script_document()
    document["default"]["duration_ms"] = 0
    clock = DeterministicClock("2026-11-10T13:00:00Z", 7)
    executor, intent, bundle, workspace = simulation(document, clock=clock)
    assert executor.clock is clock is executor.store.journal.clock
    assert executor.dispatch(intent, bundle, workspace).observed_at == "2026-11-10T13:00:00.000000Z"
    assert clock.now() == "2026-11-10T13:00:00.007000Z"
    assert executor.observe(intent, clock.now()).status == "finished"


@pytest.mark.parametrize("execution,status", [("finish", "finished"), ("hang", "running")])
def test_observe_with_past_deadline_returns_current_instant(simulation, execution, status):
    document = script_document()
    document["default"].update(execution=execution, reconcile="found_running")
    executor, intent, bundle, workspace = simulation(document)
    executor.dispatch(intent, bundle, workspace)
    executor.clock.advance_to("2026-11-10T12:00:03Z")  # Appends do journal passaram do prazo.
    before = executor.clock.now()
    observation = executor.observe(intent, "2026-11-10T12:00:00.500Z")
    assert (observation.status, observation.observed_at) == (status, before)
    assert executor.clock.now() == before


def test_clock_must_be_injected_and_shared_with_journal(simulation):
    executor, _, _, _ = simulation()
    with pytest.raises(TypeError):
        SimulatedExecutor(executor.script, executor.store)
    with pytest.raises(ExecutorError, match="invalid_clock"):
        SimulatedExecutor(executor.script, executor.store, clock=lambda: "2026-11-10T12:00:00Z")
    with pytest.raises(ExecutorError, match="clock_mismatch"):
        SimulatedExecutor(executor.script, executor.store, clock=executor.script.make_clock())


def test_same_inputs_make_identical_records_and_artifact_digests(simulation):
    runs = []
    for _ in range(2):
        executor, intent, bundle, workspace = simulation()
        ack, observation, result = finish(executor, intent, bundle, workspace)
        runs.append((ack, observation, result))
    assert runs[0] == runs[1]
    first = runs[0][2]
    second = runs[1][2]
    assert canonical_digest(first.usage) == canonical_digest(second.usage)
    assert first.produced_files[0].declared_digest == second.produced_files[0].declared_digest
    assert bytes_digest(first.produced_files[0].content) == bytes_digest(second.produced_files[0].content)


@pytest.mark.parametrize("terminal", ["finish", "fail_retryable", "fail_non_retryable"])
def test_reconciliation_preserves_confirmed_termination(simulation, terminal):
    document = script_document()
    document["default"].update(execution=terminal, reconcile="found_running")
    executor, intent, bundle, workspace = simulation(document)
    executor.dispatch(intent, bundle, workspace)
    observed = executor.observe(intent, "2026-11-10T12:00:02Z")
    reconciled = executor.reconcile(intent["dispatch_idempotency_key"], None)
    assert reconciled.outcome == ("found_finished" if terminal == "finish" else "found_failed")
    assert reconciled.termination_certainty == "confirmed"
    assert reconciled.errors == observed.errors


def test_no_network_and_storage_is_only_writer(simulation, monkeypatch):
    block_network(monkeypatch)
    executor, intent, bundle, workspace = simulation()
    written = []
    put = executor.store.put
    def capture(content):
        written.append(content)
        return put(content)
    monkeypatch.setattr(executor.store, "put", capture)
    _, _, result = finish(executor, intent, bundle, workspace)
    executor.cancel(intent, "timeout")
    executor.reconcile(intent["dispatch_idempotency_key"], None)
    executor.describe()
    assert written == [produced.content for produced in result.produced_files]
    assert not executor.store.journal.events


@pytest.mark.parametrize("field", ["workspace_id", "requested_model_id"])
def test_dispatch_conflicting_workspace_does_not_repeat_effect(simulation, field):
    executor, intent, bundle, workspace = simulation()
    executor.dispatch(intent, bundle, workspace)
    with pytest.raises(ExecutorError, match="idempotency_conflict"):
        executor.dispatch(intent, bundle, replace(workspace, **{field: "changed"}))
    assert executor.execution_count == 1


def test_invalid_runtime_output_metadata_is_rejected_before_dispatch(simulation):
    executor, intent, bundle, workspace = simulation()
    with pytest.raises(ExecutorError, match="invalid_output_spec"):
        executor.dispatch(intent, bundle, replace(workspace, expected_output=()))
    with pytest.raises(ExecutorError, match="invalid_workspace"):
        executor.dispatch(intent, bundle, replace(workspace, requested_model_id=3))
    assert executor.dispatch_count == 0


def test_script_lookup_requires_positive_numbers():
    script = signed_script()
    with pytest.raises(ScriptError, match="invalid_script_lookup"):
        script.for_attempt("T1", 0)
    with pytest.raises(ScriptError, match="invalid_script_lookup"):
        script.verdict("behavior-ok", 0, task_id="T1", attempt_number=1)
