from __future__ import annotations

import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from conftest import load_fixture
from orq_contracts.adapters import AgyAdapter, ClaudeCodeAdapter, CodexAdapter, KiroAdapter, CLIPermissions
from orq_contracts.adapters.cli import _read_state, wsl_path
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

    def create(adapter_class=CodexAdapter, mode="success", timeout_ms=5000, permissions=CLIPermissions(), fake=True, clock=None):
        root = tmp_path / str(len(resources))
        root.mkdir()
        clock = clock if clock is not None else lambda: NOW
        journal = RunJournal.create(root / "runs", "run-demo-001", clock=clock)
        resources.append(journal)
        cli = adapter_class.cli
        family = {"claude": "claude", "codex": "codex", "agy": "gemini", "kiro-cli": "kiro"}[cli]
        profile = seal({"schema_version": "0.1", "subscription_id": "subscription-test", "family_id": "family_" + family,
                        "cli_executable": cli, "quota_pool_id": "pool-test", "billing_mode": "personal_subscription",
                        "default_cooldown_ms": 3600000, "local_timezone": "America/Sao_Paulo", "detector_version": VERSIONS[cli]})
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
    observation = adapter.observe(intent, "2026-11-15T00:30:08Z")
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


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "AWS_SECRET_ACCESS_KEY", "WSLENV"])
def test_paid_api_variables_are_not_forwarded(monkeypatch, name):
    monkeypatch.setenv(name, "secret-not-to-forward")
    assert name not in safe_environment()


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
    adapter, intent, bundle, workspace = build(cls, mode="hang", timeout_ms=800 if stop == "timeout" else 5000)
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
        time.sleep(1)
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
    adapter.observe(intent, "2026-11-15T00:30:08Z")
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

    def capture(argv, **kwargs):
        captured.append(argv)
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        raise FileNotFoundError  # prova de não execução, sem chamar WSL real

    monkeypatch.setattr(subprocess, "Popen", capture)
    adapter.dispatch(intent, bundle, workspace)
    work = wsl_path(adapter.workspace_root / workspace.workspace_id, workspace=True)
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
