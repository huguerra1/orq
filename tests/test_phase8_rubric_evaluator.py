"""Gate 8C: RubricModelEvaluator sobre as 4 CLIs em modo somente leitura."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

from orq_contracts.adapters import AgyAdapter, ClaudeCodeAdapter, CLIPermissions, CodexAdapter, KiroAdapter
from orq_contracts.adapters.cli import _read_state
from orq_contracts.adapters.quota import VERSIONS
from orq_contracts.artifact_store import ArtifactStore
from orq_contracts.canonical import canonical_digest
from orq_contracts.evaluator import ContentContractRegistry
from orq_contracts.executor import ExecutorError, bytes_digest
from orq_contracts.journal import RunJournal
from orq_contracts.rubric_evaluator import RubricModelEvaluator, audit_judge_tool_trail, verify_rubric_citations
from orq_contracts.rubric_prep import JudgeRawOutcome
from orq_contracts.simulated_executor import DeterministicClock
from orq_contracts.subscription_hub import SubscriptionHub
from orq_contracts.validator import validate_instance, validate_quota_consumption_record


ADAPTERS = [ClaudeCodeAdapter, CodexAdapter, AgyAdapter, KiroAdapter]
NOW = "2026-11-18T02:00:00Z"
DEADLINE = "2026-11-18T02:02:00Z"
FAKE = Path(__file__).with_name("fake_subscription_cli.py").absolute()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/v0.1"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / "valid" / f"{name}.json").read_text(encoding="utf-8"))


def _seal(record: dict) -> dict:
    body = {k: v for k, v in record.items() if k != "record_hash"}
    body["record_hash"] = canonical_digest(body)
    return body


def _make_target(cli: str) -> tuple[dict, dict, str]:
    family = {"claude": "claude", "codex": "codex", "agy": "gemini", "kiro-cli": "kiro"}[cli]
    model_id = {
        "claude": "claude-opus-4",
        "codex": "gpt-5-codex",
        "agy": "gemini-2.5-pro",
        "kiro-cli": "claude-sonnet-4",
    }[cli]
    profile = _seal({
        "schema_version": "0.1",
        "subscription_id": f"sub-{family}",
        "family_id": f"family_{family}",
        "cli_executable": cli,
        "quota_pool_id": f"pool-{family}",
        "billing_mode": "personal_subscription",
        "default_cooldown_ms": 3600000,
        "local_timezone": "America/Sao_Paulo",
        "detector_version": VERSIONS[cli],
    })
    target = {
        "target_id": f"target-{family}",
        "version": "1",
        "status": "active",
        "family_id": f"family_{family}",
        "subscription_id": profile["subscription_id"],
        "quota_pool_id": profile["quota_pool_id"],
        "model_ref": {"id": model_id, "version": "1", "content_digest": canonical_digest(model_id)},
        "provider_ref": {"id": f"provider-{family}", "version": "1", "content_digest": canonical_digest(family)},
        "runtime_ref": {"id": f"runtime-{cli}", "version": "1", "content_digest": canonical_digest(cli)},
        "executor_ref": {"id": f"executor-{cli}", "version": "1", "content_digest": canonical_digest(["executor", cli, "0.1", "test-1"])},
    }
    return profile, target, model_id


@pytest.fixture
def env_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    journals: list[RunJournal] = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("rede proibida"))

    def create(adapter_class=CodexAdapter, mode: str = "judge_pass", *, eval_permissions=CLIPermissions(read_only=True, allow_commands=False)):
        idx = len(journals)
        root = tmp_path / f"case-{idx}"
        root.mkdir()
        store_root = root / "store"
        clock = DeterministicClock(NOW, 100)
        journal = RunJournal.create(store_root / "run-demo-008", "run-demo-008", clock=clock)
        journals.append(journal)
        profile, target, _ = _make_target(adapter_class.cli)
        adapter = adapter_class(
            profile,
            ArtifactStore(journal),
            state_directory=root / "state",
            workspace_root=root / "attempt-work",
            clock=clock,
            runtime_version="test-1",
            contracts=ContentContractRegistry(),
            permissions=eval_permissions,
            platform="linux",
        )
        # Atualiza executor_ref do target para casar exatamente com describe()
        target["executor_ref"] = adapter.describe().executor_ref
        telemetry_dir = root / "telemetry"
        telemetry_dir.mkdir()
        invocation_log = telemetry_dir / "invocation.json"
        original_argv = adapter._argv
        adapter._argv = lambda *args: [
            sys.executable,
            str(FAKE),
            adapter_class.cli,
            mode,
            f"--fake-invocation-out={invocation_log}",
            *original_argv(*args),
        ]
        return root, store_root, adapter, target, profile, invocation_log

    yield create
    for journal in journals:
        journal.close()


def _subject_artifact(crlf: bool = False) -> tuple[dict, bytes]:
    text = json.dumps({
        "summary": "Nenhuma credencial deve ser extraida das CLIs.",
        "invariants": ["reconciliation_timeout_ms"],
    }, indent=2, ensure_ascii=False)
    if crlf:
        text = text.replace("\n", "\r\n")
    raw = text.encode("utf-8")
    binding = {
        "output_id": "security_report",
        "artifact": {"artifact_id": "artifact-security_report", "content_digest": bytes_digest(raw)},
    }
    return binding, raw


def _run_judge(env_builder, cls, mode: str, *, eval_sub_id: int = 1):
    root, store_root, adapter, target, profile, inv_log = env_builder(cls, mode)
    rubric = _load("rubric-spec")
    binding, raw = _subject_artifact()
    evaluator = RubricModelEvaluator(clock=adapter.clock)
    judge_call_id = f"judge-evaluation-T3-1-1-{eval_sub_id}"
    outcome = evaluator.run_single_judge(
        adapter=adapter,
        run_id="run-demo-008",
        task_id="T3",
        attempt_id="attempt-T3-1",
        attempt_number=1,
        evaluation_id="evaluation-T3-1-1",
        evaluation_number=1,
        criterion_id="security-rubric-ok",
        judge_call_id=judge_call_id,
        judge_index=1,
        evaluator_target=target,
        profile=profile,
        rubric_spec=rubric,
        subject_artifacts=[(binding, raw)],
        author_family_ids=["family_claude" if cls is not ClaudeCodeAdapter else "family_codex"],
        author_model_vendor="anthropic" if cls is not ClaudeCodeAdapter else "openai",
        deadline_at=DEADLINE,
        evaluation_timeout_ms=10000,
        eval_temp_root=root / "isolated_eval",
        store_root=store_root,
    )
    return root, store_root, adapter, profile, outcome, inv_log


@pytest.mark.parametrize("cls", ADAPTERS)
def test_four_clis_read_only_judge_pass_and_unavailable_cost(env_builder, cls) -> None:
    root, store_root, adapter, profile, outcome, inv_log = _run_judge(env_builder, cls, "judge_pass")

    assert outcome.execution_status == "completed"
    assert outcome.criterion_status == "pass"
    assert outcome.errors == ()
    assert outcome.evidence is not None
    assert validate_instance(outcome.evidence, "rubric_evaluation_evidence") == []
    assert outcome.evidence["normalized_score"] == "0.8750"
    assert outcome.evidence["citation_verification_status"] == "verified"

    # Custo é sempre unavailable/null (inclusive quando Claude reporta total_cost_usd: 99.99)
    assert outcome.usage_record["scope_type"] == "evaluation"
    assert outcome.usage_record["scope_ref"] == "evaluation-T3-1-1"
    assert outcome.usage_record["cost_lines"] == [{
        "status": "unavailable",
        "amount": None,
        "currency": None,
        "pricing_ref": None,
        "method": "personal_subscription_unpriced",
    }]
    assert validate_instance(outcome.usage_record, "usage_record") == []

    # 1 QuotaConsumptionRecord no escopo evaluation ligado ao judge_call_id
    assert outcome.quota_consumption["scope_type"] == "evaluation"
    assert outcome.quota_consumption["evaluation_id"] == "evaluation-T3-1-1"
    assert outcome.quota_consumption["judge_call_id"] == "judge-evaluation-T3-1-1-1"
    assert outcome.quota_consumption["messages_consumed"] == 1
    assert outcome.quota_consumption["quota_outcome"] == "accepted"
    assert validate_quota_consumption_record(outcome.quota_consumption, usage_record=outcome.usage_record) == []

    # Auditoria da invocação do subprocesso
    invocation = json.loads(inv_log.read_text(encoding="utf-8"))
    assert invocation["stdin"] == ""
    assert not any(name.startswith("ORQ_") for name in invocation["environment_names"])
    assert not Path(invocation["cwd"]).resolve().is_relative_to(store_root.resolve())
    argv = invocation["argv"]
    if cls is CodexAdapter:
        assert "-s" in argv and argv[argv.index("-s") + 1] == "read-only"
        out_path = Path(argv[argv.index("-o") + 1])
        assert not out_path.resolve().is_relative_to(Path(invocation["cwd"]).resolve())
    elif cls is ClaudeCodeAdapter:
        assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "plan"
        denied = argv[argv.index("--disallowedTools") + 1].split(",")
        assert {"Bash", "Edit", "Write", "WebFetch", "WebSearch"} <= set(denied)
    elif cls is AgyAdapter:
        assert "--mode" in argv and argv[argv.index("--mode") + 1] == "plan"
        assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "text"
    elif cls is KiroAdapter:
        assert "--trust-tools=fs_read" in argv

    # Estado durável salvo no diretório do juiz em evaluations/<judge_call_id>/state.json
    judge_state_file = adapter.state_directory / "evaluations/judge-evaluation-T3-1-1-1/state.json"
    assert judge_state_file.is_file()
    assert _read_state(judge_state_file)["kind"] == "evaluation_judge"


@pytest.mark.parametrize("cls", ADAPTERS)
def test_four_clis_judge_failure_and_violation_modes(env_builder, cls) -> None:
    # 1. Falha bloqueante (R05) -> fail com evidência válida
    _, _, _, _, fail_out, _ = _run_judge(env_builder, cls, "judge_blocking_fail")
    assert fail_out.execution_status == "completed"
    assert fail_out.criterion_status == "fail"
    assert fail_out.evidence and fail_out.evidence["blocking_dimension_failed"] is True

    # 2. Citação inexistente (R14) -> inconclusive + evidence_citation_invalid
    _, _, _, _, cit_out, _ = _run_judge(env_builder, cls, "judge_invalid_citation")
    assert cit_out.execution_status == "completed"
    assert cit_out.criterion_status == "inconclusive"
    assert cit_out.errors and cit_out.errors[0]["code"] == "evidence_citation_invalid"
    assert cit_out.evidence and cit_out.evidence["citation_verification_status"] == "invalid_citation"
    assert cit_out.evidence["derived_status"] == "inconclusive"

    # 3. Saída livre sem ORQ_FINAL (R07) -> not_run + evaluator_invalid_output
    _, _, _, _, inv_out, _ = _run_judge(env_builder, cls, "judge_invalid_output")
    assert inv_out.execution_status == "failed"
    assert inv_out.criterion_status == "not_run"
    assert inv_out.evidence is None
    assert inv_out.errors and inv_out.errors[0]["code"] == "evaluator_invalid_output"

    # 4. Mutação de arquivo em eval_workspace -> not_run + eval_workspace_mutated
    _, _, _, _, mut_out, _ = _run_judge(env_builder, cls, "judge_mutate")
    assert mut_out.execution_status == "failed"
    assert mut_out.criterion_status == "not_run"
    assert mut_out.evidence is None
    assert mut_out.errors and mut_out.errors[0]["code"] == "eval_workspace_mutated"

    # 5. Leitura fora do workspace -> not_run + eval_sandbox_escape_attempt
    _, _, _, _, esc_out, _ = _run_judge(env_builder, cls, "judge_escape")
    assert esc_out.execution_status == "failed"
    assert esc_out.criterion_status == "not_run"
    assert esc_out.evidence is None
    assert esc_out.errors and esc_out.errors[0]["code"] == "eval_sandbox_escape_attempt"

    # 6. Limite de cota -> quota_limit_reached com source_evaluation_id
    _, _, _, _, q_out, _ = _run_judge(env_builder, cls, "judge_quota")
    assert q_out.execution_status == "failed"
    assert q_out.criterion_status == "not_run"
    assert q_out.quota_state is not None
    assert q_out.quota_state["source_attempt_id"] is None
    assert q_out.quota_state["source_evaluation_id"] == "evaluation-T3-1-1"
    assert validate_instance(q_out.quota_state, "quota_state_record") == []
    assert q_out.errors and q_out.errors[0]["code"] == "quota_limit_reached"
    assert q_out.quota_consumption["quota_outcome"] == "quota_exhausted_on_call"


def test_citation_verification_crlf_normalization_and_r15_edges() -> None:
    rubric = _load("rubric-spec")
    binding, raw_crlf = _subject_artifact(crlf=True)
    digest = binding["artifact"]["content_digest"]

    # Excerto em LF encontra conteúdo em CRLF e JSON pointer válido resolve
    assessments = [
        {
            "dimension_id": "auth-and-secrets-boundary",
            "raw_score": 4,
            "rationale": "Citação literal em arquivo com quebra de linha CRLF.",
            "citations": [{
                "output_id": "security_report",
                "artifact_id": "artifact-security_report",
                "content_digest": digest,
                "locator": {"kind": "line_range", "start_line": 1, "end_line": 3},
                "verbatim_excerpt": "Nenhuma credencial deve ser extraida das CLIs.",
            }],
        },
        {
            "dimension_id": "contract-completeness",
            "raw_score": 3,
            "rationale": "Citação com json_pointer RFC 6901 válido no artefato.",
            "citations": [{
                "output_id": "security_report",
                "artifact_id": "artifact-security_report",
                "content_digest": digest,
                "locator": {"kind": "json_pointer", "pointer": "/invariants/0"},
                "verbatim_excerpt": "reconciliation_timeout_ms",
            }],
        },
    ]
    res = verify_rubric_citations(rubric, assessments, [(binding, raw_crlf)])
    assert res.citation_verification_status == "verified"

    # R15: digest de outro artefato + json_pointer inexistente -> invalid_citation
    bad = copy.deepcopy(assessments)
    bad[0]["citations"][0]["content_digest"]["value"] = "0" * 64
    bad[1]["citations"][0]["locator"]["pointer"] = "/invariants/99"
    res_bad = verify_rubric_citations(rubric, bad, [(binding, raw_crlf)])
    assert res_bad.citation_verification_status == "invalid_citation"


def test_judge_reconciliation_after_reopen_and_subscription_hub(env_builder) -> None:
    root, store_root, adapter, target, profile, _ = env_builder(ClaudeCodeAdapter, "judge_pass")
    rubric = _load("rubric-spec")
    binding, raw = _subject_artifact()

    # Confere SubscriptionHub encaminhando chamadas de juiz
    fake_runner = SimpleNamespace(
        catalog={"targets": [target]},
        projection=SimpleNamespace(attempts={}, evaluations={}),
        journal=SimpleNamespace(events=[]),
    )
    hub = SubscriptionHub(fake_runner, {target["target_id"]: adapter})

    evaluator = RubricModelEvaluator(clock=adapter.clock)
    outcome = evaluator.run_single_judge(
        adapter=hub,
        run_id="run-demo-008",
        task_id="T3",
        attempt_id="attempt-T3-1",
        attempt_number=1,
        evaluation_id="evaluation-T3-1-1",
        evaluation_number=1,
        criterion_id="security-rubric-ok",
        judge_call_id="judge-hub-1",
        judge_index=1,
        evaluator_target=target,
        profile=profile,
        rubric_spec=rubric,
        subject_artifacts=[(binding, raw)],
        author_family_ids=["family_codex"],
        author_model_vendor="openai",
        deadline_at=DEADLINE,
        eval_temp_root=root / "isolated_eval",
        store_root=store_root,
    )
    assert outcome.criterion_status == "pass"

    # Reabre novo adaptador sobre o mesmo state_directory e reconcilia pela idempotency_key
    reopened = ClaudeCodeAdapter(
        adapter.profile,
        adapter.store,
        state_directory=adapter.state_directory,
        workspace_root=adapter.workspace_root,
        clock=adapter.clock,
        runtime_version=adapter.runtime_version,
        contracts=adapter.contracts,
        permissions=CLIPermissions(read_only=True, allow_commands=False),
        platform="linux",
    )
    rec = reopened.reconcile_judge("run-demo-008/judge-hub-1", "judge-session-claude")
    assert rec.outcome == "found_finished"
    assert rec.termination_certainty == "confirmed"
    with pytest.raises(ExecutorError, match="external_call_mismatch"):
        reopened.reconcile_judge("run-demo-008/judge-hub-1", "outra-sessao")


def test_tool_trail_auditor_allows_placeholder_ellipsis_and_blocks_store_root(tmp_path: Path) -> None:
    eval_ws = tmp_path / "eval_ws"
    eval_ws.mkdir()
    store_root = tmp_path / "store"
    store_root.mkdir()

    ok, reason = audit_judge_tool_trail(
        stdout_text="[tool] read_file ...\n[tool] read_file inputs/security_report.json\n",
        stderr_text="",
        tool_events=("[tool] read_file ...",),
        eval_workspace=eval_ws,
        store_root=store_root,
    )
    assert ok is True and reason is None

    escaped, reason2 = audit_judge_tool_trail(
        stdout_text="",
        stderr_text="",
        tool_events=(f"[tool] read_file {store_root / 'runs/r1/attempt.json'}",),
        eval_workspace=eval_ws,
        store_root=store_root,
    )
    assert escaped is False and reason2 is not None


def test_subscription_evaluation_cost_zero_rejected_r11() -> None:
    zero_usage = copy.deepcopy(_load("usage-record-evaluation"))
    zero_usage["cost_lines"][0] = {
        "status": "reported",
        "amount": "0.00",
        "currency": "USD",
        "pricing_ref": None,
        "method": "personal_subscription",
    }
    zero_usage["coverage"] = {"reported": ["cost"], "estimated": [], "unavailable": []}
    zero_usage = _seal(zero_usage)
    issues = validate_instance(zero_usage, "usage_record")
    assert any(issue.code == "invalid_subscription_cost_zero" for issue in issues)
