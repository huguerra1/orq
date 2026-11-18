"""Experimento científico 7F (Q28–Q30): execução pareada Camada A, estresse de cota e Postgres real no CI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket

import pytest

from conftest import ROOT
from orq_contracts.canonical import canonical_digest
from orq_contracts.cli import main
from orq_contracts.experiment_7f import (
    Experiment7FError,
    audit_and_analyze,
    cliffs_delta,
    evaluate_7f_checks,
    mcnemar_exact,
    run_quota_stress_comparison,
    run_simulated_experiment,
    verify_package,
    wilcoxon_signed_rank_exact,
)
from orq_contracts.memory_store import build_sync_plan, resolve_memory_config


PACKAGE_7F = ROOT / "experiments" / "7f"


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_create_connection = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _guarded_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, (tuple, list)) and address else None
        if host in _LOOPBACK_HOSTS:
            return real_create_connection(address, *args, **kwargs)
        raise AssertionError("rede externa proibida na execução 7F")

    def _guarded_getaddrinfo(host, *args, **kwargs):
        if host in _LOOPBACK_HOSTS:
            return real_getaddrinfo(host, *args, **kwargs)
        raise AssertionError("rede externa proibida na execução 7F")

    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)


def test_no_network_guard_allows_loopback_and_blocks_external() -> None:
    assert socket.getaddrinfo("localhost", 5432)
    with pytest.raises(AssertionError, match="rede externa proibida na execução 7F"):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(AssertionError, match="rede externa proibida na execução 7F"):
        socket.create_connection(("example.com", 443), timeout=1)


def test_verify_package_accepts_canonical_7f_bundle() -> None:
    report = verify_package(PACKAGE_7F)
    assert report["valid"] is True
    assert report["experiment_id"] == "7f"
    assert report["tasks"] == 10
    assert report["batches"] == 6
    assert report["total_runs"] == 60
    assert report["verified_files"] > 0


def test_verify_package_rejects_tampered_file(tmp_path: Path) -> None:
    copied = tmp_path / "7f"
    shutil.copytree(PACKAGE_7F, copied)
    target = copied / "tasks" / "exp7f-t01-architect-claude-tools.json"
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(Experiment7FError) as err:
        verify_package(copied)
    assert err.value.code == "package_file_digest_mismatch"


def test_evaluate_7f_checks_semantics() -> None:
    answer = {"flag": "--x", "n": 1, "ok": False, "tools": ["A", "B"], "nested": {"text": "use --bad"}}
    checks = [
        {"check_id": "C1", "kind": "equals", "pointer": "/flag", "value": "--x"},
        {"check_id": "C2", "kind": "set_equals", "pointer": "/tools", "values": ["B", "A"]},
        {"check_id": "C3", "kind": "contains_all", "pointer": "/tools", "values": ["A"]},
        {"check_id": "C4", "kind": "matches", "pointer": "/flag", "pattern": "--[a-z]"},
        {"check_id": "C5", "kind": "no_forbidden_text", "values": ["--worse"]},
    ]
    results = evaluate_7f_checks(answer, checks)
    assert all(r["passed"] for r in results)

    bad = evaluate_7f_checks(
        answer,
        [
            {"check_id": "B1", "kind": "equals", "pointer": "/n", "value": True},
            {"check_id": "B2", "kind": "matches", "pointer": "/flag", "pattern": "--"},
            {"check_id": "B3", "kind": "no_forbidden_text", "values": ["--bad"]},
        ],
    )
    assert [r["passed"] for r in bad] == [False, False, False]


def test_statistical_helpers_exactness() -> None:
    w = wilcoxon_signed_rank_exact([1, 2, 4, 7, 11], [2, 4, 7, 11, 16])
    assert w["n"] == 5 and w["w_plus"] == 0.0 and w["method"] == "exact"
    assert w["p_value"] == pytest.approx(2.0 / 32.0)

    w_tied = wilcoxon_signed_rank_exact([1, 2, 3, 4, 5], [3, 4, 5, 6, 7])
    assert w_tied["n"] == 5 and w_tied["w_plus"] == 0.0 and w_tied["method"] == "normal_approx"

    mc = mcnemar_exact([1, 1, 1, 1, 1, 1, 0], [0, 0, 0, 0, 0, 0, 0])
    assert mc["b"] == 6 and mc["c"] == 0
    assert mc["p_value"] == pytest.approx(2.0 * (0.5**6))

    assert cliffs_delta([10, 20], [1, 2]) == 1.0
    assert cliffs_delta([1, 2], [10, 20]) == -1.0
    assert cliffs_delta([1, 2], [1, 2]) == 0.0


def test_Q28_and_Q30_simulated_paired_experiment_all_tasks_offline(tmp_path: Path) -> None:
    store = tmp_path / "store"
    report = run_simulated_experiment(store, package_dir=PACKAGE_7F, repetitions=1, selection_mode="agent_mcp_choice")
    runs = report["runs"]
    assert len(runs) == 20
    assert all(r["run_state"] == "succeeded" for r in runs)
    assert all(r["backend"] == "offline_markdown_bm25" for r in runs)
    assert all(r["unavailable_cost_integrity"] for r in runs)

    rls_runs = [r for r in runs if r["condition"] == "family_partitioned_rls"]
    sg_runs = [r for r in runs if r["condition"] == "shared_global"]
    assert len(rls_runs) == 10 and len(sg_runs) == 10
    assert all(r["distractor_contamination_rate"] == 0.0 for r in rls_runs)
    assert all(r["cross_family_exposure_rate"] == 0.0 for r in rls_runs)

    audit_report = audit_and_analyze(store, package_dir=PACKAGE_7F, runs=runs)
    assert audit_report["audit"]["valid"] is True
    assert audit_report["audit"]["runs_audited"] == 20
    assert audit_report["paired_comparison"]["pair_count"] == 10
    assert audit_report["conditions"]["family_partitioned_rls"]["distractor_contamination"]["median"] == 0.0
    assert audit_report["conditions"]["family_partitioned_rls"]["pass_at_1_rate"] >= (
        audit_report["conditions"]["shared_global"]["pass_at_1_rate"]
    )


def test_Q29_quota_stress_comparison_offline(tmp_path: Path) -> None:
    store = tmp_path / "store-q29"
    report = run_quota_stress_comparison(store, package_dir=PACKAGE_7F)
    no_fb = report["arms"]["single_family_no_fallback"]
    aware = report["arms"]["quota_aware_multi_family"]

    assert no_fb["run_state"] == "failed"
    assert no_fb["wasted_dispatches_on_exhausted_quota"] == 0
    assert no_fb["quota_exhausted_events"] == 1

    assert aware["run_state"] == "succeeded"
    assert aware["wasted_dispatches_on_exhausted_quota"] == 0
    assert aware["quota_exhausted_events"] == 1
    assert aware["used_fallback"] is True
    assert aware["context_manifests_sealed"] == 3
    assert aware["bundle_hashes"][1] != aware["bundle_hashes"][2]
    assert not any(src.startswith("family-codex-") for src in aware["fallback_sources"])
    assert aware["unavailable_cost_integrity"] is True


def test_Q30_audit_rejects_hash_tamper_cost_zero_and_model_rubric(tmp_path: Path) -> None:
    store = tmp_path / "store-audit"
    run_simulated_experiment(
        store,
        package_dir=PACKAGE_7F,
        task_ids=["exp7f-t01-architect-claude-tools"],
        repetitions=1,
    )
    run_dir = next((store / "runs").iterdir())
    journal_path = run_dir / "journal.jsonl"
    events = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # Violação 1: custo zero em assinatura
    usage_ev = next(e for e in events if e["event_type"] == "usage_attached")
    usage_ev["payload"]["cost_lines"][0].update(status="reported", amount="0.00", currency="USD")
    # Violação 2: critério model_rubric proibido no 7F
    eval_ev = next(e for e in events if e["event_type"] == "evaluation_report_recorded")
    eval_ev["payload"]["criteria_snapshot"]["criteria"][0]["method"] = "model_rubric"
    # Regrava sem recalcular event_hash para testar também a cadeia de hash
    journal_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    result = audit_and_analyze(store, package_dir=PACKAGE_7F)
    assert result["audit"]["valid"] is False
    violations = next(iter(result["audit"]["violations"].values()))
    assert any(v.startswith("event_hash_mismatch@") for v in violations)
    assert "subscription_cost_not_unavailable" in violations
    assert "experiment_7f_forbids_model_rubric" in violations


def test_cli_experiment_7f_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    monkeypatch.delenv("ORQ_MEMORY_DSN", raising=False)
    monkeypatch.delenv("ORQ_KNOWLEDGE_BACKEND", raising=False)

    assert main(["experiment-7f", "verify-package", "--package-dir", str(PACKAGE_7F)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["valid"] is True

    store = tmp_path / "cli-store"
    assert main(["experiment-7f", "quota-stress", "--package-dir", str(PACKAGE_7F), "--store-root", str(store)]) == 0
    q29 = json.loads(capsys.readouterr().out)
    assert q29["arms"]["quota_aware_multi_family"]["run_state"] == "succeeded"

    assert main(["experiment-7f", "audit", "--package-dir", str(PACKAGE_7F), "--store-root", str(store)]) == 0
    q30 = json.loads(capsys.readouterr().out)
    assert q30["audit"]["valid"] is True


@pytest.mark.skipif(not (os.environ.get("ORQ_MEMORY_DSN") or "").strip(), reason="ORQ_MEMORY_DSN ausente")
def test_Q28_Q29_Q30_under_real_postgres_rls(tmp_path: Path) -> None:
    psycopg = pytest.importorskip("psycopg")
    from orq_contracts.memory_store.postgres import apply_schema, connect, sync_catalog

    config = resolve_memory_config()
    snapshot = json.loads((PACKAGE_7F / "vault" / "catalog-snapshot.json").read_text(encoding="utf-8"))
    catalog_id = snapshot["catalog_id"]
    with connect(config) as admin:
        apply_schema(admin)
        sync_catalog(admin, build_sync_plan(PACKAGE_7F / "vault", snapshot))
        try:
            store_q28 = tmp_path / "pg-q28"
            q28 = run_simulated_experiment(
                store_q28,
                package_dir=PACKAGE_7F,
                task_ids=[
                    "exp7f-t01-architect-claude-tools",
                    "exp7f-t04-backend-codex-windows-tests",
                    "exp7f-t06-backend-kiro-wsl",
                    "exp7f-t09-security-claude-policy",
                ],
                repetitions=1,
                memory_config=config,
            )
            assert len(q28["runs"]) == 8
            assert all(r["backend"] == "postgres_fts_trgm" for r in q28["runs"])
            rls_runs = [r for r in q28["runs"] if r["condition"] == "family_partitioned_rls"]
            assert all(r["distractor_contamination_rate"] == 0.0 for r in rls_runs)
            assert all(r["cross_family_exposure_rate"] == 0.0 for r in rls_runs)

            store_q29 = tmp_path / "pg-q29"
            q29 = run_quota_stress_comparison(store_q29, package_dir=PACKAGE_7F, memory_config=config)
            assert q29["arms"]["quota_aware_multi_family"]["run_state"] == "succeeded"
            assert q29["arms"]["quota_aware_multi_family"]["used_fallback"] is True

            audit_q30 = audit_and_analyze(store_q28, package_dir=PACKAGE_7F, runs=q28["runs"])
            assert audit_q30["audit"]["valid"] is True
        finally:
            assert isinstance(admin, psycopg.Connection)
            with admin.transaction():
                admin.execute("DELETE FROM orq_memory.knowledge_units WHERE catalog_id = %s", (catalog_id,))
                admin.execute("DELETE FROM orq_memory.knowledge_sources WHERE catalog_id = %s", (catalog_id,))
                admin.execute("DELETE FROM orq_memory.knowledge_catalogs WHERE catalog_id = %s", (catalog_id,))
