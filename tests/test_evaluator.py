from __future__ import annotations

import copy

import pytest

from conftest import load_fixture
from orq_contracts.artifact_store import ArtifactStoreError
from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.evaluator import (
    ArtifactEvidence, ContentContractRegistry, DeterministicEvaluator, EvaluationError,
)
from orq_contracts.executor import bytes_digest
from orq_contracts.simulated_executor import ScriptError, simulated_output_contract
from test_simulated_executor import assert_record, block_network, build_simulation, finish, script_document


ARTIFACT_ID = "artifact-T1-1-report"


def criterion(method, *, required=True, output_ids=None, script_key="behavior-ok", id="criterion"):
    return {
        "criterion_id": id, "requirement_ids": ["REQ1"], "method": method,
        "pass_rule": {"script_key": script_key} if method == "simulated_verdict" else {
            "output_ids": output_ids if output_ids is not None else ["report"]},
        "required": required, "verifier_ref": None,
    }


def descriptor(content, locator, contract, *, artifact_id=ARTIFACT_ID, digest=None):
    """Payload de artifact_ingested, como o 6E grava antes de vincular o output."""
    return ArtifactEvidence({
        "schema_version": "0.1", "artifact_id": artifact_id, "artifact_type": "report",
        "media_type": "application/json", "content_contract_ref": copy.deepcopy(contract),
        "content_digest": digest if digest is not None else bytes_digest(content),
        "size_bytes": len(content), "locator": locator, "created_at": "2026-11-10T12:00:01Z",
        "producer": {"kind": "attempt_output", "run_id": "run-demo-001", "task_id": "T1", "attempt_id": "attempt-T1-1"},
        "derived_from": [], "data_classification": "internal",
    })


def store_content(evaluator, request, content, contract):
    """Grava bytes, liga o binding a eles e registra o descritor ingerido correspondente."""
    locator = evaluator.store.put(content)
    request["subject_bindings"][0]["artifact"]["content_digest"] = bytes_digest(content)
    request["artifacts"][ARTIFACT_ID] = descriptor(content, locator, contract)


def as_workflow(request, criterion_value, output_ids):
    request.update(scope={"kind": "workflow"}, evaluation_id="evaluation-workflow-1", attempt_number=None)
    request["subject_bindings"][0]["output_id"] = "final-report"
    criterion_value["workflow_requirement_ids"] = criterion_value.pop("requirement_ids")
    criterion_value["output_ids"] = list(output_ids)
    request["criteria"] = [criterion_value]


@pytest.fixture
def evaluation(tmp_path):
    journals = []
    def create(document=None, *, register=True):
        journal, executor, intent, bundle, workspace = build_simulation(tmp_path / str(len(journals)), document)
        journals.append(journal)
        _, _, result = finish(executor, intent, bundle, workspace)
        produced = result.produced_files[0]
        binding = {"output_id": "report", "artifact": {
            "artifact_id": ARTIFACT_ID, "content_digest": bytes_digest(produced.content)}}
        evaluator = DeterministicEvaluator(executor.store, executor.script, clock=executor.clock)
        if register:
            evaluator.contracts.register(*simulated_output_contract())
        artifacts = {ARTIFACT_ID: descriptor(produced.content, produced.storage_locator, produced.content_contract_ref)}
        request = {"run_id": intent["run_id"], "evaluation_id": "evaluation-T1-1-1", "evaluation_number": 1,
                   "scope": {"kind": "task_attempt", "task_id": "T1", "attempt_id": "attempt-T1-1"},
                   "attempt_number": 1, "subject_bindings": [binding], "artifacts": artifacts,
                   "criteria": [criterion("output_binding_present")]}
        return evaluator, request, produced
    yield create
    for journal in journals:
        journal.close()


def validate_result(result):
    assert result.termination_certainty == "confirmed"
    assert_record(result.report, "evaluation_report")
    assert not result.report["usage_record_ids"]
    assert result.report["context_manifest_ref"] is None
    assert result.report["patch_application_refs"] == []
    assert result.report["criteria_snapshot"]["criteria_hash"] == canonical_digest(result.report["criteria_snapshot"]["criteria"])
    for error in result.errors:
        assert_record(error, "error_record")
        assert error["error_id"] in result.report["error_record_ids"]


@pytest.mark.parametrize("method", ["output_binding_present", "digest_matches", "content_contract_valid", "simulated_verdict"])
def test_each_supported_method(evaluation, method):
    evaluator, request, produced = evaluation()
    request["criteria"] = [criterion(method)]
    # content_contract_valid usa a referência real declarada no OutputSpec (envelope versionado).
    assert request["artifacts"][ARTIFACT_ID].content_contract_ref == simulated_output_contract()[0]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == "pass"
    assert result.report["verdict"] == "pass"
    assert result.report["subject_bindings"] == request["subject_bindings"]


def test_wrong_declared_contract_is_not_run(evaluation):
    document = script_document()
    document["default"]["outputs"] = {"report": "wrong_contract"}
    evaluator, request, produced = evaluation(document)
    assert produced.content_contract_ref != simulated_output_contract()[0]
    request["criteria"] = [criterion("content_contract_valid")]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == "not_run"
    assert result.report["verdict"] == "inconclusive"


@pytest.mark.parametrize("method", ["schema_validation", "unrecognized"])
def test_unknown_method_not_run_never_pass(evaluation, method):
    evaluator, request, _ = evaluation()
    request["criteria"] = [criterion(method)]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == "not_run"
    assert result.report["verdict"] == "inconclusive"
    assert result.report["limitations"]


@pytest.mark.parametrize("method", ["output_binding_present", "digest_matches", "content_contract_valid"])
def test_output_binding_missing_fails(evaluation, method):
    evaluator, request, _ = evaluation()
    request["criteria"] = [criterion(method, output_ids=["missing"])]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == "fail"
    assert result.report["verdict"] == "fail"


def corrupt(evaluator, request, produced, corruption):
    if corruption == "disk":
        (evaluator.store.directory / produced.storage_locator.split("/")[-1]).write_bytes(b'{"changed":true}')
    elif corruption == "missing":
        (evaluator.store.directory / produced.storage_locator.split("/")[-1]).unlink()
    elif corruption == "binding":
        request["subject_bindings"][0]["artifact"]["content_digest"]["value"] = "0" * 64
    elif corruption == "locator":
        other = b'{"different":true}'
        request["artifacts"][ARTIFACT_ID] = descriptor(
            produced.content, evaluator.store.put(other), produced.content_contract_ref)
    elif corruption in {"size_bytes", "run_id", "task_id", "attempt_id"}:
        raw = copy.deepcopy(request["artifacts"][ARTIFACT_ID].descriptor)
        if corruption == "size_bytes":
            raw["size_bytes"] += 1
        else:
            raw["producer"][corruption] = "other"
        request["artifacts"][ARTIFACT_ID] = ArtifactEvidence(raw)
    else:
        raise ValueError(corruption)


@pytest.mark.parametrize("method", ["output_binding_present", "digest_matches", "content_contract_valid", "simulated_verdict"])
@pytest.mark.parametrize("corruption", ["disk", "missing", "binding", "locator",
                                        "size_bytes", "run_id", "task_id", "attempt_id"])
def test_integrity_divergence_is_fail_and_integrity_error(evaluation, method, corruption):
    evaluator, request, produced = evaluation()
    request["criteria"] = [criterion(method)]
    corrupt(evaluator, request, produced, corruption)
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == "fail"
    assert result.report["verdict"] == "fail"
    assert len(result.errors) == 1
    error = result.errors[0]
    assert (error["category"], error["code"], error["termination_certainty"]) == ("integrity", "integrity_mismatch", "confirmed")
    assert error["scope_type"] == "evaluation"
    assert error["scope_ref"] == request["evaluation_id"]
    assert result.report["criterion_results"][0]["error_record_ids"] == [error["error_id"]]
    message = error["message"]
    if corruption == "size_bytes":
        assert "Tamanho" in message
    elif corruption in {"run_id", "task_id", "attempt_id"}:
        assert "Produtor" in message
    elif corruption in {"binding", "locator"}:
        assert "Descritor" in message
    elif corruption == "missing":
        assert "ausente" in message
    else:
        assert "Bytes" in message


def test_t3_presence_plus_scripted_pass_fails_on_corrupted_bytes(evaluation):
    evaluator, request, produced = evaluation()
    request["criteria"] = [criterion("output_binding_present", id="present"), criterion("simulated_verdict", id="verdict")]
    corrupt(evaluator, request, produced, "disk")
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert [r["status"] for r in result.report["criterion_results"]] == ["fail", "fail"]
    assert result.report["verdict"] == "fail"
    assert [e["code"] for e in result.errors] == ["integrity_mismatch"]


def test_integrity_failure_with_only_unknown_methods_never_passes(evaluation):
    evaluator, request, produced = evaluation()
    request["criteria"] = [criterion("unrecognized")]
    corrupt(evaluator, request, produced, "disk")
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == "not_run"
    assert result.report["verdict"] == "inconclusive"
    assert [e["code"] for e in result.errors] == ["integrity_mismatch"]


def test_content_contract_unknown_is_not_run(evaluation):
    evaluator, request, _ = evaluation(register=False)
    request["criteria"] = [criterion("content_contract_valid")]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == "not_run"
    assert result.report["verdict"] == "inconclusive"


@pytest.mark.parametrize("content", [b'{"ok":false}', b'{', b'\xff', b'{"ok":true,"ok":false}', b'{"ok":NaN}'])
def test_invalid_content_fails_registered_contract(evaluation, content):
    evaluator, request, _ = evaluation()
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"const": True}}, "additionalProperties": False}
    ref = {"id": "contract", "version": "1", "content_digest": canonical_digest(schema)}
    evaluator.contracts.register(ref, schema)
    store_content(evaluator, request, content, ref)
    request["criteria"] = [criterion("content_contract_valid")]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == "fail"
    assert result.report["verdict"] == "fail"
    assert not result.errors  # Falha de contrato não é divergência de digest.


@pytest.mark.parametrize("status,verdict", [("pass", "pass"), ("fail", "fail"), ("inconclusive", "inconclusive"), ("not_run", "inconclusive")])
def test_scripted_verdict_values(evaluation, status, verdict):
    document = script_document()
    document["default"]["verifier"]["behavior-ok"] = status
    evaluator, request, _ = evaluation(document)
    request["criteria"] = [criterion("simulated_verdict")]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == status
    assert result.report["verdict"] == verdict
    assert result.report["execution_status"] == "completed"


def test_s24_list_uses_evaluation_number_and_preserves_bindings(evaluation):
    document = script_document()
    document["attempts"]["T1"] = {"1": {"verifier": {"behavior-ok": ["inconclusive", "pass"]}}}
    evaluator, request, _ = evaluation(document)
    request["criteria"] = [criterion("simulated_verdict")]
    first = evaluator.evaluate(**request)
    request.update(evaluation_id="evaluation-T1-1-2", evaluation_number=2)
    second = evaluator.evaluate(**request)
    validate_result(first)
    validate_result(second)
    assert [first.report["verdict"], second.report["verdict"]] == ["inconclusive", "pass"]
    assert first.report["subject_bindings"] == second.report["subject_bindings"]
    assert first.report["evaluation_id"] != second.report["evaluation_id"]
    assert first.report["record_hash"] != second.report["record_hash"]
    request.update(evaluation_id="evaluation-T1-1-3", evaluation_number=3)
    now = evaluator.clock.now()
    with pytest.raises(ScriptError, match="evaluation_script_exhausted"):
        evaluator.evaluate(**request)
    assert evaluator.clock.now() == now


def test_script_missing_key_is_invalid_not_a_pass(evaluation):
    evaluator, request, _ = evaluation()
    request["criteria"] = [criterion("simulated_verdict", script_key="missing")]
    with pytest.raises(ScriptError, match="script_key_missing"):
        evaluator.evaluate(**request)


def test_s59_unknown_termination_has_no_final_report_or_usage(evaluation):
    document = script_document()
    document["default"]["verifier"]["behavior-ok"] = "unknown_termination"
    evaluator, request, _ = evaluation(document)
    request["criteria"] = [criterion("simulated_verdict")]
    result = evaluator.evaluate(**request)
    assert result.report is None
    assert result.termination_certainty == "unknown"
    assert not result.errors
    assert not evaluator.store.journal.events


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_workflow_scope_uses_workflow_verifier_not_task_verifier(evaluation, verdict):
    document = script_document()
    document["workflow_verifier"]["shared-key"] = verdict
    document["default"]["verifier"]["shared-key"] = "fail" if verdict == "pass" else "pass"
    evaluator, request, _ = evaluation(document)
    as_workflow(request, criterion("simulated_verdict", script_key="shared-key"), ["final-report"])
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["scope"] == {"kind": "workflow"}
    assert result.report["criterion_results"][0]["requirement_ids"] == ["REQ1"]
    assert result.report["criterion_results"][0]["status"] == verdict
    assert result.report["verdict"] == verdict


@pytest.mark.parametrize("method", ["output_binding_present", "digest_matches", "content_contract_valid", "simulated_verdict"])
@pytest.mark.parametrize("output_id,expected", [("final-report", "pass"), ("report", "fail")])
def test_workflow_scope_output_methods_use_final_output_ids(evaluation, method, output_id, expected):
    evaluator, request, _ = evaluation()
    as_workflow(request, criterion(method, output_ids=[output_id], script_key="workflow-behavior-ok"), [output_id])
    result = evaluator.evaluate(**request)
    validate_result(result)
    # Na avaliação global só o ID final está vinculado; o ID da tarefa não conta.
    assert result.report["criterion_results"][0]["status"] == expected
    assert result.report["verdict"] == expected
    if expected == "pass":
        artifact = request["subject_bindings"][0]["artifact"]
        assert artifact in result.report["criterion_results"][0]["evidence_refs"]
        assert artifact in result.report["evidence_refs"]


@pytest.mark.parametrize("field,expected", [("run_id", "fail"), ("task_id", "pass"), ("attempt_id", "pass")])
def test_workflow_artifact_producer_must_match_run_but_may_be_any_task(evaluation, field, expected):
    evaluator, request, produced = evaluation()
    as_workflow(request, criterion("simulated_verdict", script_key="workflow-behavior-ok"), ["final-report"])
    corrupt(evaluator, request, produced, field)
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["verdict"] == expected
    assert [error["code"] for error in result.errors] == (["integrity_mismatch"] if expected == "fail" else [])


@pytest.mark.parametrize("required_status,optional_status,expected", [
    ("pass", "fail", "pass"), ("fail", "pass", "fail"),
    ("inconclusive", "pass", "inconclusive"), ("not_run", "pass", "inconclusive"),
])
def test_required_and_optional_aggregation(evaluation, required_status, optional_status, expected):
    document = script_document()
    document["default"]["verifier"] = {"required": required_status, "optional": optional_status}
    evaluator, request, _ = evaluation(document)
    request["criteria"] = [criterion("simulated_verdict", id="required", script_key="required"),
                           criterion("simulated_verdict", id="optional", script_key="optional", required=False)]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["verdict"] == expected
    assert len(result.report["criterion_results"]) == 2


def test_required_failure_beats_unknown_method(evaluation):
    evaluator, request, _ = evaluation()
    request["criteria"] = [criterion("output_binding_present", output_ids=["missing"], id="failed"),
                           criterion("unknown", id="unknown")]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["verdict"] == "fail"


def test_missing_descriptor_is_integration_error_before_clock(evaluation):
    evaluator, request, _ = evaluation()
    request["artifacts"] = {}
    now = evaluator.clock.now()
    with pytest.raises(EvaluationError, match="artifact_descriptor_missing"):
        evaluator.evaluate(**request)
    assert evaluator.clock.now() == now


def test_descriptor_must_be_an_ingested_payload(evaluation):
    evaluator, request, produced = evaluation()
    with pytest.raises(EvaluationError, match="invalid_artifact_descriptor"):
        descriptor(produced.content, "../escape", produced.content_contract_ref)
    with pytest.raises(EvaluationError, match="invalid_artifact_descriptor"):
        ArtifactEvidence({"storage_locator": produced.storage_locator})
    # Descritor de outro artifact_id não serve de evidência para o binding.
    request["artifacts"][ARTIFACT_ID] = descriptor(
        produced.content, produced.storage_locator, produced.content_contract_ref, artifact_id="artifact-other")
    with pytest.raises(EvaluationError, match="artifact_descriptor_missing"):
        evaluator.evaluate(**request)


def test_local_schema_references_never_fetch_network(evaluation, monkeypatch):
    evaluator, request, produced = evaluation()
    block_network(monkeypatch)
    schema = {"$ref": "https://unregistered.invalid/contract.json"}
    ref = {"id": "unregistered", "version": "1", "content_digest": canonical_digest(schema)}
    evaluator.contracts.register(ref, schema)
    request["artifacts"][ARTIFACT_ID] = descriptor(produced.content, produced.storage_locator, ref)
    request["criteria"] = [criterion("content_contract_valid")]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["criterion_results"][0]["status"] == "not_run"


def test_registered_local_refs_and_contract_digest():
    registry = ContentContractRegistry()
    schema = {"$id": "https://local.test/integer", "type": "integer"}
    ref = {"id": "integer", "version": "1", "content_digest": canonical_digest(schema)}
    registry.register(ref, schema)
    wrapper = {"$ref": "https://local.test/integer"}
    wrapper_ref = {"id": "wrapper", "version": "1", "content_digest": canonical_digest(wrapper)}
    registry.register(wrapper_ref, wrapper)
    assert registry.validator(wrapper_ref).is_valid(5)
    assert not registry.validator(wrapper_ref).is_valid("5")
    wrong = copy.deepcopy(ref)
    wrong["content_digest"]["value"] = "0" * 64
    assert registry.validator(wrong) is None
    with pytest.raises(EvaluationError, match="content_contract_digest_mismatch"):
        registry.register(wrong, schema)
    with pytest.raises(EvaluationError, match="invalid_content_contract"):
        registry.register({}, schema)
    invalid = {"type": "invalid"}
    with pytest.raises(EvaluationError, match="invalid_content_contract"):
        registry.register({**ref, "content_digest": canonical_digest(invalid)}, invalid)


def test_format_is_annotation_only_for_determinism():
    registry = ContentContractRegistry()
    schema = {"type": "string", "format": "email"}
    ref = {"id": "email", "version": "1", "content_digest": canonical_digest(schema)}
    registry.register(ref, schema)
    # Com FormatChecker o resultado dependeria dos pacotes instalados; aqui format não é asserção.
    assert registry.validator(ref).is_valid("sem arroba")
    assert not registry.validator(ref).is_valid(5)


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(evaluation_number=0), lambda r: r.update(attempt_number=None),
    lambda r: r.update(subject_bindings=[]), lambda r: r.update(criteria=[]),
    lambda r: r["subject_bindings"].append(copy.deepcopy(r["subject_bindings"][0])),
    lambda r: r["criteria"].append(copy.deepcopy(r["criteria"][0])),
    lambda r: r["criteria"][0].update(pass_rule={"output_ids": []}),
])
def test_invalid_evaluation_request_is_typed(evaluation, mutation):
    evaluator, request, _ = evaluation()
    mutation(request)
    with pytest.raises(EvaluationError, match="invalid_evaluation_request"):
        evaluator.evaluate(**request)


def test_workflow_criterion_mismatch_and_evaluation_number_are_rejected(evaluation):
    evaluator, request, _ = evaluation()
    request.update(scope={"kind": "workflow"}, attempt_number=None)
    c = request["criteria"][0]
    c["workflow_requirement_ids"] = c.pop("requirement_ids")
    c["output_ids"] = ["other"]
    with pytest.raises(EvaluationError, match="semantic.criterion_output_ids_mismatch"):
        evaluator.evaluate(**request)
    c["output_ids"] = ["report"]
    request["evaluation_number"] = 2
    with pytest.raises(EvaluationError, match="invalid_evaluation_request"):
        evaluator.evaluate(**request)


def test_two_runs_produce_identical_evaluation_records_and_digests(evaluation):
    results = []
    for _ in range(2):
        evaluator, request, _ = evaluation()
        request["criteria"] = [criterion("output_binding_present", id="present"),
                               criterion("digest_matches", id="digest"),
                               criterion("content_contract_valid", id="contract"),
                               criterion("simulated_verdict", id="verdict")]
        result = evaluator.evaluate(**request)
        validate_result(result)
        assert result.report["verdict"] == "pass"
        results.append(result)
    assert results[0] == results[1]
    assert canonicalize(results[0].report) == canonicalize(results[1].report)


def test_two_runs_produce_identical_integrity_errors(evaluation):
    results = []
    for _ in range(2):
        evaluator, request, _ = evaluation()
        request["criteria"] = [criterion("digest_matches")]
        request["subject_bindings"][0]["artifact"]["content_digest"]["value"] = "0" * 64
        result = evaluator.evaluate(**request)
        validate_result(result)
        results.append(result)
    assert results[0] == results[1]
    assert canonical_digest(results[0].errors) == canonical_digest(results[1].errors)


def test_baseline_schema_validation_remains_not_run(evaluation):
    evaluator, request, _ = evaluation()
    request["criteria"] = load_fixture("valid/task-minimal.json")["evaluation_criteria"]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["verdict"] == "inconclusive"
    assert result.report["criterion_results"][0]["status"] == "not_run"


def test_numerical_overflow_in_content_is_not_accepted(evaluation):
    evaluator, request, _ = evaluation()
    schema = {"type": "object", "properties": {"n": {"type": "number"}}}
    ref = {"id": "number", "version": "1", "content_digest": canonical_digest(schema)}
    evaluator.contracts.register(ref, schema)
    store_content(evaluator, request, b'{"n":1e999}', ref)
    request["criteria"] = [criterion("content_contract_valid")]
    result = evaluator.evaluate(**request)
    validate_result(result)
    assert result.report["verdict"] == "fail"


def test_workflow_missing_key_is_invalid_script(evaluation):
    evaluator, request, _ = evaluation()
    request.update(scope={"kind": "workflow"}, attempt_number=None)
    c = criterion("simulated_verdict", script_key="absent")
    c["workflow_requirement_ids"] = c.pop("requirement_ids")
    c["output_ids"] = ["report"]
    request["criteria"] = [c]
    with pytest.raises(ScriptError, match="script_key_missing"):
        evaluator.evaluate(**request)


def test_storage_refusal_does_not_become_pass(evaluation, monkeypatch):
    evaluator, request, _ = evaluation()
    request["criteria"] = [criterion("digest_matches")]
    def refuse(locator):
        raise ArtifactStoreError("run_lock_lost", "escritor perdeu o lock")
    monkeypatch.setattr(evaluator.store, "read", refuse)
    with pytest.raises(EvaluationError, match="artifact_read_failed"):
        evaluator.evaluate(**request)


def test_clock_must_be_injected_and_shared_with_journal(tmp_path):
    journal, executor, *_ = build_simulation(tmp_path)
    try:
        with pytest.raises(TypeError):
            DeterministicEvaluator(executor.store, executor.script)
        with pytest.raises(EvaluationError, match="invalid_clock"):
            DeterministicEvaluator(executor.store, executor.script, clock=lambda: "2026-11-10T12:00:00Z")
        with pytest.raises(EvaluationError, match="clock_mismatch"):
            DeterministicEvaluator(executor.store, executor.script, clock=executor.script.make_clock())
    finally:
        journal.close()
