"""Gate 8E: calibração determinística de juízes, viés Δ_self e auditoria pós-hoc R22–R25."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from conftest import ROOT, load_fixture
from orq_contracts.calibration import (
    CALIBRATION_FAMILIES,
    cohen_kappa_binary,
    compute_cfpr,
    compute_mad_inter,
    compute_precision_recall_f1,
    kendall_tau_b,
    krippendorff_alpha_ordinal,
    run_calibration,
    run_post_hoc_rubric_audit,
)
from orq_contracts.canonical import canonical_digest
from orq_contracts.cli import main as cli_main
from orq_contracts.executor import ExecutorError
from orq_contracts.experiment_7f import audit_and_analyze
from orq_contracts.runner import SequentialRunner
from orq_contracts.schemas import load_schemas
from orq_contracts.simulated_executor import SimulationScript
from orq_contracts.validator import validate_instance, validate_workflow


CAL_DIR = ROOT / "fixtures" / "v0.1" / "calibration"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _tree_digests(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory).as_posix()): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def test_calibration_schema_fixtures_and_validate_cli(capsys) -> None:
    schema = load_schemas()["calibration_report"]
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False

    valid_report = load_fixture("valid/calibration-report.json")
    assert validate_instance(valid_report, "calibration_report") == []

    bad_report = load_fixture("invalid/calibration-report-bad.json")
    codes = {issue.code for issue in validate_instance(bad_report, "calibration_report")}
    assert "semantic.record_hash_mismatch" in codes
    assert "semantic.calibration_sample_count_mismatch" in codes
    assert "semantic.calibration_gates_mismatch" in codes

    # Validação via CLI para os contratos da Fase 8
    for kind, relpath in (
        ("rubric_spec", "valid/rubric-spec.json"),
        ("evaluator_routing_policy", "valid/evaluator-routing-policy.json"),
        ("rubric_evaluation_evidence", "valid/rubric-evaluation-evidence.json"),
        ("calibration_report", "valid/calibration-report.json"),
        ("post_hoc_rubric_audit_record", "valid/post-hoc-rubric-audit-record.json"),
    ):
        assert cli_main(["validate", kind, str(ROOT / "fixtures" / "v0.1" / relpath)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"valid": True, "errors": []}

    assert cli_main(["validate", "calibration_report", str(ROOT / "fixtures" / "v0.1" / "invalid" / "calibration-report-bad.json")]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False

    # Estrutura balanceada do corpus inicial em draft_pending_owner_review (ADR 0009 Decisão 6)
    corpus = _read_json(CAL_DIR / "corpus-v0.1.json")
    assert corpus["calibrated_from_pilot"] is False
    assert corpus["curation_status"] == "draft_pending_owner_review"
    items = corpus["items"]
    assert len(items) == 24
    assert sum(1 for i in items if i["category"] == "gold_pass") == 10
    assert sum(1 for i in items if i["category"] == "seeded_semantic_fail") == 10
    assert sum(1 for i in items if i["category"] == "borderline_case") == 4
    defects = {i["defect_category"] for i in items if i["category"] == "seeded_semantic_fail"}
    assert defects == {
        "missing_auth_check",
        "silent_error_swallow",
        "missing_contract_validation",
        "inconsistent_state_transition",
        "missing_negative_test",
    }
    for fam in CALIBRATION_FAMILIES:
        assert sum(1 for i in items if i["author_family_id"] == fam) == 6


def test_R22_cfpr_citation_gate_and_reproducibility(tmp_path: Path, capsys) -> None:
    corpus_path = CAL_DIR / "corpus-v0.1.json"
    rubric_path = CAL_DIR / "rubric-code-review-v1.json"
    resp_path = CAL_DIR / "panel-responses-pass.json"

    out1 = tmp_path / "cal1"
    out2 = tmp_path / "cal2"
    rc1 = cli_main([
        "calibrate", "evaluator",
        "--corpus", str(corpus_path),
        "--rubric", str(rubric_path),
        "--responses", str(resp_path),
        "--output-dir", str(out1),
        "--recorded-at", "2026-11-18T00:00:00Z",
    ])
    stdout1 = json.loads(capsys.readouterr().out)
    rc2 = cli_main([
        "calibrate", "evaluator",
        "--corpus", str(corpus_path),
        "--rubric", str(rubric_path),
        "--responses", str(resp_path),
        "--output-dir", str(out2),
        "--recorded-at", "2026-11-18T00:00:00Z",
    ])
    stdout2 = json.loads(capsys.readouterr().out)

    assert rc1 == 0 and rc2 == 0
    assert stdout1 == stdout2
    assert stdout1["gates_passed"] is True
    assert stdout1["failed_gates"] == []
    assert (out1 / "calibration-report.json").read_bytes() == (out2 / "calibration-report.json").read_bytes()

    usages = _read_json(out1 / "usage-records.json")
    assert len(usages) == 4
    for u in usages:
        assert u["cost_lines"][0]["status"] == "unavailable"
        assert u["cost_lines"][0]["amount"] is None
        assert u["cost_lines"][0]["currency"] is None

    # Injeta 1 falso positivo crítico em seeded_fail (1/10 = 0.1000 > 0.05) -> gate_cfpr
    bad_cfpr = _read_json(resp_path)
    bad_cfpr["family_codex"]["cal-11"] = {
        "raw_scores": {"security-and-isolation": 4, "contract-and-error-integrity": 4, "verification-depth": 4},
        "citation_mode": "valid",
    }
    bad_cfpr_path = tmp_path / "resp-bad-cfpr.json"
    bad_cfpr_path.write_text(json.dumps(bad_cfpr), encoding="utf-8")
    out_cfpr = tmp_path / "out_cfpr"
    assert cli_main([
        "calibrate", "evaluator",
        "--corpus", str(corpus_path),
        "--rubric", str(rubric_path),
        "--responses", str(bad_cfpr_path),
        "--output-dir", str(out_cfpr),
    ]) == 1
    rep_cfpr = json.loads(capsys.readouterr().out)
    assert rep_cfpr["gates_passed"] is False
    assert "gate_cfpr" in rep_cfpr["failed_gates"]

    # Injeta citações inválidas (>2% inválidas -> valid_citation_rate < 0.98) -> gate_valid_citations
    bad_cit = _read_json(resp_path)
    for item_id in ("cal-01", "cal-02"):
        bad_cit["family_gemini"][item_id]["citation_mode"] = "nonexistent_excerpt"
    rep_cit = run_calibration(
        corpus=_read_json(corpus_path),
        rubric=_read_json(rubric_path),
        responses=bad_cit,
    )
    assert rep_cit["gates_passed"] is False
    assert "gate_valid_citations" in rep_cit["failed_gates"]


def test_R23_inter_evaluator_alpha_kendall_mad_and_metrics() -> None:
    assert compute_cfpr(["fail", "pass", "pass"], ["fail", "fail", "pass"]) == Decimal("0.5000")
    prec, rec, f1 = compute_precision_recall_f1(["fail", "fail", "pass"], ["fail", "pass", "pass"])
    assert (prec, rec, f1) == (Decimal("0.5000"), Decimal("1.0000"), Decimal("0.6667"))
    assert cohen_kappa_binary(["pass", "fail", "pass", "fail"], ["pass", "fail", "pass", "fail"]) == Decimal("1.0000")
    assert krippendorff_alpha_ordinal([[4, 4, 4], [1, 1, 1], [3, 3, 3]]) == Decimal("1.0000")
    assert kendall_tau_b([Decimal("0.9"), Decimal("0.5"), Decimal("0.2")], [Decimal("0.9"), Decimal("0.5"), Decimal("0.2")]) == Decimal("1.0000")
    assert compute_mad_inter([[Decimal("0.9"), Decimal("0.7")]]) == Decimal("0.2000")

    corpus = _read_json(CAL_DIR / "corpus-v0.1.json")
    rubric = _read_json(CAL_DIR / "rubric-code-review-v1.json")
    noisy = _read_json(CAL_DIR / "panel-responses-pass.json")

    # Ruído forte nas duas famílias secundárias derruba alpha_ord, MAD_inter e tau_b
    for idx, item in enumerate(corpus["items"]):
        item_id = item["item_id"]
        noisy["family_claude"][item_id]["raw_scores"] = {
            "security-and-isolation": 4 if idx % 2 == 0 else 1,
            "contract-and-error-integrity": 4 if idx % 2 == 0 else 1,
            "verification-depth": 4 if idx % 2 == 0 else 1,
        }
        noisy["family_kiro"][item_id]["raw_scores"] = {
            "security-and-isolation": 1 if idx % 2 == 0 else 4,
            "contract-and-error-integrity": 1 if idx % 2 == 0 else 4,
            "verification-depth": 1 if idx % 2 == 0 else 4,
        }

    rep = run_calibration(corpus=corpus, rubric=rubric, responses=noisy)
    assert rep["gates_passed"] is False
    assert "gate_krippendorff_global" in rep["failed_gates"]
    assert "gate_krippendorff_dimension:security-and-isolation" in rep["failed_gates"]
    assert "gate_mad_inter" in rep["failed_gates"]
    assert "gate_cohen_kappa" in rep["failed_gates"]
    assert Decimal(rep["inter_evaluator_metrics"]["krippendorff_alpha_ordinal_global"]) < Decimal("0.67")
    assert Decimal(rep["inter_evaluator_metrics"]["mad_inter"]) > Decimal("0.15")

    # Fora de calibration_mode=true, allow_same_for_calibration é proibido em runs normais
    wf_cal = load_fixture("valid/workflow-reference.json")
    wf_cal["tasks"][0]["evaluation_criteria"].append({
        "criterion_id": "rubric-cal-check",
        "requirement_ids": ["REQ1"],
        "method": "model_rubric",
        "pass_rule": {
            "output_ids": [wf_cal["tasks"][0]["expected_output"][0]["output_id"]],
            "rubric_ref": load_fixture("valid/calibration-report.json")["rubric_ref"],
            "prompt_template_ref": {"id": "p", "version": "0.1", "content_digest": canonical_digest("p")},
            "cross_family_policy": "allow_same_for_calibration",
        },
        "required": True,
        "verifier_ref": None,
    })
    assert any(i.code == "semantic.calibration_policy_requires_calibration_mode" for i in validate_workflow(wf_cal, calibration_mode=False))
    assert not validate_workflow(wf_cal, calibration_mode=True)


def test_R24_self_preference_matrix_and_disqualification() -> None:
    corpus = _read_json(CAL_DIR / "corpus-v0.1.json")
    rubric = _read_json(CAL_DIR / "rubric-code-review-v1.json")
    biased = _read_json(CAL_DIR / "panel-responses-pass.json")

    # Infla sistematicamente todos os 6 artefatos próprios de family_codex quando avaliados pela própria family_codex
    for item in corpus["items"]:
        if item["author_family_id"] == "family_codex":
            # Outras famílias dão score baixo (borderline/fail), codex dá 4/4/4 para si mesma (+0.50..+0.75 > 0.10, p < 0.05)
            biased["family_codex"][item["item_id"]]["raw_scores"] = {
                "security-and-isolation": 4,
                "contract-and-error-integrity": 4,
                "verification-depth": 4,
            }
            for other in ("family_claude", "family_gemini", "family_kiro"):
                biased[other][item["item_id"]]["raw_scores"] = {
                    "security-and-isolation": 1,
                    "contract-and-error-integrity": 1,
                    "verification-depth": 1,
                }

    rep = run_calibration(corpus=corpus, rubric=rubric, responses=biased)
    assert len(rep["self_preference_metrics"]["matrix"]) == 16  # matriz 4x4 completa
    codex_entry = next(b for b in rep["self_preference_metrics"]["by_family"] if b["family_id"] == "family_codex")
    assert codex_entry["n_paired"] == 6
    assert Decimal(codex_entry["delta_self"]) > Decimal("0.10")
    assert Decimal(codex_entry["wilcoxon_p_value"]) < Decimal("0.05")
    assert codex_entry["disqualified"] is True
    assert "family_codex" in rep["recommended_thresholds"]["disqualified_evaluator_families"]


def test_R25_7f_forbids_model_rubric_and_post_hoc_audit_immutable(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORQ_MEMORY_DSN", raising=False)
    monkeypatch.delenv("ORQ_KNOWLEDGE_BACKEND", raising=False)

    # Guarda 7F rejeita model_rubric e calibration_mode=true
    wf = load_fixture("valid/workflow-reference.json")
    wf_7f = deepcopy(wf)
    wf_7f["project_id"] = "orq-7f"
    wf_7f["tasks"][0]["evaluation_criteria"].append({
        "criterion_id": "rubric-forbidden-in-7f",
        "requirement_ids": ["REQ1"],
        "method": "model_rubric",
        "pass_rule": {
            "output_ids": [wf_7f["tasks"][0]["expected_output"][0]["output_id"]],
            "rubric_ref": load_fixture("valid/calibration-report.json")["rubric_ref"],
            "prompt_template_ref": {"id": "p", "version": "0.1", "content_digest": canonical_digest("p")},
        },
        "required": True,
        "verifier_ref": None,
    })
    assert any(i.code == "experiment_7f_forbids_model_rubric" for i in validate_workflow(wf_7f))

    # Executa run determinístico completo (camada simulada) e captura snapshot criptográfico
    fixtures = ROOT / "fixtures" / "v0.1"
    store_root = tmp_path / "store"
    doc = _read_json(fixtures / "valid" / "simulation-script-reference.json")
    script = SimulationScript(doc, expected_digest=canonical_digest(doc))
    with SequentialRunner.create(
        store_root,
        workflow=_read_json(fixtures / "valid" / "workflow-reference.json"),
        request=_read_json(fixtures / "run-request.json"),
        catalog=_read_json(fixtures / "run-catalog.json"),
        policy=_read_json(fixtures / "run-policy.json"),
        script=script,
        knowledge_catalog=_read_json(fixtures / "vault" / "catalog-snapshot.json"),
        vault_root=fixtures / "vault",
        clock=script.make_clock(),
    ) as runner:
        proj = runner.run()
        assert proj.state == "succeeded"

    run_dir = store_root / "runs" / proj.id
    snapshot_before = _tree_digests(run_dir)
    assert "run.lock" not in snapshot_before

    # Roda auditoria pós-hoc opcional estritamente somente leitura via CLI `orq audit rubric`
    audit_out = tmp_path / "audit_out"
    rubric_path = CAL_DIR / "rubric-code-review-v1.json"
    cal_report_path = fixtures / "valid" / "calibration-report.json"
    rc = cli_main([
        "audit", "rubric",
        "--store-root", str(store_root),
        "--run-id", proj.id,
        "--rubric", str(rubric_path),
        "--calibration-report", str(cal_report_path),
        "--output-dir", str(audit_out),
        "--recorded-at", "2026-11-18T01:00:00Z",
    ])
    assert rc == 0
    cli_rec = json.loads(capsys.readouterr().out)
    assert validate_instance(cli_rec, "post_hoc_rubric_audit_record") == []
    assert cli_rec["source_run_id"] == proj.id
    assert cli_rec["source_deterministic_verdict"] == "pass"
    assert cli_rec["audit_derived_status"] == "pass"

    # Snapshot completo da pasta do run deve permanecer 100% inalterado (nem run.lock criado!)
    snapshot_after = _tree_digests(run_dir)
    assert snapshot_after == snapshot_before

    # Também valida sobre run real do harness 7F (`experiment-7f quota-stress`) e `audit_and_analyze`
    package_7f = ROOT / "experiments" / "7f"
    store_7f = tmp_path / "store_7f"
    assert cli_main(["experiment-7f", "quota-stress", "--package-dir", str(package_7f), "--store-root", str(store_7f)]) == 0
    q29 = json.loads(capsys.readouterr().out)
    run_7f_id = q29["arms"]["quota_aware_multi_family"]["run_id"]
    before_7f_tree = _tree_digests(store_7f)
    before_7f_audit = audit_and_analyze(store_7f, package_dir=package_7f)
    assert cli_main([
        "audit", "rubric",
        "--store-root", str(store_7f),
        "--run-id", run_7f_id,
        "--rubric", str(rubric_path),
        "--calibration-report", str(cal_report_path),
        "--output-dir", str(tmp_path / "audit_7f_out"),
        "--recorded-at", "2026-11-18T01:05:00Z",
    ]) == 0
    rec_7f = json.loads(capsys.readouterr().out)
    assert rec_7f["source_run_id"] == run_7f_id
    assert validate_instance(rec_7f, "post_hoc_rubric_audit_record") == []
    assert _tree_digests(store_7f) == before_7f_tree
    assert audit_and_analyze(store_7f, package_dir=package_7f) == before_7f_audit

    # UsageRecord da auditoria continua unavailable / amount=null e saída não vaza workspace tmp
    usage_files = list(audit_out.glob("usage-*.json"))
    assert len(usage_files) == 1
    usage_rec = _read_json(usage_files[0])
    assert usage_rec["cost_lines"][0]["status"] == "unavailable"
    assert usage_rec["cost_lines"][0]["amount"] is None
    assert not (audit_out / "_eval_tmp").exists()

    # Família desqualificada na calibração não pode ser escolhida pela auditoria pós-hoc
    cal_disq = deepcopy(_read_json(cal_report_path))
    cal_disq["recommended_thresholds"]["disqualified_evaluator_families"] = ["family_gemini"]
    cal_disq["record_hash"] = canonical_digest({k: v for k, v in cal_disq.items() if k != "record_hash"})
    audit_out_2 = tmp_path / "audit_out_2"
    audit_rec_2 = run_post_hoc_rubric_audit(
        store_root=store_root,
        run_id=proj.id,
        rubric=_read_json(rubric_path),
        calibration_report=cal_disq,
        output_dir=audit_out_2,
        recorded_at="2026-11-18T01:00:00Z",
    )
    cons_files_2 = list(audit_out_2.glob("consumption-*.json"))
    assert len(cons_files_2) == 1
    assert _read_json(cons_files_2[0])["family_id"] != "family_gemini"
    assert validate_instance(audit_rec_2, "post_hoc_rubric_audit_record") == []

    # Run não encerrado é recusado pela auditoria pós-hoc
    incomplete_store = tmp_path / "incomplete" / "runs" / "run-open"
    incomplete_store.mkdir(parents=True)
    lines = (run_dir / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    (incomplete_store / "journal.jsonl").write_text(lines[0] + "\n", encoding="utf-8")
    with pytest.raises(ExecutorError, match="run_not_closed"):
        run_post_hoc_rubric_audit(
            store_root=tmp_path / "incomplete",
            run_id="run-open",
            rubric=_read_json(rubric_path),
            calibration_report=_read_json(cal_report_path),
            output_dir=tmp_path / "audit_fail",
        )
