"""Gate 8D: cenários R01–R21 ponta a ponta no CompositeEvaluator + SequentialRunner."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import uuid

import pytest

from conftest import ROOT, load_fixture
from _memory_vault import build_family_vault
from test_runner_scenarios import InjectedCrash, assert_reservations_closed, edges, events, inputs, make_runner
from orq_contracts.adapters.quota import VERSIONS
from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.cli import main as cli_main
from orq_contracts.composite_evaluator import CompositeEvaluator, default_evaluator_routing_policy
from orq_contracts.executor import ExecutorError, bytes_digest, seal_record
from orq_contracts.memory_store import MemoryScope, build_sync_plan
from orq_contracts.memory_store.config import resolve_memory_config
from orq_contracts.projections import ProjectionError
from orq_contracts.quota import QuotaStateSnapshot
from orq_contracts.runner import SequentialRunner, reference
from orq_contracts.runner_family import FamilyRuntime
from orq_contracts.simulated_executor import SimulationScript, format_utc, parse_utc
from orq_contracts.validator import validate_instance, validate_workflow


EXPECTED_SIMULATE_DIGEST = "2e571353f5f6a19eb8049e70cde2d93c7f6f2d5f93d93bb6da1b5aa0393af64c"


_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch):
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def forbidden(*args, **kwargs):
        raise AssertionError("rede externa proibida")

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host in _LOOPBACK:
            return real_getaddrinfo(host, *args, **kwargs)
        return forbidden(host, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, (tuple, list)) and address else None
        if host in _LOOPBACK:
            return real_create_connection(address, *args, **kwargs)
        return forbidden(address, *args, **kwargs)

    def loopback_connect(sock, address):
        if isinstance(address, tuple) and address[0] in _LOOPBACK:
            return original_connect(sock, address)
        return forbidden(sock, address)

    def loopback_connect_ex(sock, address):
        if isinstance(address, tuple) and address[0] in _LOOPBACK:
            return original_connect_ex(sock, address)
        return forbidden(sock, address)

    def loopback_sendto(sock, data, *args):
        address = args[-1] if args else None
        if isinstance(address, tuple) and address[0] in _LOOPBACK:
            return original_sendto(sock, data, *args)
        return forbidden(sock, address)

    for name in ("gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", loopback_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", loopback_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", loopback_sendto)


def _seal(record: dict) -> dict:
    body = {k: v for k, v in record.items() if k != "record_hash"}
    body["record_hash"] = canonical_digest(body)
    return body


class SubscriptionSimExecutor:
    """Wrapper fino sobre SimulatedExecutor compatível com FamilyRuntime + chamadas de juiz."""

    def __init__(self, runner: SequentialRunner, *, attempt_quota_fail: bool = False):
        self.runner = runner
        self.inner = runner.simulated_executor
        self.attempt_quota_fail = attempt_quota_fail
        self._sessions: dict[str, object] = {}

    def describe(self):
        return self.inner.describe()

    def bind_knowledge_session(self, intent, session):
        self._sessions[intent["attempt_id"]] = session

    def dispatch(self, intent, bundle, workspace):
        session = self._sessions.get(intent["attempt_id"])
        if session is not None and getattr(session, "selection", None) is None:
            selections: dict[str, list[dict[str, str]]] = {}
            for req in session.config["requirements"]:
                rid = req["knowledge_requirement_id"]
                resp = session.call(
                    "search_family_knowledge",
                    {"requirement_id": rid, "query": req["subject"], "limit": 5},
                )
                cands = resp.get("candidates", [])
                selections[rid] = [
                    {"source_id": cands[0]["source_id"], "revision": cands[0]["revision"], "unit_id": cands[0]["unit_id"]}
                ] if cands else []
            session.call("select_family_knowledge", {"selections": selections, "rationale": "selecao deterministica de teste"})
        return self.inner.dispatch(intent, bundle, workspace)

    def observe(self, intent, until):
        return self.inner.observe(intent, until)

    def cancel(self, intent, reason):
        return self.inner.cancel(intent, reason)

    def reconcile(self, key, external_call_id):
        return self.inner.reconcile(key, external_call_id)

    def collect(self, intent):
        result = self.inner.collect(intent)
        target = self.runner.family_runtime.target(intent)
        usage = deepcopy(result.usage[0])
        usage["subscription_id"] = target["subscription_id"]
        line = usage["cost_lines"][0]
        usage["cost_lines"] = [{**line, "status": "unavailable", "amount": None, "currency": None}]
        usage["coverage"] = {"reported": [], "estimated": [], "unavailable": ["cost"]}
        usage = seal_record(usage, "usage_record")
        from dataclasses import replace as _replace
        return _replace(result, usage=(usage,))

    def quota_consumption(self, intent):
        usage = self.collect(intent).usage[0]
        target = self.runner.family_runtime.target(intent)
        return seal_record({
            "schema_version": "0.1",
            "quota_consumption_id": f"cons-{intent['attempt_id']}",
            "run_id": intent["run_id"],
            "task_id": intent["task_id"],
            "attempt_id": intent["attempt_id"],
            **{k: target[k] for k in ("family_id", "subscription_id", "quota_pool_id")},
            "usage_record_ref": {"id": usage["usage_id"], "version": "0.1", "content_digest": canonical_digest(usage)},
            "messages_consumed": 1,
            "input_tokens": None,
            "output_tokens": None,
            "credit_multiplier": None,
            "quota_outcome": "quota_exhausted_on_call" if self.attempt_quota_fail else "accepted",
            "recorded_at": usage["recorded_at"],
        }, "quota_consumption_record")

    # Interface de juiz read-only encaminhada diretamente ao SimulatedExecutor
    def dispatch_judge(self, intent, prompt_bytes, eval_workspace):
        return self.inner.dispatch_judge(intent, prompt_bytes, eval_workspace)

    def observe_judge(self, intent, until):
        return self.inner.observe_judge(intent, until)

    def cancel_judge(self, intent, reason):
        return self.inner.cancel_judge(intent, reason)

    def reconcile_judge(self, key, external_call_id):
        return self.inner.reconcile_judge(key, external_call_id)

    def collect_judge(self, intent, **kwargs):
        return self.inner.collect_judge(intent, **kwargs)


def _rubric_spec(*, inconclusive_band: dict | None = None, min_citations: int = 1) -> dict:
    spec = load_fixture("valid/rubric-spec.json")
    spec["inconclusive_band"] = {"lower": "0.65", "upper": "0.75"} if inconclusive_band is None else inconclusive_band
    for dim in spec["dimensions"]:
        dim["min_citations"] = min_citations
    return _seal(spec)


def _setup_phase8_bundle(
    tmp_path: Path,
    *,
    rubric_eval_script: dict | list,
    kiro_model: str = "claude-sonnet-4",
    author_family: str = "family_codex",
    required_rubric: bool = True,
    routing_policy: dict | None = None,
    rubric: dict | None = None,
    panel_size: int | None = None,
    knowledge_req_ids: list[str] | None = None,
    evaluator_role_id: str = "security_reviewer",
    max_evaluations_per_attempt: int = 2,
    max_run_duration_ms: int = 10_000_000,
    attempt_override: dict | None = None,
    authoritative_not_found: bool = True,
    probe: object | None = None,
    cross_family_policy: str = "require_different",
    calibration_mode: bool = False,
    catalog_id: str = "cat-phase8-fixed",
):
    vault_dir = tmp_path / "vault"
    catalog = build_family_vault(vault_dir, catalog_id)
    rubric_obj = rubric or _rubric_spec()
    eval_policy = routing_policy or default_evaluator_routing_policy()
    if calibration_mode and eval_policy["cross_family_policy"] != "allow_same_for_calibration":
        eval_policy = _seal({**eval_policy, "cross_family_policy": "allow_same_for_calibration"})

    data = inputs()
    data["vault_root"] = vault_dir
    data["knowledge_catalog"] = catalog
    data["workflow"]["project_id"] = data["request"]["project_id"] = "proj-a"
    data["workflow"]["constraints"]["max_run_duration_ms"] = max_run_duration_ms
    data["workflow"]["constraints"]["max_evaluations_per_attempt"] = max_evaluations_per_attempt
    data["policy"]["resolved_limits"]["max_run_duration_ms"] = max_run_duration_ms
    data["policy"]["resolved_limits"]["max_evaluations_per_attempt"] = max_evaluations_per_attempt

    task = data["workflow"]["tasks"][0]
    task["constraints"] = {"max_attempts": 2, "max_evaluations_per_attempt": max_evaluations_per_attempt}
    task["agent_role"] = {"id": "backend_engineer", "version": "0.1", "content_digest": canonical_digest("backend_engineer")}
    task["task_type"] = "analysis"
    task["knowledge_requirements"] = []

    rubric_criterion = {
        "criterion_id": "rubric-sec",
        "requirement_ids": ["REQ1"],
        "method": "model_rubric",
        "pass_rule": {
            "output_ids": [task["expected_output"][0]["output_id"]],
            "rubric_ref": {
                "id": rubric_obj["rubric_id"],
                "version": rubric_obj["version"],
                "content_digest": deepcopy(rubric_obj["record_hash"]),
            },
            "prompt_template_ref": {
                "id": "prompt-rubric-eval-v1",
                "version": "0.1",
                "content_digest": canonical_digest("prompt-rubric-eval-v1:0.1"),
            },
            "cross_family_policy": cross_family_policy,
            "knowledge_requirement_ids": knowledge_req_ids or [],
            "evaluator_role_id": evaluator_role_id,
        },
        "required": required_rubric,
        "verifier_ref": None,
    }
    if panel_size is not None:
        rubric_criterion["pass_rule"]["panel_size"] = panel_size
    task["evaluation_criteria"].append(rubric_criterion)

    # Ajusta papéis aceitos no catálogo de destinos + perfis de assinatura
    profiles = {}
    targets = []
    reviewer_roles = [
        task["agent_role"],
        {"id": "code_reviewer", "version": "0.1", "content_digest": canonical_digest("code_reviewer")},
        {"id": "security_reviewer", "version": "0.1", "content_digest": canonical_digest("security_reviewer")},
    ]
    model_ids = {
        "codex": "gpt-5-codex",
        "claude": "claude-opus-4",
        "gemini": "gemini-2.5-pro",
        "kiro": kiro_model,
    }
    base_target = deepcopy(data["catalog"]["targets"][0])
    # Ordem de prioridade escolhe author_family para a tentativa produtiva
    priority_map = {f"family_{n}": (0 if f"family_{n}" == author_family else idx + 1)
                    for idx, n in enumerate(("codex", "claude", "gemini", "kiro"))}
    for family_short, cli in (("codex", "codex"), ("claude", "claude"), ("gemini", "agy"), ("kiro", "kiro-cli")):
        fam_id = f"family_{family_short}"
        prof = load_fixture("valid/subscription-profile.json")
        prof.update(
            subscription_id=f"sub-{family_short}",
            family_id=fam_id,
            quota_pool_id=f"pool-{family_short}",
            cli_executable=cli,
            detector_version=VERSIONS[cli],
        )
        prof = seal_record(prof, "subscription_profile")
        profiles[prof["subscription_id"]] = prof

        t = deepcopy(base_target)
        t.update(
            target_id=f"target-{family_short}",
            priority=priority_map[fam_id],
            family_id=fam_id,
            subscription_id=prof["subscription_id"],
            quota_pool_id=prof["quota_pool_id"],
            model_ref={"id": model_ids[family_short], "version": "1", "content_digest": canonical_digest(model_ids[family_short])},
            provider_ref={"id": f"provider-{family_short}", "version": "1", "content_digest": canonical_digest(family_short)},
            supported_role_refs=reviewer_roles,
            supported_task_types=["analysis", "generation", "review", "test", "documentation"],
        )
        targets.append(t)

    data["catalog"]["targets"] = targets
    data["catalog"]["catalog_hash"] = canonical_digest({k: v for k, v in data["catalog"].items() if k != "catalog_hash"})
    if calibration_mode:
        data["policy"]["experiment_metadata"] = {"calibration_mode": True}

    doc = data["document"]
    doc["reconcile_not_found_authoritative"] = authoritative_not_found
    doc["default"]["rubric_evaluations"] = {"rubric-sec": rubric_eval_script}
    if attempt_override is not None:
        doc["attempts"] = {"T1": {"1": deepcopy(attempt_override), "2": deepcopy(attempt_override)}}
    else:
        doc["attempts"] = {}

    rt = FamilyRuntime(
        profiles,
        lambda r: SubscriptionSimExecutor(r),
        memory_config=resolve_memory_config({}),
        probe=probe,
    )
    runner_kwargs = {
        "family_runtime": rt,
        "evaluator_routing_policy": eval_policy,
        "rubric_specs": [rubric_obj],
        "evaluator_profiles": profiles,
        "eval_temp_root": tmp_path / "eval_tmp",
    }
    return data, runner_kwargs, rubric_obj, eval_policy, profiles


def test_R01_deterministic_stage1_short_circuits_model_rubric(tmp_path: Path) -> None:
    data, kw, *_ = _setup_phase8_bundle(
        tmp_path,
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}},
        attempt_override={"verifier": {"behavior-ok": "fail"}},
    )
    with make_runner(tmp_path / "store", data, **kw) as runner:
        proj = runner.run()
        assert proj.state == "failed"
        assert not events(runner, "evaluator_dispatch_requested")
        assert not [e for e in events(runner, "quota_consumption_recorded") if e["aggregate_type"] == "evaluation"]
        reports = [e["payload"] for e in events(runner, "evaluation_report_recorded") if e["payload"]["scope"]["kind"] == "task_attempt"]
        assert reports and reports[0]["verdict"] == "fail"
        rubric_res = next(r for r in reports[0]["criterion_results"] if r["criterion_id"] == "rubric-sec")
        assert rubric_res["status"] == "not_run"
        assert "skipped_due_to_deterministic_gate" in rubric_res["observations"]
        assert any(e["payload"]["reason"] == "deterministic_only_evaluation" for e in events(runner, "budget_released"))
        assert_reservations_closed(runner)


def test_R02_subject_bytes_corrupted_triggers_integrity_mismatch_before_stage2(tmp_path: Path) -> None:
    data, kw, *_ = _setup_phase8_bundle(
        tmp_path,
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}},
    )

    def corrupt_before_eval(runner, event):
        if event["event_type"] == "state_transition" and event["aggregate_type"] == "task" and event["payload"]["to"] == "evaluating":
            att = runner.projection.attempts[runner.projection.tasks["T1"].attempt_ids[-1]]
            digest = att.output_bindings[0]["artifact"]["content_digest"]["value"]
            blob = runner.journal.directory / "artifacts" / "sha256" / digest
            blob.write_bytes(b"corrupted-bytes")

    with make_runner(tmp_path / "store", data, on_event=corrupt_before_eval, **kw) as runner:
        proj = runner.run()
        assert proj.state == "failed"
        assert not events(runner, "evaluator_dispatch_requested")
        eval_errors = [e["payload"] for e in events(runner, "error_attached", "evaluation")]
        assert any(e["code"] == "integrity_mismatch" for e in eval_errors)
        report = [e["payload"] for e in events(runner, "evaluation_report_recorded") if e["payload"]["scope"]["kind"] == "task_attempt"][0]
        assert report["verdict"] == "fail"
        rubric_res = next(r for r in report["criterion_results"] if r["criterion_id"] == "rubric-sec")
        assert rubric_res["status"] == "not_run"
        assert "skipped_due_to_integrity_mismatch" in rubric_res["observations"]
        assert not events(runner, "outputs_accepted")


def test_R03_rule5_precedence_and_workflow_scope_rejected(tmp_path: Path) -> None:
    data, kw, rubric, *_ = _setup_phase8_bundle(
        tmp_path,
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}},
    )
    bad_workflow = deepcopy(data["workflow"])
    bad_workflow["evaluation_criteria"] = [{
        "criterion_id": "wf-rubric",
        "workflow_requirement_ids": ["WREQ1"],
        "output_ids": [bad_workflow["outputs"][0]["output_id"]],
        "method": "model_rubric",
        "pass_rule": {
            "output_ids": [bad_workflow["outputs"][0]["output_id"]],
            "rubric_ref": {"id": rubric["rubric_id"], "version": rubric["version"], "content_digest": rubric["record_hash"]},
            "prompt_template_ref": {"id": "p", "version": "0.1", "content_digest": canonical_digest("p")},
        },
        "required": True,
        "verifier_ref": None,
    }]
    issues = validate_workflow(bad_workflow)
    assert any(i.code == "workflow_scope_model_rubric_not_supported" for i in issues)

    # Regra 5 (determinístico obrigatório fail + model_rubric pass -> fail)
    rep = load_fixture("valid/evaluation-report-rubric.json")
    rep["criterion_results"][0]["status"] = "fail"
    rep["verdict"] = "inconclusive"
    rep = _seal(rep)
    assert any(i.code == "semantic.required_criterion_failed_verdict_mismatch" for i in validate_instance(rep, "evaluation_report"))


def test_R04_R05_optional_rubric_and_blocking_dimension(tmp_path: Path) -> None:
    # R04: critério model_rubric opcional reprova -> tarefa passa (outputs_accepted)
    data_opt, kw_opt, *_ = _setup_phase8_bundle(
        tmp_path / "opt",
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 0, "contract-completeness": 0}},
        required_rubric=False,
    )
    with make_runner(tmp_path / "store_opt", data_opt, **kw_opt) as runner:
        proj = runner.run()
        assert proj.state == "succeeded"
        assert events(runner, "outputs_accepted", "task", "T1")

    # R05: 2 (< mandatory min 3) e 4 -> normalized 0.7500 >= 0.75, mas dimensão bloqueante força fail
    data_blk, kw_blk, *_ = _setup_phase8_bundle(
        tmp_path / "blk",
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 2, "contract-completeness": 4}},
        required_rubric=True,
    )
    with make_runner(tmp_path / "store_blk", data_blk, **kw_blk) as runner:
        proj = runner.run()
        assert proj.state == "failed"
        rep = [e["payload"] for e in events(runner, "evaluation_report_recorded") if e["payload"]["scope"]["kind"] == "task_attempt"][0]
        assert rep["verdict"] == "fail"
        assert not events(runner, "outputs_accepted", "task", "T1")


def test_R06_R10_R12_R13_cross_family_vendor_reproducibility_and_cost_unavailable(tmp_path: Path) -> None:
    # R06(a), R10, R12, R13: codex -> gemini; reprodutível byte a byte em dois diretórios
    digests = []
    for run_idx in (1, 2):
        sub = tmp_path / f"run_{run_idx}"
        data, kw, *_ = _setup_phase8_bundle(
            sub,
            rubric_eval_script={
                "raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 3},
                "resolved_model_id": "gemini-2.5-pro",
                "usage": {"input_tokens": 110, "output_tokens": 42, "total_cost_usd": "0.99"},
            },
            author_family="family_codex",
        )
        with make_runner(sub / "store", data, **kw) as runner:
            proj = runner.run()
            assert proj.state == "succeeded"
            disp = events(runner, "evaluator_dispatch_requested")[0]
            assert disp["payload"]["evaluator_family_id"] == "family_gemini"
            assert disp["payload"]["evaluator_model_vendor"] == "google"

            # R13: artifact_ingested (RubricEvaluationEvidence) precede evaluation_report_recorded
            ev_ingested = [e for e in events(runner, "artifact_ingested", "evaluation")
                           if e["payload"]["artifact_type"] == "rubric-evaluation-evidence"][0]
            rep_ev = [e for e in events(runner, "evaluation_report_recorded")
                      if e["payload"]["scope"]["kind"] == "task_attempt"][0]
            assert ev_ingested["global_sequence"] < rep_ev["global_sequence"]
            assert runner.projection.manifest["evaluator_snapshot"] == rep_ev["payload"]["evaluator_snapshot"]
            assert rep_ev["payload"]["evaluator_snapshot"]["kind"] == "composite_rubric"

            # R10: 1 UsageRecord unavailable/null + 1 QuotaConsumptionRecord + budget_settled
            eval_usages = [e["payload"] for e in events(runner, "usage_attached", "evaluation")]
            eval_cons = [e["payload"] for e in events(runner, "quota_consumption_recorded", "evaluation")]
            assert len(eval_usages) == 1 and len(eval_cons) == 1
            assert eval_usages[0]["cost_lines"][0]["amount"] is None
            assert eval_usages[0]["cost_lines"][0]["status"] == "unavailable"
            assert events(runner, "budget_settled")
            assert proj.published_summary["cost_summary"]["unavailable_count"] >= 2
            assert proj.published_summary["cost_summary"]["total_cost_exact"] is False
            digests.append((
                disp["payload"]["rendered_prompt_digest"],
                rep_ev["payload"]["record_hash"],
                runner.journal.final_digest,
            ))

    # R12: mesmos prompt digest, report digest e final event_hash byte a byte
    assert digests[0] == digests[1]

    # R06(b): autor family_claude (anthropic) com family_kiro usando claude-sonnet-4 (anthropic)
    # -> exclui claude E kiro, escolhe gemini
    data_b, kw_b, *_ = _setup_phase8_bundle(
        tmp_path / "r06b",
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}},
        author_family="family_claude",
        kiro_model="claude-sonnet-4",
    )
    with make_runner(tmp_path / "r06b_store", data_b, **kw_b) as runner:
        assert runner.run().state == "succeeded"
        assert events(runner, "evaluator_dispatch_requested")[0]["payload"]["evaluator_family_id"] == "family_gemini"


def test_R07_mutation_and_sandbox_escape_trigger_reevaluation(tmp_path: Path) -> None:
    for bad_field, expected_err in (
        ("workspace_mutated", "eval_workspace_mutated"),
        ("sandbox_escape_path", "eval_sandbox_escape_attempt"),
    ):
        case_dir = tmp_path / bad_field
        step1 = {bad_field: True if bad_field == "workspace_mutated" else "/home/user/.claude/credentials.json"}
        step2 = {"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}}
        data, kw, *_ = _setup_phase8_bundle(case_dir, rubric_eval_script=[step1, step2])
        with make_runner(case_dir / "store", data, **kw) as runner:
            proj = runner.run()
            assert proj.state == "succeeded"
            assert ("evaluating", "waiting_evaluation") in edges(runner, "task", "T1")
            assert ("waiting_evaluation", "evaluating") in edges(runner, "task", "T1")
            assert any(e["payload"]["code"] == expected_err for e in events(runner, "error_attached", "evaluation"))


def test_R08_quota_limit_on_first_evaluator_switches_family_in_tk22(tmp_path: Path) -> None:
    step1 = {
        "quota_exhausted": {
            "excerpt": "RESOURCE_EXHAUSTED: quota exceeded",
            "blocked_until": "2026-11-18T05:00:00Z",
            "reset_source": "cli_reported",
        }
    }
    step2 = {
        "raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4},
        "resolved_model_id": "claude-sonnet-4",
    }
    data, kw, *_ = _setup_phase8_bundle(tmp_path, rubric_eval_script=[step1, step2], author_family="family_codex")
    with make_runner(tmp_path / "store", data, **kw) as runner:
        proj = runner.run()
        assert proj.state == "succeeded"
        assert len(proj.tasks["T1"].attempt_ids) == 1  # Sem repetir tentativa produtiva!
        dispatches = [e["payload"] for e in events(runner, "evaluator_dispatch_requested")]
        assert [d["evaluator_family_id"] for d in dispatches] == ["family_gemini", "family_kiro"]
        q_state = events(runner, "quota_exhausted_recorded")[0]["payload"]
        assert q_state["source_attempt_id"] is None
        assert q_state["source_evaluation_id"] == "evaluation-T1-1-1"
        assert any(e["payload"]["code"] == "quota_limit_reached" for e in events(runner, "error_attached", "evaluation"))


def test_R09_all_evaluators_blocked_waits_for_blocked_until_or_fails_tk20(tmp_path: Path) -> None:
    # (a) Todos bloqueados via cooldown com blocked_until <= run_deadline_at:
    # aguarda min_blocked_until, sonda + quota_reset_observed e aprova via TK22 sem autoavaliação
    policy_two_only = _seal({
        **default_evaluator_routing_policy(),
        "fallback_evaluator_families": [],
    })
    probe_calls = []

    def probe_ok(target, req):
        probe_calls.append(target["family_id"])
        return {"available": True}

    def inject_blocks(start_iso: str, kiro_until: str, gemini_until: str, runner_box: dict):
        pass

    data_wait, kw_wait, *_ = _setup_phase8_bundle(
        tmp_path / "wait",
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}},
        routing_policy=policy_two_only,
        author_family="family_codex",
        max_run_duration_ms=20_000_000,
        probe=probe_ok,
    )

    injected = {"done": False}

    def inject_cooldown(runner, event):
        if not injected["done"] and event["event_type"] == "state_transition" and event["aggregate_type"] == "task" and event["payload"]["to"] == "evaluating":
            injected["done"] = True
            observed = runner.clock.now()
            for fam, pool, sub, ms in (
                ("family_gemini", "pool-gemini", "sub-gemini", 3_600_000),
                ("family_kiro", "pool-kiro", "sub-kiro", 1_800_000),
            ):
                until = format_utc(parse_utc(observed) + timedelta(milliseconds=ms))
                runner.append("quota_exhausted_recorded", seal_record({
                    "schema_version": "0.1",
                    "quota_event_id": f"qe-pre-{fam}",
                    "run_id": runner.journal.run_id,
                    "source_attempt_id": runner.projection.tasks["T1"].attempt_ids[0],
                    "subscription_id": sub,
                    "family_id": fam,
                    "quota_pool_id": pool,
                    "status": "cooldown",
                    "observed_at": observed,
                    "reset_at": None,
                    "blocked_until": until,
                    "reset_source": "cooldown_policy",
                    "default_cooldown_ms": ms,
                    "raw_message_excerpt": "cooldown ativo",
                }, "quota_state_record"))

    with make_runner(tmp_path / "store_wait", data_wait, on_event=inject_cooldown, **kw_wait) as runner:
        proj = runner.run()
        assert proj.state == "succeeded"
        assert ("evaluating", "waiting_evaluation") in edges(runner, "task", "T1")
        assert ("waiting_evaluation", "evaluating") in edges(runner, "task", "T1")
        assert any(e["payload"]["code"] == "no_eligible_cross_family_evaluator" for e in events(runner, "error_attached", "evaluation"))
        assert events(runner, "quota_reset_observed")
        # Nunca relaxou para autoavaliação por family_codex!
        assert events(runner, "evaluator_dispatch_requested")[0]["payload"]["evaluator_family_id"] == "family_kiro"
        assert "family_kiro" in probe_calls

    # (b) blocked_until além do run_deadline_at -> TK20 direto sem aguardar em vão
    data_exp, kw_exp, *_ = _setup_phase8_bundle(
        tmp_path / "exp",
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}},
        routing_policy=policy_two_only,
        author_family="family_codex",
        max_run_duration_ms=600_000,  # deadline muito antes de +5h
    )
    injected_exp = {"done": False}

    def inject_long_block(runner, event):
        if not injected_exp["done"] and event["event_type"] == "state_transition" and event["aggregate_type"] == "task" and event["payload"]["to"] == "evaluating":
            injected_exp["done"] = True
            observed = runner.clock.now()
            long_until = format_utc(parse_utc(observed) + timedelta(hours=5))
            for fam, pool, sub in (("family_gemini", "pool-gemini", "sub-gemini"), ("family_kiro", "pool-kiro", "sub-kiro")):
                runner.append("quota_exhausted_recorded", seal_record({
                    "schema_version": "0.1",
                    "quota_event_id": f"qe-long-{fam}",
                    "run_id": runner.journal.run_id,
                    "source_attempt_id": runner.projection.tasks["T1"].attempt_ids[0],
                    "subscription_id": sub,
                    "family_id": fam,
                    "quota_pool_id": pool,
                    "status": "quota_exhausted",
                    "observed_at": observed,
                    "reset_at": long_until,
                    "blocked_until": long_until,
                    "reset_source": "cli_reported",
                    "raw_message_excerpt": "limit until tomorrow",
                }, "quota_state_record"))

    with make_runner(tmp_path / "store_exp", data_exp, on_event=inject_long_block, **kw_exp) as runner:
        proj = runner.run()
        assert proj.state == "failed"
        assert ("evaluating", "waiting_evaluation") not in edges(runner, "task", "T1")
        assert ("evaluating", "failed") in edges(runner, "task", "T1")
        assert not events(runner, "evaluator_dispatch_requested")


def test_R14_R15_invalid_citations_force_inconclusive_in_runner(tmp_path: Path) -> None:
    for mode in ("nonexistent_excerpt", "wrong_digest", "bad_json_pointer"):
        sub = tmp_path / mode
        step = {
            "raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4},
            "citation_mode": mode,
        }
        data, kw, *_ = _setup_phase8_bundle(sub, rubric_eval_script=step, max_evaluations_per_attempt=1)
        with make_runner(sub / "store", data, **kw) as runner:
            proj = runner.run()
            assert proj.state == "failed"
            assert any(e["payload"]["code"] == "evidence_citation_invalid" for e in events(runner, "error_attached", "evaluation"))
            rep = [e["payload"] for e in events(runner, "evaluation_report_recorded") if e["payload"]["scope"]["kind"] == "task_attempt"][0]
            assert rep["verdict"] == "inconclusive"
            assert not events(runner, "outputs_accepted")


def test_R16_normative_context_under_evaluator_rls_offline(tmp_path: Path) -> None:
    data, kw, *_ = _setup_phase8_bundle(
        tmp_path,
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}},
        author_family="family_codex",
        knowledge_req_ids=["K_NORM"],
        evaluator_role_id="security_reviewer",
    )
    with make_runner(tmp_path / "store", data, **kw) as runner:
        proj = runner.run()
        assert proj.state == "succeeded"
        rep = [e["payload"] for e in events(runner, "evaluation_report_recorded") if e["payload"]["scope"]["kind"] == "task_attempt"][0]
        assert rep["context_manifest_ref"] is not None
        desc = next(a for a in runner.projection.artifacts.values() if a["artifact_type"] == "context-manifest")
        manifest = json.loads(runner.store.read(desc["locator"]).decode("utf-8"))
        sources = {item["knowledge_provenance"]["unit_ref"]["source_id"] for item in manifest["items"]}
        assert "codex-only" not in sources
        assert "common-project" in sources or "gemini-only" in sources or "global-ops" in sources


@pytest.mark.skipif(not os.environ.get("ORQ_MEMORY_DSN"), reason="ORQ_MEMORY_DSN ausente")
def test_R16_normative_context_under_postgres_rls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    psycopg = pytest.importorskip("psycopg")
    from orq_contracts.memory_store.postgres import apply_schema, connect, sync_catalog

    monkeypatch.delenv("ORQ_KNOWLEDGE_BACKEND", raising=False)
    data, kw, *_ = _setup_phase8_bundle(
        tmp_path,
        rubric_eval_script={"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}},
        author_family="family_codex",
        knowledge_req_ids=["K_NORM"],
        evaluator_role_id="security_reviewer",
        catalog_id="cat-phase8-pg-" + uuid.uuid4().hex[:8],
    )
    cfg = resolve_memory_config()
    with connect(cfg) as admin:
        apply_schema(admin)
        sync_catalog(admin, build_sync_plan(data["vault_root"], data["knowledge_catalog"]))
    try:
        kw["family_runtime"].memory_config = cfg
        with make_runner(tmp_path / "store", data, **kw) as runner:
            proj = runner.run()
            assert proj.state == "succeeded"
            rep = [e["payload"] for e in events(runner, "evaluation_report_recorded") if e["payload"]["scope"]["kind"] == "task_attempt"][0]
            assert rep["context_manifest_ref"] is not None
            desc = next(a for a in runner.projection.artifacts.values() if a["artifact_type"] == "context-manifest")
            manifest = json.loads(runner.store.read(desc["locator"]).decode("utf-8"))
            sources = {item["knowledge_provenance"]["unit_ref"]["source_id"] for item in manifest["items"]}
            assert "codex-only" not in sources
    finally:
        with connect(cfg) as admin:
            with admin.transaction():
                for table in ("knowledge_units", "knowledge_sources", "knowledge_catalogs"):
                    admin.execute(f"DELETE FROM orq_memory.{table} WHERE catalog_id = %s", (data["knowledge_catalog"]["catalog_id"],))


def test_R17_R18_inconclusive_band_vs_valid_fail(tmp_path: Path) -> None:
    # R17: 0.7000 na banda [0.65, 0.75) por 2x -> TK19 -> TK22 -> TK20
    rubric = _rubric_spec(inconclusive_band={"lower": "0.65", "upper": "0.75"})
    rubric["dimensions"][0]["weight"] = "0.60"
    rubric["dimensions"][1]["weight"] = "0.40"
    rubric = _seal(rubric)
    # 3/4 * 0.60 + 2/4 * 0.40 = 0.6500 inside [0.65, 0.75)
    inc_step = {"raw_scores": {"auth-and-secrets-boundary": 3, "contract-completeness": 2}}
    data_17, kw_17, *_ = _setup_phase8_bundle(tmp_path / "r17", rubric_eval_script=[inc_step, inc_step], rubric=rubric)
    with make_runner(tmp_path / "store17", data_17, **kw_17) as runner:
        proj = runner.run()
        assert proj.state == "failed"
        assert ("evaluating", "waiting_evaluation") in edges(runner, "task", "T1")
        assert ("waiting_evaluation", "evaluating") in edges(runner, "task", "T1")
        assert ("evaluating", "failed") in edges(runner, "task", "T1")

    # R18: fail válido (0.0000 < 0.65) -> direto retry_wait/failed sem waiting_evaluation
    fail_step = {"raw_scores": {"auth-and-secrets-boundary": 0, "contract-completeness": 0}}
    data_18, kw_18, *_ = _setup_phase8_bundle(tmp_path / "r18", rubric_eval_script=fail_step)
    with make_runner(tmp_path / "store18", data_18, **kw_18) as runner:
        proj = runner.run()
        assert proj.state == "failed"
        assert ("evaluating", "waiting_evaluation") not in edges(runner, "task", "T1")
        assert ("evaluating", "retry_wait") in edges(runner, "task", "T1")


def test_R19_panel_disagreement_tiebreaker_a_and_b(tmp_path: Path) -> None:
    panel_step = {
        "judges": [
            {"evaluator_family_id": "family_gemini", "raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}},
            {"evaluator_family_id": "family_kiro", "raw_scores": {"auth-and-secrets-boundary": 0, "contract-completeness": 0}},
        ]
    }
    # (a) Sem 3ª família elegível -> no_third_family_tiebreaker_available -> TK20 direto
    policy_no_tie = _seal({**default_evaluator_routing_policy(), "panel_size": 2, "fallback_evaluator_families": []})
    data_a, kw_a, *_ = _setup_phase8_bundle(
        tmp_path / "r19a", rubric_eval_script=panel_step, routing_policy=policy_no_tie, author_family="family_codex", panel_size=2
    )
    with make_runner(tmp_path / "store19a", data_a, **kw_a) as runner:
        proj = runner.run()
        assert proj.state == "failed"
        assert len(events(runner, "evaluation_report_recorded")) == 1
        assert len(events(runner, "usage_attached", "evaluation")) == 2
        assert len(events(runner, "quota_consumption_recorded", "evaluation")) == 2
        assert any(e["payload"]["code"] == "evaluator_panel_disagreement" for e in events(runner, "error_attached", "evaluation"))
        assert ("evaluating", "waiting_evaluation") not in edges(runner, "task", "T1")

    # (b) Com 3ª família elegível (family_claude) -> TK19 -> TK22 desempata em panel_size=1 com family_claude
    policy_with_tie = _seal({**default_evaluator_routing_policy(), "panel_size": 2, "fallback_evaluator_families": ["family_claude"]})
    tie_step = {"evaluator_family_id": "family_claude", "raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}}
    data_b, kw_b, *_ = _setup_phase8_bundle(
        tmp_path / "r19b", rubric_eval_script=[panel_step, tie_step], routing_policy=policy_with_tie, author_family="family_codex", panel_size=2
    )
    with make_runner(tmp_path / "store19b", data_b, **kw_b) as runner:
        proj = runner.run()
        assert proj.state == "succeeded"
        dispatches = [e["payload"] for e in events(runner, "evaluator_dispatch_requested")]
        assert [d["evaluator_family_id"] for d in dispatches] == ["family_gemini", "family_kiro", "family_claude"]


def test_R20_confirmed_judge_timeout_and_reeval(tmp_path: Path) -> None:
    step1 = {"execution": "hang", "on_cancel": "confirmed", "reconcile": "found_stopped"}
    step2 = {"raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4}}
    data, kw, *_ = _setup_phase8_bundle(tmp_path, rubric_eval_script=[step1, step2])
    with make_runner(tmp_path / "store", data, **kw) as runner:
        proj = runner.run()
        assert proj.state == "succeeded"
        terms = [e["payload"] for e in events(runner, "evaluator_termination_confirmed")]
        assert terms[0]["cause"] == "timeout"
        rep1 = [e["payload"] for e in events(runner, "evaluation_report_recorded") if e["payload"]["scope"]["kind"] == "task_attempt"][0]
        assert rep1["execution_status"] == "timed_out" and rep1["verdict"] == "not_run"
        assert any(e["payload"]["code"] == "timeout" for e in events(runner, "error_attached", "evaluation"))


def test_R21_judge_unknown_termination_crash_recovery_tk21_and_tk15(tmp_path: Path) -> None:
    # (a) Crash logo após evaluator_dispatch_requested com término encontrado na reconciliação (TK21 -> TK11 -> succeeded)
    step_rec = {
        "execution": "hang",
        "on_cancel": "unconfirmed",
        "reconcile": "found_finished",
        "raw_scores": {"auth-and-secrets-boundary": 4, "contract-completeness": 4},
    }
    data_ok, kw_ok, *_ = _setup_phase8_bundle(tmp_path / "rec_ok", rubric_eval_script=step_rec)

    def crash_on_judge_dispatch(runner, event):
        if event["event_type"] == "evaluator_dispatch_acknowledged":
            raise InjectedCrash("crash durante execução externa de juiz")

    with pytest.raises(InjectedCrash):
        with make_runner(tmp_path / "store_ok", data_ok, on_event=crash_on_judge_dispatch, **kw_ok) as r1:
            r1.run()

    with make_runner(tmp_path / "store_ok", data_ok, resume=True, **kw_ok) as r2:
        proj = r2.run()
        assert proj.state == "succeeded"
        assert ("evaluating", "reconciling") in edges(r2, "task", "T1")
        assert ("reconciling", "evaluating") in edges(r2, "task", "T1")
        assert len(events(r2, "evaluator_dispatch_requested")) == 1  # Nunca redespacha!
        assert events(r2, "evaluator_reconciliation_requested")
        assert events(r2, "evaluator_reconciliation_observed")[-1]["payload"]["outcome"] == "found_finished"

    # (b) Reconciliação expira com outcome unknown -> TK15 -> RN10 indeterminate sem EvaluationReport falso
    step_ind = {"execution": "hang", "on_cancel": "unconfirmed", "reconcile": "unknown"}
    data_ind, kw_ind, *_ = _setup_phase8_bundle(tmp_path / "rec_ind", rubric_eval_script=step_ind)
    with make_runner(tmp_path / "store_ind", data_ind, **kw_ind) as r_ind:
        proj_ind = r_ind.run()
        assert proj_ind.state == "indeterminate"
        assert proj_ind.tasks["T1"].state == "indeterminate"
        assert not [e for e in events(r_ind, "evaluation_report_recorded") if e["payload"]["scope"]["kind"] == "task_attempt"]


def test_orq_run_simulate_reference_digest_unchanged(tmp_path: Path, capsys) -> None:
    fixtures = ROOT / "fixtures/v0.1"
    args = [
        "run", "simulate",
        "--store-root", str(tmp_path),
        "--workflow", str(fixtures / "valid/workflow-reference.json"),
        "--request", str(fixtures / "run-request.json"),
        "--catalog", str(fixtures / "run-catalog.json"),
        "--policy", str(fixtures / "run-policy.json"),
        "--script", str(fixtures / "valid/simulation-script-reference.json"),
        "--knowledge-catalog", str(fixtures / "vault/catalog-snapshot.json"),
        "--vault-root", str(fixtures / "vault"),
    ]
    assert cli_main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state"] == "succeeded"
    assert out["journal_digest"]["value"] == EXPECTED_SIMULATE_DIGEST
