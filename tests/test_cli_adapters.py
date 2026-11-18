from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import load_fixture
from orq_contracts.adapters import AgyAdapter, ClaudeCodeAdapter, CodexAdapter, KiroAdapter, CLIPermissions
from orq_contracts.adapters.cli import _read_state, wsl_path
from orq_contracts.adapters import cli as cli_module, _supervisor as supervisor
from orq_contracts.adapters._supervisor import write_record, safe_environment
from orq_contracts.adapters.quota import VERSIONS
from orq_contracts.artifact_store import ArtifactStore
from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.evaluator import ContentContractRegistry
from orq_contracts.executor import ExecutorAdapter, ExecutorError, WorkspaceHandle, bytes_digest
from orq_contracts.journal import RunJournal
from orq_contracts.validator import validate_instance, validate_quota_consumption_record
from orq_contracts.simulated_executor import DeterministicClock


ADAPTERS = [ClaudeCodeAdapter, CodexAdapter, AgyAdapter, KiroAdapter]
NOW = "2026-11-15T00:30:00Z"  # 21:30 em São Paulo
FAKE = Path(__file__).with_name("fake_subscription_cli.py").absolute()


def seal(record):
    record["record_hash"] = canonical_digest({k: v for k, v in record.items() if k != "record_hash"})
    return record


@pytest.fixture
def build(tmp_path, monkeypatch):
    resources = []
    # Rede bloqueada também no processo pytest; as CLIs falsas não importam clientes de rede.
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("rede proibida"))

    def create(adapter_class=CodexAdapter, mode="success", timeout_ms=10000, permissions=CLIPermissions(), fake=True, clock=None,
               cooldown_ms=3600000):
        root = tmp_path / str(len(resources))
        root.mkdir()
        clock = clock if clock is not None else lambda: NOW
        journal = RunJournal.create(root / "runs", "run-demo-001", clock=clock)
        resources.append(journal)
        cli = adapter_class.cli
        family = {"claude": "claude", "codex": "codex", "agy": "gemini", "kiro-cli": "kiro"}[cli]
        profile = seal({"schema_version": "0.1", "subscription_id": "subscription-test", "family_id": "family_" + family,
                        "cli_executable": cli, "quota_pool_id": "pool-test", "billing_mode": "personal_subscription",
                        "default_cooldown_ms": cooldown_ms, "local_timezone": "America/Sao_Paulo", "detector_version": VERSIONS[cli]})
        schema = {"type": "object", "properties": {"ok": {"const": True}}, "required": ["ok"], "additionalProperties": False}
        reference = {"id": "cli-test-output", "version": "0.1", "content_digest": canonical_digest(schema)}
        registry = ContentContractRegistry()
        registry.register(reference, schema)
        adapter = adapter_class(profile, ArtifactStore(journal), state_directory=root / "state", workspace_root=root / "work",
                                clock=clock, runtime_version="test-1", contracts=registry, permissions=permissions,
                                platform="linux")
        if fake:
            original = adapter._argv
            adapter._argv = lambda *args: [sys.executable, str(FAKE), cli, mode, *original(*args)]
        bundle = canonicalize({"context": "tarefa de teste; $() e ` não são shell"})
        intent = load_fixture("valid/dispatch-intent.json")
        intent.update(bundle_hash=bytes_digest(bundle), executor_ref=adapter.describe().executor_ref,
                      attempt_timeout_ms=timeout_ms, created_at=NOW, deadline_at="2026-11-15T00:31:00Z")
        seal(intent)
        spec = {"output_id": "report", "artifact_type": "report", "description": "relatório JSON", "content_contract_ref": reference}
        workspace = WorkspaceHandle("workspace-test", intent["workspace_base_ref"], (spec,), "requested-model")
        return adapter, intent, bundle, workspace

    yield create
    for journal in resources:
        journal.close()


def finish(adapter, intent, bundle, workspace):
    assert adapter.dispatch(intent, bundle, workspace).acknowledged
    observation = adapter.observe(intent, "2026-11-15T00:30:20Z")
    return observation, adapter.collect(intent)


@pytest.mark.parametrize("cls", ADAPTERS)
def test_protocol_collect_closed_stdin_and_cost(build, cls):
    adapter, intent, bundle, workspace = build(cls)
    assert isinstance(adapter, ExecutorAdapter)
    observation, result = finish(adapter, intent, bundle, workspace)
    assert observation.status == "finished"
    assert not result.errors
    assert len(result.produced_files) == 1
    assert result.produced_files[0].content == b'{"ok":true}'
    assert adapter.store.read(result.produced_files[0].storage_locator) == b'{"ok":true}'
    invocation = json.loads((adapter.workspace_root / workspace.workspace_id / "invocation.json").read_text())
    assert invocation["stdin"] == ""
    assert invocation["cwd"] == str(adapter.workspace_root / workspace.workspace_id)
    usage = result.usage[0]
    assert not validate_instance(usage, "usage_record")
    assert usage["cost_lines"][0] == {"status": "unavailable", "amount": None, "currency": None,
                                      "pricing_ref": None, "method": "personal_subscription"}
    consumption = adapter.quota_consumption(intent)
    assert not validate_quota_consumption_record(consumption, usage_record=usage)
    assert consumption["messages_consumed"] == 1
    assert consumption["credit_multiplier"] is None
    assert consumption["input_tokens"] == (12 if cls in [CodexAdapter, ClaudeCodeAdapter] else None)
    assert consumption["quota_outcome"] == "accepted"
    assert result.resolved_model_id == ("resolved-model" if cls in [CodexAdapter, ClaudeCodeAdapter] else None)
    assert result.provider_context_coverage == "unknown"
    assert all(c["support"] == "unknown" and c["evidence_refs"] == [] for c in adapter.describe().controls)
    assert adapter.collect(intent) == result


@pytest.mark.parametrize("cls", ADAPTERS)
@pytest.mark.parametrize("write", [False, True])
def test_exact_argv(build, cls, write):
    adapter, *_ = build(cls, permissions=CLIPermissions(workspace_write=write), fake=False)
    args = adapter._argv("/work", "model", "prompt;$(x)", "/final", 1501)
    if cls == CodexAdapter:
        expected = ["codex", "exec", "-C", "/work", "-s", "workspace-write" if write else "read-only", "-m", "model",
                    "-c", 'model_reasoning_effort="medium"', "--json", "-o", "/final", "--skip-git-repo-check", "prompt;$(x)"]
    elif cls == AgyAdapter:
        expected = ["agy", "-p", "prompt;$(x)", "--model", "model", "--mode", "accept-edits" if write else "plan",
                    "--output-format", "text", "--print-timeout", "2"]
    elif cls == KiroAdapter:
        expected = ["kiro-cli", "chat", "--no-interactive", "--trust-workspace", "--trust-tools=fs_read" + (",fs_write" if write else ""),
                    "--model", "model", "prompt;$(x)"]
    else:
        tools = "Read(/work/**)" + (",Edit(/work/**),Write(/work/**)" if write else "")
        expected = ["claude", "-p", "prompt;$(x)", "--output-format", "json", "--model", "model", "--permission-mode",
                    "acceptEdits" if write else "plan", "--allowedTools", tools, "--disallowedTools", "WebFetch,WebSearch,Bash"]
    assert args == expected
    assert not any(a.startswith("--dangerously-") or a == "--trust-all-tools" for a in args)


def test_spawn_without_shell_and_state_fsynced_before_spawn(build, monkeypatch):
    adapter, intent, bundle, workspace = build()
    original = subprocess.Popen
    calls = []

    def checked(argv, **kwargs):
        directory = adapter._directory(intent["dispatch_idempotency_key"])
        state = _read_state(directory / "state.json")
        assert state["phase"] == "spawning"
        assert state["intent"] == intent
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert isinstance(argv, list)
        calls.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", checked)
    finish(adapter, intent, bundle, workspace)
    assert len(calls) == 1
    launch = _read_state(adapter._directory(intent["dispatch_idempotency_key"]) / "launch.json")
    assert "env" not in launch


@pytest.mark.parametrize("cls", ADAPTERS)
def test_paid_api_variables_are_not_forwarded(build, monkeypatch, cls):
    names = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "AWS_SECRET_ACCESS_KEY", "WSLENV"}
    for name in names:
        monkeypatch.setenv(name, "secret-not-to-forward")
    assert names.isdisjoint(safe_environment())
    adapter, intent, bundle, workspace = build(cls)
    finish(adapter, intent, bundle, workspace)
    invocation = json.loads((adapter.workspace_root / workspace.workspace_id / "invocation.json").read_bytes())
    assert names.isdisjoint(invocation["environment_names"])


def test_never_reads_credentials(build, monkeypatch):
    adapter, intent, bundle, workspace = build()
    original = Path.open

    def guard(path, *args, **kwargs):
        assert not any(part in {".codex", ".claude", ".aws"} for part in path.parts)
        assert not (".gemini" in path.parts and not path.is_relative_to(adapter.workspace_root))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard)
    finish(adapter, intent, bundle, workspace)


@pytest.mark.parametrize("cls", ADAPTERS)
@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_timeout_and_cancel_kill_tree_even_without_observe(build, cls, stop):
    adapter, intent, bundle, workspace = build(cls, mode="hang", timeout_ms=5000)
    adapter.dispatch(intent, bundle, workspace)
    work = adapter.workspace_root / workspace.workspace_id
    end = time.monotonic() + 5
    while not (work / "heartbeat").exists() and time.monotonic() < end:
        time.sleep(.02)
    assert (work / "heartbeat").exists()
    if stop == "cancel":
        assert adapter.cancel(intent, "teste").termination_certainty == "confirmed"
    else:
        # Supervisor aplica o timeout mesmo se o runner nunca chamar observe.
        directory = adapter._directory(intent["dispatch_idempotency_key"])
        adapter._processes[str(directory)].wait(timeout=10)
    observation = adapter.observe(intent, NOW)  # prazo vencido retorna imediatamente
    assert observation.status == "stopped"
    assert observation.termination_certainty == "confirmed"
    heartbeat = (work / "heartbeat").read_bytes()
    time.sleep(.12)
    assert (work / "heartbeat").read_bytes() == heartbeat
    receipt = _read_state(adapter._directory(intent["dispatch_idempotency_key"]) / "receipt.json")
    assert receipt["stop_reason"] == stop + ("led" if stop == "cancel" else "")
    assert receipt["remote_consumption"] == "unknown"
    result = adapter.collect(intent)
    assert result.usage[0]["cost_lines"][0]["amount"] is None
    assert adapter.quota_consumption(intent)["quota_outcome"] == "unknown"


def test_reconcile_without_state_is_never_not_found(build):
    adapter, *_ = build()
    result = adapter.reconcile("unknown/key", None)
    assert result.outcome == "unknown"
    assert not result.authoritative


def test_spawn_failure_durable_not_found_and_no_redispatch(build, monkeypatch):
    adapter, intent, bundle, workspace = build()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert not adapter.dispatch(intent, bundle, workspace).acknowledged
    result = adapter.reconcile(intent["dispatch_idempotency_key"], None)
    assert result.outcome == "not_found" and result.authoritative
    assert result.evidence_refs
    assert not adapter.dispatch(intent, bundle, workspace).acknowledged
    restored = reopen(adapter)
    assert restored.reconcile(intent["dispatch_idempotency_key"], None).outcome == "not_found"


def reopen(adapter):
    return type(adapter)(adapter.profile, adapter.store, state_directory=adapter.state_directory,
                         workspace_root=adapter.workspace_root, clock=adapter.clock, runtime_version=adapter.runtime_version,
                         contracts=adapter.contracts, permissions=adapter.permissions, platform="linux")


def test_reconstructed_terminal_collect_and_session(build):
    adapter, intent, bundle, workspace = build()
    _, result = finish(adapter, intent, bundle, workspace)
    restored = reopen(adapter)
    assert restored.collect(intent) == result
    assert restored.quota_consumption(intent) == adapter.quota_consumption(intent)
    found = restored.reconcile(intent["dispatch_idempotency_key"], "session-codex")
    assert found.outcome == "found_finished"
    assert found.termination_certainty == "confirmed"
    with pytest.raises(ExecutorError, match="external_call_mismatch"):
        restored.reconcile(intent["dispatch_idempotency_key"], "other-session")


def test_spawning_without_receipt_is_unknown_and_never_restarted(build):
    adapter, intent, bundle, workspace = build()
    finish(adapter, intent, bundle, workspace)
    directory = adapter._directory(intent["dispatch_idempotency_key"])
    (directory / "receipt.json").unlink()
    meta = _read_state(directory / "state.json")
    meta.pop("state_hash")
    meta["phase"] = "spawning"
    write_record(directory / "state.json", meta)
    restored = reopen(adapter)
    assert restored.reconcile(intent["dispatch_idempotency_key"], None).outcome == "unknown"
    assert not restored.dispatch(intent, bundle, workspace).acknowledged


def test_corrupt_state_is_rejected(build):
    adapter, intent, bundle, workspace = build()
    finish(adapter, intent, bundle, workspace)
    state = adapter._directory(intent["dispatch_idempotency_key"]) / "state.json"
    body = json.loads(state.read_bytes())
    body["phase"] = "prepared"
    state.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(ExecutorError, match="invalid_executor_state"):
        reopen(adapter).reconcile(intent["dispatch_idempotency_key"], None)


@pytest.mark.parametrize("cls", ADAPTERS)
@pytest.mark.parametrize("mode", ["empty", "noise_only", "bad_contract", "project_quota"])
def test_no_false_success_or_project_quota(build, cls, mode):
    adapter, intent, bundle, workspace = build(cls, mode=mode)
    _, result = finish(adapter, intent, bundle, workspace)
    assert result.errors
    assert not result.produced_files
    assert all(e["code"] != "quota_limit_reached" for e in result.errors)
    assert all(not validate_instance(e, "error_record") for e in result.errors)


def test_real_style_quota_error_uses_7a_contract(build):
    adapter, intent, bundle, workspace = build(mode="quota")
    _, result = finish(adapter, intent, bundle, workspace)
    error = result.errors[0]
    assert error["code"] == "quota_limit_reached"
    assert not validate_instance(error, "error_record")
    assert error["quota_state"]["reset_at"] == "2026-11-15T01:05:00.000000Z"
    assert error["retryability"] == "retryable"
    assert error["termination_certainty"] == "confirmed"
    assert adapter.quota_consumption(intent)["quota_outcome"] == "quota_exhausted_on_call"


@pytest.mark.parametrize("cls", ADAPTERS)
def test_each_adapter_quota_and_cooldown_contract(build, cls):
    adapter, intent, bundle, workspace = build(cls, mode="quota")
    _, result = finish(adapter, intent, bundle, workspace)
    error = result.errors[0]
    assert error["code"] == "quota_limit_reached"
    assert not validate_instance(error, "error_record")
    state = error["quota_state"]
    if cls == CodexAdapter:
        assert state["status"] == "quota_exhausted"
    else:
        assert state["status"] == "cooldown"
        assert state["reset_at"] is None
        assert state["blocked_until"] == "2026-11-15T01:30:00.000000Z"
        assert state["default_cooldown_ms"] == 3600000


def test_no_final_with_zero_exit_and_json_error_can_be_quota(build):
    adapter, intent, bundle, workspace = build(mode="json_quota")
    _, result = finish(adapter, intent, bundle, workspace)
    assert result.errors[0]["code"] == "quota_limit_reached"


def test_expired_until_does_not_advance_shared_clock(build):
    clock = DeterministicClock(NOW, 1)
    adapter, intent, bundle, workspace = build(mode="hang", clock=clock)
    adapter.dispatch(intent, bundle, workspace)
    before = clock.now()
    adapter.observe(intent, "2026-11-14T00:00:00Z")
    assert clock.now() == before
    assert adapter.cancel(intent, "teste").termination_certainty == "confirmed"


def test_clock_must_be_shared_with_journal(build):
    adapter, *_ = build()
    with pytest.raises(ExecutorError, match="clock_mismatch"):
        type(adapter)(adapter.profile, adapter.store, state_directory=adapter.state_directory,
                      workspace_root=adapter.workspace_root, clock=lambda: NOW,
                      runtime_version=adapter.runtime_version, contracts=adapter.contracts)


def test_kiro_login_failure_is_not_quota(build):
    adapter, intent, bundle, workspace = build(KiroAdapter, mode="login")
    _, result = finish(adapter, intent, bundle, workspace)
    assert result.errors[0]["message"] == "authentication_unavailable"
    assert "quota_state" not in result.errors[0]
    assert result.errors[0]["retryability"] == "retryable"
    assert result.errors[0]["termination_certainty"] == "confirmed"


@pytest.mark.parametrize("cls", ADAPTERS)
def test_cooldown_profile_override_is_executed(build, cls):
    adapter, intent, bundle, workspace = build(cls, mode="quota_no_reset", cooldown_ms=120000)
    _, result = finish(adapter, intent, bundle, workspace)
    state = result.errors[0]["quota_state"]
    assert state["status"] == "cooldown" and state["reset_at"] is None
    assert state["blocked_until"] == "2026-11-15T00:32:00.000000Z"
    assert state["default_cooldown_ms"] == 120000
    assert result.errors[0]["code"] == "quota_limit_reached" and result.errors[0]["retryability"] == "retryable"


@pytest.mark.parametrize("cls", ADAPTERS)
@pytest.mark.parametrize("mode,reason", [("network", "network_unavailable"), ("login", "authentication_unavailable")])
def test_non_quota_unavailability_is_retryable_without_quota_state(build, cls, mode, reason):
    # Regressão do problema 1: sem cota não há reset_at, mas o erro confirmado
    # é retryable para o runner trocar de destino (sem isso a tarefa falhava).
    adapter, intent, bundle, workspace = build(cls, mode=mode)
    _, result = finish(adapter, intent, bundle, workspace)
    [error] = result.errors
    assert (error["code"], error["message"]) == ("execution_failed", reason)
    assert error["retryability"] == "retryable" and error["termination_certainty"] == "confirmed"
    assert "quota_state" not in error and "reset_at" not in error
    assert adapter.quota_consumption(intent)["quota_outcome"] == "unknown"


def test_empty_output_without_unavailability_stays_unknown(build):
    adapter, intent, bundle, workspace = build(AgyAdapter, mode="empty")
    _, result = finish(adapter, intent, bundle, workspace)
    assert result.errors[0]["code"] == "permission_or_empty_output"
    assert result.errors[0]["retryability"] == "unknown"


def test_codex_absolute_reset_date_through_real_adapter(build):
    # Formato real de 14/11/2026: "try again at Nov 15th, 2026 2:00 AM" (São Paulo).
    adapter, intent, bundle, workspace = build(CodexAdapter, mode="quota_absolute")
    _, result = finish(adapter, intent, bundle, workspace)
    [error] = result.errors
    state = error["quota_state"]
    assert error["code"] == "quota_limit_reached"
    assert state["status"] == "quota_exhausted" and state["reset_source"] == "cli_reported"
    assert state["reset_at"] == state["blocked_until"] == "2026-11-15T05:00:00.000000Z"
    assert adapter.profile["detector_version"] == "codex-quota-v2"


def test_multiline_json_envelope_among_noise(build):
    adapter, intent, bundle, workspace = build(ClaudeCodeAdapter, mode="pretty")
    _, result = finish(adapter, intent, bundle, workspace)
    assert not result.errors
    assert result.produced_files[0].content == b'{"ok":true}'


def test_rehashed_inconsistent_state_is_rejected(build):
    adapter, intent, bundle, workspace = build()
    finish(adapter, intent, bundle, workspace)
    path = adapter._directory(intent["dispatch_idempotency_key"]) / "state.json"
    state = _read_state(path)
    state.pop("state_hash")
    state["requested_model"] = "different-model"
    write_record(path, state)
    with pytest.raises(ExecutorError, match="invalid_executor_state"):
        reopen(adapter).reconcile(intent["dispatch_idempotency_key"], None)


def test_symlink_output_outside_workspace_is_never_published(build, monkeypatch, tmp_path):
    adapter, intent, bundle, workspace = build(mode="file")
    adapter.dispatch(intent, bundle, workspace)
    adapter.observe(intent, "2026-11-15T00:30:20Z")
    path = adapter.workspace_root / workspace.workspace_id / ".orq-output/report.json"
    external = tmp_path / "outside.json"
    external.write_text('{"ok":true}', encoding="utf-8")
    path.unlink()
    try:
        path.symlink_to(external)
    except OSError:
        pytest.skip("symlink indisponível neste Windows")
    monkeypatch.setattr(adapter.store, "put", lambda *a: pytest.fail("publicação de bytes fora do workspace"))
    result = adapter.collect(intent)
    assert not result.produced_files
    assert result.errors[0]["code"] == "output_contract_mismatch"


@pytest.mark.parametrize("cls", ADAPTERS)
def test_output_file_validated_before_publication(build, cls):
    adapter, intent, bundle, workspace = build(cls, mode="file", permissions=CLIPermissions(workspace_write=True))
    _, result = finish(adapter, intent, bundle, workspace)
    assert not result.errors
    assert result.produced_files[0].content == b'{"ok":true}'


def test_agy_permissions_only_in_workspace_then_removed(build):
    adapter, intent, bundle, workspace = build(AgyAdapter, mode="hang", permissions=CLIPermissions(workspace_write=True))
    adapter.dispatch(intent, bundle, workspace)
    path = adapter.workspace_root / workspace.workspace_id / ".gemini/antigravity-cli/settings.json"
    rules = json.loads(path.read_bytes())["permissions"]["allow"]
    assert rules == [f"read_file({path.parents[2]}/**)", f"write_file({path.parents[2]}/**)", f"edit_file({path.parents[2]}/**)"]
    assert adapter.cancel(intent, "teste").termination_certainty == "confirmed"
    assert not path.exists()


def test_bad_digest_and_idempotency_conflict(build):
    adapter, intent, bundle, workspace = build()
    with pytest.raises(ExecutorError, match="bundle_digest_mismatch"):
        adapter.dispatch(intent, b"changed", workspace)
    finish(adapter, intent, bundle, workspace)
    changed = copy.deepcopy(intent)
    changed["attempt_timeout_ms"] = 1
    seal(changed)
    with pytest.raises(ExecutorError, match="idempotency_conflict"):
        adapter.dispatch(changed, bundle, workspace)


def test_kiro_wsl_paths_and_linux_invocation(build):
    adapter, *_ = build(KiroAdapter, fake=False)
    assert not adapter._wsl
    assert adapter._argv("/tmp/work", "model", "prompt", "unused", 1)[0] == "kiro-cli"
    adapter.platform = "win32"
    assert adapter._wsl
    assert wsl_path(r"C:\dev\test space\task", workspace=True) == "/mnt/c/dev/test space/task"
    for path in [r"D:\dev\work", r"C:\Users\work", r"C:\dev\..\Users\work", r"\\server\dev\work"]:
        with pytest.raises(ExecutorError):
            wsl_path(path, workspace=True)


def test_kiro_windows_launch_uses_wsl_supervisor_without_shell(build, monkeypatch):
    adapter, intent, bundle, workspace = build(KiroAdapter, fake=False)
    adapter.platform = "win32"
    captured = []

    # Simula a montagem WSL; paths reais de teste podem estar em qualquer volume/SO.
    def mounted(path, *, workspace=False):
        relative = Path(path).relative_to(adapter.workspace_root) if workspace else Path(path).name
        return "/mnt/c/dev/test/" + str(relative).replace("\\", "/")

    monkeypatch.setattr(cli_module, "wsl_path", mounted)

    def capture(argv, **kwargs):
        captured.append(argv)
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        raise FileNotFoundError  # prova de não execução, sem chamar WSL real

    monkeypatch.setattr(subprocess, "Popen", capture)
    adapter.dispatch(intent, bundle, workspace)
    work = mounted(adapter.workspace_root / workspace.workspace_id, workspace=True)
    assert captured[0][:5] == ["wsl.exe", "--cd", work, "--", "python3"]
    config = _read_state(adapter._directory(intent["dispatch_idempotency_key"]) / "launch.json")
    assert config["argv"][0:5] == ["kiro-cli", "chat", "--no-interactive", "--trust-workspace", "--trust-tools=fs_read"]
    assert config["cwd"] == work


def test_command_grants_are_explicit_and_injection_rejected(build):
    permission = CLIPermissions(command_prefixes=(("python", "-m", "pytest"),))
    for cls in [KiroAdapter, ClaudeCodeAdapter]:
        adapter, *_ = build(cls, permissions=permission, fake=False)
        argv = adapter._argv("/work", "model", "prompt", "unused", 1)
        assert any("execute_bash" in a if cls == KiroAdapter else "Bash(python -m pytest:*)" in a for a in argv)
    with pytest.raises(ExecutorError):
        CLIPermissions(command_prefixes=(("echo", "$(secret)"),))


@pytest.mark.parametrize("mode", ["intermediate_quota", "final_then_quota", "intermediate_stderr_quota"])
def test_codex_message_before_quota_does_not_hide_failure(build, mode):
    adapter, intent, bundle, workspace = build(mode=mode)
    _, result = finish(adapter, intent, bundle, workspace)
    assert result.errors[0]["code"] == "quota_limit_reached"
    assert not result.produced_files


def test_codex_valid_final_after_recovered_error_is_preserved(build):
    adapter, intent, bundle, workspace = build(mode="error_then_final")
    observation, result = finish(adapter, intent, bundle, workspace)
    assert observation.status == "finished"
    assert not result.errors
    assert result.produced_files[0].content == b'{"ok":true}'


@pytest.mark.parametrize("cls", [AgyAdapter, KiroAdapter])
def test_text_stdout_project_error_is_not_cli_quota(build, cls):
    adapter, intent, bundle, workspace = build(cls, mode="project_json_error")
    _, result = finish(adapter, intent, bundle, workspace)
    assert result.errors[0]["code"] == "execution_failed"
    assert "quota_state" not in result.errors[0]


def test_json_literal_or_object_with_suffix_keeps_whole_diagnostic_line(build):
    adapter, intent, bundle, workspace = build(mode="json_prefix_noise")
    _, result = finish(adapter, intent, bundle, workspace)
    assert result.errors[0]["code"] == "execution_failed"
    assert "quota_state" not in result.errors[0]


@pytest.mark.parametrize("failure", ["bundle", "wsl_path", "argv"])
def test_dispatch_validates_before_creating_state_or_workspace(build, monkeypatch, failure):
    adapter, intent, bundle, workspace = build(KiroAdapter)
    original_argv = adapter._argv
    if failure == "bundle":
        bundle = b"\xff"
        intent["bundle_hash"] = bytes_digest(bundle)
        seal(intent)
    elif failure == "wsl_path":
        adapter.platform = "win32"
        monkeypatch.setattr(cli_module, "wsl_path", lambda *a, **k: (_ for _ in ()).throw(ExecutorError("invalid_workspace", "test")))
    else:
        monkeypatch.setattr(adapter, "_argv", lambda *a: (_ for _ in ()).throw(ExecutorError("invalid_workspace", "test")))
    with pytest.raises(ExecutorError, match="invalid_bundle" if failure == "bundle" else "invalid_workspace"):
        adapter.dispatch(intent, bundle, workspace)
    assert not adapter._directory(intent["dispatch_idempotency_key"]).exists()
    assert not (adapter.workspace_root / workspace.workspace_id).exists()
    assert adapter.reconcile(intent["dispatch_idempotency_key"], None).outcome == "unknown"
    # Uma falha de validação não deixa um despacho impossível de corrigir.
    adapter.platform = "linux"
    monkeypatch.setattr(adapter, "_argv", original_argv)
    valid = canonicalize({"context": "corrigido"})
    intent["bundle_hash"] = bytes_digest(valid)
    seal(intent)
    assert finish(adapter, intent, valid, workspace)[0].status == "finished"


def test_prepared_state_exists_before_workspace_creation(build, monkeypatch):
    adapter, intent, bundle, workspace = build()
    original = Path.mkdir
    work = adapter.workspace_root / workspace.workspace_id

    def checked(path, *args, **kwargs):
        if path == work:
            meta = _read_state(adapter._directory(intent["dispatch_idempotency_key"]) / "state.json")
            assert meta["phase"] == "prepared"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", checked)
    finish(adapter, intent, bundle, workspace)


def test_initial_state_write_failure_does_not_leave_empty_dispatch(build, monkeypatch):
    adapter, intent, bundle, workspace = build()
    original = cli_module.write_record
    monkeypatch.setattr(cli_module, "write_record", lambda *a: (_ for _ in ()).throw(OSError("fsync failed")))
    with pytest.raises(OSError, match="fsync failed"):
        adapter.dispatch(intent, bundle, workspace)
    assert not adapter._directory(intent["dispatch_idempotency_key"]).exists()
    assert not (adapter.workspace_root / workspace.workspace_id).exists()
    monkeypatch.setattr(cli_module, "write_record", original)
    assert finish(adapter, intent, bundle, workspace)[0].status == "finished"


def test_launched_without_receipt_is_running_while_supervisor_lives(build, monkeypatch):
    adapter, intent, bundle, workspace = build()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: SimpleNamespace(poll=lambda: None))
    adapter.dispatch(intent, bundle, workspace)
    observation = adapter.observe(intent, NOW)
    assert observation.status == "running"
    assert observation.termination_certainty == "not_terminated"


def test_observe_rereads_receipt_after_supervisor_exits(build, monkeypatch):
    adapter, intent, bundle, workspace = build()
    finish(adapter, intent, bundle, workspace)
    directory = adapter._directory(intent["dispatch_idempotency_key"])
    _, meta = adapter._metadata(intent)
    running = {"fingerprint": meta["fingerprint"], "phase": "running"}
    terminal = {**running, "phase": "terminal", "returncode": 0, "stop_reason": None}
    write_record(directory / "receipt.json", running)
    polls = []

    def poll():
        polls.append(True)
        if len(polls) == 1:
            return None  # _status viu running antes da saída.
        write_record(directory / "receipt.json", terminal)
        return 0

    waited = []
    adapter._processes[str(directory)] = SimpleNamespace(poll=poll, wait=lambda **k: waited.append(True))
    assert adapter.observe(intent, "2026-11-15T00:30:20Z").status == "finished"
    assert waited


def test_reconcile_uses_single_receipt_snapshot(build, monkeypatch):
    adapter, intent, bundle, workspace = build()
    finish(adapter, intent, bundle, workspace)
    directory, meta = adapter._metadata(intent)
    terminal = adapter._receipt(directory, meta)
    running = {"fingerprint": meta["fingerprint"], "phase": "running"}
    calls = []

    def racing_receipt(*args):
        calls.append(True)
        return running if len(calls) == 1 else terminal

    monkeypatch.setattr(adapter, "_receipt", racing_receipt)
    adapter._processes[str(directory)] = SimpleNamespace(poll=lambda: None)
    found = adapter.reconcile(intent["dispatch_idempotency_key"], None)
    assert len(calls) == 1
    assert found.outcome == "found_running"
    assert found.external_call_id is None
    ref = found.evidence_refs[0]
    assert ref["content_digest"] == bytes_digest(canonicalize({"state": meta, "receipt": running}))


def test_atomic_record_retries_creation_replace_and_read(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "WINDOWS", True)
    path = tmp_path / "receipt.json"
    write_record(path, {"phase": "launching"})
    original_create, original_replace, original_open = supervisor.tempfile.mkstemp, supervisor.os.replace, Path.open
    created, replaced, opened = [], [], []
    fsynced = []
    original_fsync = supervisor.os.fsync

    def fsync(fd):
        fsynced.append(fd)
        return original_fsync(fd)

    def create(**kwargs):
        created.append(kwargs)
        if len(created) == 1:
            raise PermissionError("temporary locked")
        return original_create(**kwargs)

    def replace(source, target):
        replaced.append(source)
        assert source.parent == path.parent
        assert source.name.startswith(".receipt.json.") and source.suffix == ".pending"
        assert fsynced  # temporário foi sincronizado antes de substituir o destino.
        assert _read_state(path)["phase"] == "launching"
        if len(replaced) == 1:
            raise PermissionError("destination locked")
        return original_replace(source, target)

    monkeypatch.setattr(supervisor.tempfile, "mkstemp", create)
    monkeypatch.setattr(supervisor.os, "replace", replace)
    monkeypatch.setattr(supervisor.os, "fsync", fsync)
    write_record(path, {"phase": "terminal"})
    assert len(created) == len(replaced) == 2
    assert not list(tmp_path.glob("*.pending"))

    def read_open(target, *args, **kwargs):
        if target == path:
            opened.append(True)
            if len(opened) < 3:
                raise PermissionError("delete pending")
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read_open)
    assert _read_state(path)["phase"] == "terminal"
    assert len(opened) == 3


def test_record_retry_is_bounded_and_failed_replace_cleans_temporary(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "WINDOWS", True)
    monkeypatch.setattr(supervisor, "PERMISSION_ATTEMPTS", 3)
    path = tmp_path / "state.json"
    write_record(path, {"phase": "prepared"})
    attempts = []

    def denied(*args, **kwargs):
        attempts.append(True)
        raise PermissionError("locked")

    monkeypatch.setattr(supervisor.os, "replace", denied)
    with pytest.raises(PermissionError):
        write_record(path, {"phase": "spawning"})
    assert len(attempts) == 3
    assert _read_state(path)["phase"] == "prepared"
    assert not list(tmp_path.glob("*.pending"))
    attempts.clear()
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(PermissionError):
        _read_state(path)
    assert len(attempts) == 3


def test_atomic_records_allow_concurrent_writers_and_reader(tmp_path):
    path = tmp_path / "state.json"
    write_record(path, {"writer": -1, "index": -1})

    def writer(identity):
        for index in range(30):
            write_record(path, {"writer": identity, "index": index})

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(writer, identity) for identity in range(2)]
        for _ in range(150):
            record = _read_state(path)
            assert record["writer"] in {-1, 0, 1}
        for future in futures:
            future.result()
    assert not list(tmp_path.glob("*.pending"))


def test_kiro_fast_exit_receipt_stress(build):
    # Repete launching -> running -> terminal com outro leitor concorrente.
    for _ in range(25):
        adapter, intent, bundle, workspace = build(KiroAdapter, mode="noise_only")
        directory = adapter._directory(intent["dispatch_idempotency_key"])
        assert adapter.dispatch(intent, bundle, workspace).acknowledged

        def read_while_running():
            process = adapter._processes[str(directory)]
            while process.poll() is None:
                _read_state(directory / "state.json")
                try:
                    _read_state(directory / "receipt.json")
                except FileNotFoundError:
                    pass
                time.sleep(.001)

        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(read_while_running)
            observation = adapter.observe(intent, "2026-11-15T00:30:20Z")
            reading.result(timeout=10)
        assert observation.status == "finished"
        assert _read_state(directory / "receipt.json")["phase"] == "terminal"
        result = adapter.collect(intent)
        assert result.errors[0]["code"] == "execution_failed"
        assert not result.produced_files


@pytest.fixture
def supervisor_case(tmp_path, monkeypatch):
    write_record(tmp_path / "launch.json", {"argv": ["fake"], "cwd": str(tmp_path), "timeout_ms": 0,
                                          "fingerprint": "test", "budget_deadline_ms": time.time_ns() // 1000000 + 10000})

    class Process:
        pid = 123
        alive = True
        killed = False
        waits = 0

        def poll(self):
            return None if self.alive else 0

        def kill(self):
            self.killed = True
            self.alive = False

        def wait(self, timeout):
            assert timeout == 5
            self.waits += 1
            if self.alive:
                raise subprocess.TimeoutExpired("fake", timeout)
            return -9 if self.killed else 0

    class Job:
        closed = False

        def attach_and_resume(self, process):
            pass

        def stop(self):
            return False

        def close(self):
            self.closed = True

    process, job = Process(), Job()
    monkeypatch.setattr(supervisor, "WINDOWS", True)
    monkeypatch.setattr(supervisor, "WindowsJob", lambda: job)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: process)
    return tmp_path, process, job


def test_supervisor_cleans_suspended_child_if_job_attachment_fails(supervisor_case, monkeypatch):
    directory, process, job = supervisor_case
    monkeypatch.setattr(job, "attach_and_resume", lambda p: (_ for _ in ()).throw(OSError("AssignProcessToJobObject")))
    with pytest.raises(OSError, match="AssignProcessToJobObject"):
        supervisor.main(directory)
    assert job.closed and process.killed and process.waits == 1
    receipt = _read_state(directory / "receipt.json")
    assert receipt["phase"] == "unknown"
    assert receipt["remote_consumption"] == "unknown"


def test_supervisor_wait_timeout_persists_unknown_and_bounds_cleanup(supervisor_case):
    directory, process, job = supervisor_case
    supervisor.main(directory)
    receipt = _read_state(directory / "receipt.json")
    assert receipt["phase"] == "unknown"
    assert receipt["returncode"] is None
    assert receipt["stop_reason"] == "timeout"
    assert job.closed and process.killed and process.waits == 2


@pytest.mark.parametrize("disappears", [False, True])
def test_posix_receipt_waits_for_entire_process_group(supervisor_case, monkeypatch, disappears):
    directory, process, _ = supervisor_case
    process.alive = False
    monkeypatch.setattr(supervisor, "WINDOWS", False)
    probes = []

    def killpg(pid, sig):
        assert pid == process.pid
        if sig == 0:
            probes.append(True)
            if disappears and len(probes) == 3:
                raise ProcessLookupError()

    monkeypatch.setattr(supervisor.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(supervisor.signal, "SIGKILL", 9, raising=False)
    ticks = iter([0., 0., .5, 1., 2.])
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(supervisor.time, "sleep", lambda s: None)
    supervisor.main(directory)
    assert len(probes) == 3
    receipt = _read_state(directory / "receipt.json")
    assert receipt["phase"] == ("terminal" if disappears else "unknown")


def test_safe_environment_adds_local_bin_on_posix_and_filters_extra_env(tmp_path, monkeypatch):
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    monkeypatch.setattr(supervisor, "WINDOWS", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    env = safe_environment({"PYTHONPATH": "/opt/orq/src"})
    assert env["PYTHONPATH"] == "/opt/orq/src"
    assert str(local_bin) in env["PATH"].split(supervisor.os.pathsep)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        safe_environment({"OPENAI_API_KEY": "nope"})


def test_bind_knowledge_session_requires_vault_root(build):
    adapter, intent, _bundle, _workspace = build(CodexAdapter)
    with pytest.raises(ExecutorError, match="mcp_binding_unverified"):
        adapter.bind_knowledge_session(intent, SimpleNamespace(reference={"id": "s", "version": "0.1", "content_digest": {"algorithm": "sha256", "value": "0" * 64}}))


@pytest.mark.parametrize("cls", ADAPTERS)
def test_native_mcp_connector_roundtrip_and_cleanup(tmp_path, monkeypatch, cls):
    from _search_vault import build_search_vault, requirement
    from _memory_vault import PROJECT
    from orq_contracts.family_mcp import AttemptKnowledgeSession
    from orq_contracts.knowledge_backend import EligibilityPolicy, OfflineMarkdownBackend
    from orq_contracts.memory_store.config import MemoryScope

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("rede proibida"))
    root = tmp_path / "case"
    root.mkdir()
    vault = root / "vault"
    vault.mkdir()
    catalog = build_search_vault(vault, "cat-mcp")
    clock = lambda: NOW
    journal = RunJournal.create(root / "runs", "run-demo-001", clock=clock)
    try:
        cli = cls.cli
        family = {"claude": "claude", "codex": "codex", "agy": "gemini", "kiro-cli": "kiro"}[cli]
        profile = seal({
            "schema_version": "0.1", "subscription_id": "subscription-test", "family_id": "family_" + family,
            "cli_executable": cli, "quota_pool_id": "pool-test", "billing_mode": "personal_subscription",
            "default_cooldown_ms": 3600000, "local_timezone": "America/Sao_Paulo", "detector_version": VERSIONS[cli],
        })
        schema = {"type": "object", "properties": {"ok": {"const": True}}, "required": ["ok"], "additionalProperties": False}
        contract_ref = {"id": "cli-test-output", "version": "0.1", "content_digest": canonical_digest(schema)}
        registry = ContentContractRegistry()
        registry.register(contract_ref, schema)
        adapter = cls(profile, ArtifactStore(journal), state_directory=root / "state", workspace_root=root / "work",
                      clock=clock, runtime_version="test-1", contracts=registry, platform="linux", vault_root=vault)
        original = adapter._argv
        adapter._argv = lambda *args: [sys.executable, str(FAKE), cli, "mcp_roundtrip", *original(*args)]

        scope = MemoryScope(catalog["catalog_id"], PROJECT, "family_" + family, "backend_engineer")
        policy = EligibilityPolicy(PROJECT, NOW)
        backend = OfflineMarkdownBackend(vault, catalog, policy, scope)
        req = requirement("K1")
        session_cfg = {
            "session_id": "session-mcp-1",
            "scope": {"catalog_id": scope.catalog_id, "project_id": scope.project_id,
                      "family_id": scope.family_id, "role_id": scope.role_id},
            "eligibility": {"project_id": PROJECT, "as_of": NOW},
            "requirements": [req],
            "choice": {
                "run_id": "run-demo-001",
                "workflow_ref": {"id": "wf", "version": "1", "content_digest": {"algorithm": "sha256", "value": "0" * 64}},
                "task_id": "T1", "candidate_attempt_number": 1, "created_at": NOW, "max_tokens": 2048,
                "policy_snapshot_refs": [{"id": "pol", "version": "1", "content_digest": {"algorithm": "sha256", "value": "1" * 64}}],
                "selection_id": "sel-mcp-1",
            },
            "materialize": {
                "routing_decision_ref": {"id": "rd", "version": "0.1", "content_digest": {"algorithm": "sha256", "value": "2" * 64}},
                "target_ref": {"id": "target-" + family, "version": "1", "content_digest": {"algorithm": "sha256", "value": "3" * 64}},
                "target_context_limit_tokens": 4096, "reserved_output_tokens": 256, "context_manifest_id": "cm-mcp-1",
            },
            "backend_id": backend.backend_id,
        }
        session = AttemptKnowledgeSession(root / "session", session_cfg, backend, clock=clock)
        bundle = canonicalize({"context": "mcp"})
        intent = load_fixture("valid/dispatch-intent.json")
        intent.update(bundle_hash=bytes_digest(bundle), executor_ref=adapter.describe().executor_ref,
                      attempt_timeout_ms=10000, created_at=NOW, deadline_at="2026-11-15T00:31:00Z",
                      knowledge_session_ref=session.reference)
        seal(intent)
        spec = {"output_id": "report", "artifact_type": "report", "description": "relatório JSON", "content_contract_ref": contract_ref}
        workspace = WorkspaceHandle("workspace-mcp", intent["workspace_base_ref"], (spec,), "requested-model")
        adapter.bind_knowledge_session(intent, session)
        observation, result = finish(adapter, intent, bundle, workspace)
        assert observation.status == "finished"
        assert not result.errors
        assert result.produced_files[0].content == b'{"ok":true}'
        sealed_result, audit, _closed = session.seal()
        assert sealed_result is not None
        assert any(entry["tool"] == "select_family_knowledge" and "error" not in entry["response"] for entry in audit)
        work_dir = adapter.workspace_root / workspace.workspace_id
        assert not (work_dir / ".orq-mcp.json").exists()
        assert not (work_dir / ".gemini" / "antigravity-cli" / "settings.json").exists()
        assert not (work_dir / ".kiro" / "settings" / "mcp.json").exists()
    finally:
        journal.close()


def test_probe_cli_versions_never_promotes_unknown_controls(monkeypatch):
    from orq_contracts.adapters import V01_V12_IDS, probe_cli_versions

    def fake_run(argv, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["shell"] is False
        assert "OPENAI_API_KEY" not in kwargs["env"]
        if argv[0] == "claude":
            return subprocess.CompletedProcess(argv, 0, stdout="2.1.319 (Claude Code)\n", stderr="")
        raise FileNotFoundError(argv[0])

    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    report = probe_cli_versions(include_wsl=False, platform="win32", runner=fake_run)
    assert report["all_controls_status"] == "unknown"
    by_cli = {item["cli"]: item for item in report["adapters"]}
    assert by_cli["claude"]["installed"] is True
    assert by_cli["claude"]["detected_version"] == "2.1.319 (Claude Code)"
    assert by_cli["kiro-cli"]["status"] == "skipped_wsl_disabled"
    assert all(item["controls_status"] == "unknown" for item in report["adapters"])
    assert all(item["pending_checklist"] == list(V01_V12_IDS) for item in report["adapters"])
