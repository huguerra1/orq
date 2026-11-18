"""Gate 8A: contratos, schemas fechados e guardas da Fase 8 (sem executor/runner)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from conftest import load_fixture
from orq_contracts.canonical import canonical_digest
from orq_contracts.schemas import load_schemas, validator_for
from orq_contracts.validator import (
    validate_instance,
    validate_quota_consumption_record,
    validate_task,
    validate_workflow,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures/v0.1"
NEW_SCHEMAS = (
    "rubric_spec",
    "rubric_evaluation_evidence",
    "evaluator_routing_policy",
    "post_hoc_rubric_audit_record",
)
VALID = {
    "rubric-spec": "rubric_spec",
    "evaluator-routing-policy": "evaluator_routing_policy",
    "rubric-evaluation-evidence": "rubric_evaluation_evidence",
    "usage-record-evaluation": "usage_record",
    "quota-consumption-record-evaluation": "quota_consumption_record",
    "quota-state-record-evaluation": "quota_state_record",
    "evaluation-report-rubric": "evaluation_report",
    "post-hoc-rubric-audit-record": "post_hoc_rubric_audit_record",
    "journal-event-evaluator-dispatch": "journal_event",
}
PINNED_DIGESTS = {
    "rubric-spec": "d55cb012e7ceeb957c82497db76409b0eb8aee70a91330cc76f5414cbe25a2f5",
    "evaluator-routing-policy": "8e13d552bc16987f7ad6b0bf4ffaa76f41442e54c09b24e55847c447be6b055e",
    "rubric-evaluation-evidence": "abe80500b1493b05ca83d5e6c20baa9c35beaddae6faec4cc3bf00dd3089b3d8",
    "usage-record-evaluation": "19efd230c95ff70f25b252886c3c73f629e360ea07af23a0a54bb0784f24eb26",
    "quota-consumption-record-evaluation": "3cac85f2072b1975eda7f205e4aad9ca65441dba72475c115f4d10bc55d6b399",
    "quota-state-record-evaluation": "3cc0deac6fd56937f790f8dbcada69651158258b0f074158ca6107d3a667e19f",
    "evaluation-report-rubric": "f680d1330e190d63a8d0ba7930533e5ba15779d911be5700c6d5d7a1aea5d5fa",
    "post-hoc-rubric-audit-record": "2c8e701bfdbd881cc5176536e4ee4c35d4e50097600b290fb52a9c0256ecdd77",
    "journal-event-evaluator-dispatch": "456b56b87528e732b74746fab90877f499dec4a92f1b77b0461136d5b7371ec4",
}


def load(name: str, directory: str = "valid") -> dict:
    return json.loads((FIXTURES / directory / f"{name}.json").read_text(encoding="utf-8"))


def sign(record: dict, field: str = "record_hash") -> dict:
    record[field] = canonical_digest({key: value for key, value in record.items() if key != field})
    return record


def codes(record: dict, schema: str, *, calibration_mode: bool | None = None) -> set[str]:
    return {issue.code for issue in validate_instance(record, schema, calibration_mode=calibration_mode)}


def test_new_phase8_schemas_are_closed_draft_2020_12() -> None:
    schemas = load_schemas()
    for name in NEW_SCHEMAS:
        schema = schemas[name]
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False


@pytest.mark.parametrize("filename,schema", VALID.items())
def test_valid_phase8_fixtures_and_pinned_jcs(filename: str, schema: str) -> None:
    record = load(filename)
    assert validate_instance(record, schema) == []
    field = "event_hash" if schema == "journal_event" else "record_hash"
    unsigned = {key: value for key, value in record.items() if key != field}
    digest = canonical_digest(unsigned)
    assert digest["value"] == PINNED_DIGESTS[filename]
    assert record[field] == digest


@pytest.mark.parametrize("schema", NEW_SCHEMAS)
def test_phase8_required_and_additional_properties(schema: str) -> None:
    filename = next(name for name, kind in VALID.items() if kind == schema)
    record = load(filename)
    for field in load_schemas()[schema]["required"]:
        incomplete = copy.deepcopy(record)
        del incomplete[field]
        assert "schema.required" in codes(incomplete, schema)
    record["unexpected"] = True
    assert "schema.additionalProperties" in codes(record, schema)


@pytest.mark.parametrize(
    ("filename", "schema", "code", "repair"),
    [
        (
            "rubric-spec-weights-mismatch",
            "rubric_spec",
            "semantic.rubric_weights_sum_mismatch",
            {"weight": "0.50"},
        ),
        (
            "rubric-evaluation-evidence-invalid-citation",
            "rubric_evaluation_evidence",
            "semantic.invalid_citation_must_be_inconclusive",
            {"derived_status": "inconclusive"},
        ),
        (
            "evaluator-routing-policy-overlap",
            "evaluator_routing_policy",
            "semantic.overlapping_evaluator_families",
            {"fallback_evaluator_families": ["family_claude", "family_codex"]},
        ),
        (
            "evaluation-report-rubric-override",
            "evaluation_report",
            "schema.const",
            {"status": "pass"},
        ),
        (
            "usage-record-evaluation-zero",
            "usage_record",
            "invalid_subscription_cost_zero",
            {
                "cost_lines": [
                    {
                        "amount": None,
                        "currency": None,
                        "status": "unavailable",
                        "pricing_ref": None,
                        "method": "personal_subscription_unpriced",
                    }
                ]
            },
        ),
    ],
)
def test_invalid_phase8_fixtures_have_documented_defect_and_repair(
    filename: str, schema: str, code: str, repair: dict
) -> None:
    record = load(filename, "invalid")
    assert code in codes(record, schema)
    assert record["record_hash"] == canonical_digest({k: v for k, v in record.items() if k != "record_hash"})
    if filename == "rubric-spec-weights-mismatch":
        record["dimensions"][1]["weight"] = repair["weight"]
    elif filename == "evaluation-report-rubric-override":
        record["criterion_results"][0]["status"] = repair["status"]
    else:
        record.update(repair)
    assert validate_instance(sign(record), schema) == []


def test_R03_rule5_and_workflow_scope_guards() -> None:
    # R03(a): critério determinístico obrigatório fail com model_rubric pass não pode gerar pass/inconclusive
    report = load("evaluation-report-rubric")
    report["criterion_results"][0]["status"] = "fail"
    report["verdict"] = "inconclusive"
    assert "semantic.required_criterion_failed_verdict_mismatch" in codes(sign(report), "evaluation_report")

    # R01 / guarda determinística: evaluator_snapshot deterministic exige model_rubric = not_run
    det_report = load("evaluation-report")
    rubric = load("rubric-spec")
    det_report["criteria_snapshot"]["criteria"].append(
        {
            "criterion_id": "rubric-extra",
            "requirement_ids": ["R2"],
            "method": "model_rubric",
            "pass_rule": {
                "output_ids": ["code_patch"],
                "rubric_ref": {"id": rubric["rubric_id"], "version": rubric["version"], "content_digest": rubric["record_hash"]},
                "prompt_template_ref": {"id": "prompt-1", "version": "0.1", "content_digest": canonical_digest("p1")},
            },
            "required": False,
            "verifier_ref": None,
        }
    )
    det_report["criteria_snapshot"]["criteria_hash"] = canonical_digest(det_report["criteria_snapshot"]["criteria"])
    det_report["criterion_results"].append(
        {
            "criterion_id": "rubric-extra",
            "required": False,
            "requirement_ids": ["R2"],
            "method": "model_rubric",
            "status": "pass",
            "evidence_refs": [],
            "observations": [],
            "error_record_ids": [],
        }
    )
    assert "schema.const" in codes(sign(det_report), "evaluation_report")

    # R03(b): WorkflowSpec com model_rubric em evaluation_criteria é rejeitado
    wf = load_fixture("valid/workflow-minimal.json")
    wf["evaluation_criteria"].append(
        {
            "criterion_id": "WCRIT_RUBRIC",
            "workflow_requirement_ids": ["WREQ1"],
            "output_ids": ["final-report"],
            "method": "model_rubric",
            "pass_rule": {
                "output_ids": ["final-report"],
                "rubric_ref": {"id": rubric["rubric_id"], "version": rubric["version"], "content_digest": rubric["record_hash"]},
                "prompt_template_ref": {"id": "prompt-1", "version": "0.1", "content_digest": canonical_digest("p1")},
            },
            "required": False,
            "verifier_ref": None,
        }
    )
    assert "workflow_scope_model_rubric_not_supported" in {i.code for i in validate_workflow(wf)}
    assert "workflow_scope_model_rubric_not_supported" in codes(wf, "workflow")

    # R03(b) em EvaluationReport de escopo workflow
    wf_report = load("evaluation-report-workflow")
    wf_report["criteria_snapshot"]["criteria"][0].update(
        method="model_rubric",
        pass_rule={
            "output_ids": ["code_patch"],
            "rubric_ref": {"id": rubric["rubric_id"], "version": rubric["version"], "content_digest": rubric["record_hash"]},
            "prompt_template_ref": {"id": "prompt-1", "version": "0.1", "content_digest": canonical_digest("p1")},
        },
    )
    wf_report["criteria_snapshot"]["criteria_hash"] = canonical_digest(wf_report["criteria_snapshot"]["criteria"])
    wf_report["criterion_results"][0].update(method="model_rubric", status="not_run")
    assert "workflow_scope_model_rubric_not_supported" in codes(sign(wf_report), "evaluation_report")


def test_R05_rubric_spec_semantic_invariants() -> None:
    base = load("rubric-spec")

    bad_scale = copy.deepcopy(base)
    bad_scale["scale"] = {"min_score": 4, "max_score": 4}
    assert "semantic.invalid_rubric_scale" in codes(sign(bad_scale), "rubric_spec")

    bad_min = copy.deepcopy(base)
    bad_min["dimensions"][0]["mandatory_minimum_score"] = 5
    assert "semantic.invalid_mandatory_minimum_score" in codes(sign(bad_min), "rubric_spec")

    bad_band = copy.deepcopy(base)
    bad_band["inconclusive_band"] = {"lower": "0.60", "upper": "0.70"}
    assert "semantic.invalid_inconclusive_band" in codes(sign(bad_band), "rubric_spec")

    bad_cit = copy.deepcopy(base)
    bad_cit["dimensions"][0]["min_citations"] = 0
    assert "semantic.rubric_citations_required" in codes(sign(bad_cit), "rubric_spec")


def test_R10_R11_evaluation_usage_quota_and_events_aggregate_isolation() -> None:
    usage = load("usage-record-evaluation")
    cons = load("quota-consumption-record-evaluation")
    assert validate_quota_consumption_record(cons, usage_record=usage) == []

    # Custo zero rejeitado
    bad_usage = copy.deepcopy(usage)
    bad_usage["cost_lines"][0].update(status="reported", amount="0", currency="USD")
    assert "invalid_subscription_cost_zero" in codes(sign(bad_usage), "usage_record")

    # Mismatch de escopo entre consumo e uso
    mismatch = copy.deepcopy(cons)
    mismatch["evaluation_id"] = "evaluation-other"
    assert "semantic.quota_usage_scope_mismatch" in {
        i.code for i in validate_quota_consumption_record(sign(mismatch), usage_record=usage)
    }

    # Eventos de uso e consumo de avaliação só entram no agregado evaluation
    base_ev = load("journal-event-evaluator-dispatch")
    usage_ev = copy.deepcopy(base_ev)
    usage_ev.update(event_type="usage_attached", payload=usage)
    assert validate_instance(sign(usage_ev, "event_hash"), "journal_event") == []
    for wrong_agg in ("run", "task", "attempt"):
        bad_ev = copy.deepcopy(usage_ev)
        bad_ev["aggregate_type"] = wrong_agg
        assert codes(sign(bad_ev, "event_hash"), "journal_event")

    cons_ev = copy.deepcopy(base_ev)
    cons_ev.update(event_type="quota_consumption_recorded", payload=cons)
    assert validate_instance(sign(cons_ev, "event_hash"), "journal_event") == []
    for wrong_agg in ("run", "task", "attempt"):
        bad_ev = copy.deepcopy(cons_ev)
        bad_ev["aggregate_type"] = wrong_agg
        assert codes(sign(bad_ev, "event_hash"), "journal_event")

    # Todos os 5 novos eventos evaluator_* só são válidos em aggregate_type="evaluation"
    eval_payloads = {
        "evaluator_dispatch_requested": base_ev["payload"],
        "evaluator_dispatch_acknowledged": {"judge_call_id": "judge-1", "external_call_id": "call-1"},
        "evaluator_termination_confirmed": {
            "judge_call_id": "judge-1",
            "cause": "finished",
            "observed_at": "2026-11-18T00:10:00Z",
            "resolved_model_id": "gemini-2.5-pro",
            "provider_context_coverage": "platform_only",
        },
        "evaluator_reconciliation_requested": {
            "judge_call_id": "judge-1",
            "dispatch_idempotency_key": "k-1",
            "external_call_id": None,
            "deadline_at": "2026-11-18T00:12:00Z",
        },
        "evaluator_reconciliation_observed": {
            "judge_call_id": "judge-1",
            "outcome": "found_finished",
            "external_call_id": "call-1",
            "evidence_refs": [],
        },
    }
    for ev_type, payload in eval_payloads.items():
        ev = copy.deepcopy(base_ev)
        ev.update(event_type=ev_type, payload=payload)
        assert validate_instance(sign(ev, "event_hash"), "journal_event") == []
        for wrong in ("run", "task", "attempt"):
            bad = copy.deepcopy(ev)
            bad["aggregate_type"] = wrong
            assert "schema.enum" in codes(sign(bad, "event_hash"), "journal_event")


def test_R08_R14_R15_quota_state_evaluation_and_evidence_validations() -> None:
    qstate = load("quota-state-record-evaluation")
    err = sign(
        {
            "schema_version": "0.1",
            "error_id": "err-eval-quota-1",
            "run_id": qstate["run_id"],
            "scope_type": "evaluation",
            "scope_ref": qstate["source_evaluation_id"],
            "phase": "evaluation",
            "category": "provider",
            "code": "quota_limit_reached",
            "message": "quota limit reached in evaluator CLI",
            "occurred_at": qstate["observed_at"],
            "recorded_at": qstate["observed_at"],
            "retryability": "retryable",
            "termination_certainty": "confirmed",
            "external_call_ref": None,
            "cause_error_refs": [],
            "diagnostic_artifact_refs": [],
            "quota_state": qstate,
        }
    )
    assert validate_instance(err, "error_record") == []

    err_mismatch = copy.deepcopy(err)
    err_mismatch["scope_ref"] = "evaluation-other"
    assert "semantic.quota_evaluation_mismatch" in codes(sign(err_mismatch), "error_record")

    # QuotaStateRecord não pode ter ambos source_attempt_id e source_evaluation_id preenchidos
    both_sources = copy.deepcopy(qstate)
    both_sources["source_attempt_id"] = "attempt-T3-1"
    assert "schema.oneOf" in codes(sign(both_sources), "quota_state_record")

    # R14/R15: line_range invertido e bloqueante falhando com derived_status="pass"
    evid = load("rubric-evaluation-evidence")
    bad_range = copy.deepcopy(evid)
    bad_range["dimension_assessments"][0]["citations"][0]["locator"] = {"kind": "line_range", "start_line": 5, "end_line": 2}
    assert "semantic.invalid_citation_line_range" in codes(sign(bad_range), "rubric_evaluation_evidence")

    bad_block = copy.deepcopy(evid)
    bad_block["blocking_dimension_failed"] = True
    assert "semantic.blocking_dimension_must_fail" in codes(sign(bad_block), "rubric_evaluation_evidence")


def test_R23_and_R25_calibration_and_7f_guards() -> None:
    rubric = load("rubric-spec")
    rubric_ref = {"id": rubric["rubric_id"], "version": rubric["version"], "content_digest": rubric["record_hash"]}
    prompt_ref = {"id": "prompt-1", "version": "0.1", "content_digest": canonical_digest("p1")}

    # R23: allow_same_for_calibration rejeitado em task/manifest/report normal e permitido em calibration_mode=True
    task = load_fixture("valid/task-minimal.json")
    task["evaluation_criteria"].append(
        {
            "criterion_id": "crit-rubric",
            "requirement_ids": ["REQ1"],
            "method": "model_rubric",
            "pass_rule": {
                "output_ids": ["report"],
                "rubric_ref": rubric_ref,
                "prompt_template_ref": prompt_ref,
                "cross_family_policy": "allow_same_for_calibration",
            },
            "required": True,
            "verifier_ref": None,
        }
    )
    assert "semantic.calibration_policy_requires_calibration_mode" in {i.code for i in validate_task(task)}
    assert validate_task(task, calibration_mode=True) == []

    report = load("evaluation-report-rubric")
    report["evaluator_snapshot"]["cross_family_policy"] = "allow_same_for_calibration"
    assert "semantic.calibration_policy_requires_calibration_mode" in codes(sign(report), "evaluation_report")
    assert validate_instance(sign(report), "evaluation_report", calibration_mode=True) == []

    # R25(a): experimento 7F proíbe model_rubric e calibration_mode
    wf7f = load_fixture("valid/workflow-minimal.json")
    wf7f["project_id"] = "orq-7f"
    task_7f = copy.deepcopy(task)
    task_7f["task_id"] = "T1"
    task_7f["evaluation_criteria"][-1]["pass_rule"]["cross_family_policy"] = "require_different"
    wf7f["tasks"][0] = task_7f
    assert "experiment_7f_forbids_model_rubric" in {i.code for i in validate_workflow(wf7f)}
    assert "experiment_7f_forbids_model_rubric" in codes(wf7f, "workflow")

    intent_7f = load_fixture("valid/run-intent.json")
    intent_7f["project_id"] = "orq-7f"
    intent_7f["experiment_metadata"] = {"experiment_id": "7F", "calibration_mode": True}
    assert "experiment_7f_forbids_model_rubric" in codes(sign(intent_7f), "run_intent")

    manifest_7f = load_fixture("valid/run-manifest.json")
    manifest_7f["project_id"] = "orq-7f"
    manifest_7f["evaluator_snapshot"] = report["evaluator_snapshot"]
    assert "experiment_7f_forbids_model_rubric" in codes(sign(manifest_7f), "run_manifest")
