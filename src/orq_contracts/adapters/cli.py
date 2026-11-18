"""ExecutorAdapter real com supervisão durável, parsers e política por CLI.

O runner fornece política já intersectada, workspace limpo e contratos locais.
Controles continuam unknown até a sessão V01–V12 da versão concreta do runtime.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import sys
import time
from typing import Callable

from .._storage import StorageError, confined_path, ensure_directory, fsync_directory
from ..artifact_store import ArtifactStore, MAX_ARTIFACT_BYTES
from ..canonical import canonical_digest, canonicalize
from ..evaluator import ContentContractRegistry
from ..executor import (CancelObservation, DispatchAck, ExecutionObservation, ExecutorDescription,
                        ExecutorError, ExecutorResult, ProducedFile, ReconciliationObservation,
                        Record, WorkspaceHandle, bytes_digest, error_record, seal_record, stable_id)
from ..simulated_executor import CONTROL_IDS, AUTHORITATIVE_NOT_FOUND_CONTROL, format_utc, parse_utc
from ..validator import validate_instance, validate_quota_consumption_record
from ._supervisor import read_bytes, safe_environment, write_record
from .quota import QuotaLimitDetector, VERSIONS


@dataclass(frozen=True)
class CLIPermissions:
    """Interseção efetiva da ExecutionPolicy, fornecida pelo host (nega por padrão).

    Prefixos são argv, não strings de shell. Kiro não limita prefixos dentro de
    execute_bash: o controle sandbox continua unknown até enforcement externo.
    """
    workspace_write: bool = False
    command_prefixes: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self):
        for prefix in self.command_prefixes:
            if not prefix or any(not re.fullmatch(r"[A-Za-z0-9_./=-]+", token) for token in prefix):
                raise ExecutorError("subscription_policy_violation", "prefixo de comando inválido")


@dataclass
class ParsedOutput:
    final: str | None = None
    diagnostics: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    session: str | None = None
    failed: bool = False


def _frames(text: str):
    """JSONL ou envelope JSON multilinha, com ruído entre os objetos."""
    decoder = json.JSONDecoder()
    offset = 0
    while offset < len(text):
        end = text.find("\n", offset)
        end = len(text) if end < 0 else end
        line = text[offset:end].strip()
        start = offset + len(text[offset:end]) - len(text[offset:end].lstrip())
        try:
            event, consumed = decoder.raw_decode(text, start)
        except ValueError:
            yield line, None
            offset = end + 1
        else:
            tail_end = text.find("\n", consumed)
            tail_end = len(text) if tail_end < 0 else tail_end
            if isinstance(event, dict) and not text[consumed:tail_end].strip():
                yield line, event
                offset = tail_end + 1
            else:
                # Não promove um sufixo da mesma linha a diagnóstico separado.
                yield line, None
                offset = end + 1


def _read_limited(path: Path) -> bytes:
    content = read_bytes(path, MAX_ARTIFACT_BYTES + 1)
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ExecutorError("artifact_too_large", "saída excede 8 MiB")
    return content


def _read_state(path: Path) -> Record:
    record = json.loads(_read_limited(path))
    if not isinstance(record, dict) or record.get("state_hash") != canonical_digest(
        {key: value for key, value in record.items() if key != "state_hash"}
    ):
        raise ExecutorError("invalid_executor_state", "estado durável corrompido")
    return record


def wsl_path(value: str | Path, *, workspace: bool = False) -> str:
    path = PureWindowsPath(str(value))
    if path.drive.lower() != "c:" or not path.is_absolute() or ".." in path.parts:
        raise ExecutorError("invalid_workspace", "WSL exige caminho absoluto no volume C")
    if workspace and (len(path.parts) < 3 or path.parts[1].lower() != "dev"):
        raise ExecutorError("invalid_workspace", "workspace Kiro deve ficar em C:\\dev")
    return "/mnt/c/" + "/".join(path.parts[1:])


class CLIAdapter:
    cli: str
    adapter_version = "0.1"

    def __init__(self, profile: Record, store: ArtifactStore, *, state_directory: Path,
                 workspace_root: Path, clock: Callable[[], str], runtime_version: str,
                 contracts: ContentContractRegistry, permissions: CLIPermissions = CLIPermissions(),
                 reasoning_effort: str = "medium", platform: str | None = None):
        if validate_instance(profile, "subscription_profile") or profile["cli_executable"] != self.cli:
            raise ExecutorError("subscription_policy_violation", "perfil inválido ou CLI divergente")
        if profile["detector_version"] != VERSIONS[self.cli]:
            raise ExecutorError("detector_version_mismatch", "gramática difere do perfil")
        if reasoning_effort not in {"low", "medium", "high"}:
            raise ExecutorError("subscription_policy_violation", "reasoning_effort inválido")
        self.profile, self.store = copy.deepcopy(profile), store
        self.clock, self.runtime_version, self.contracts = clock, runtime_version, contracts
        if clock is not store.journal.clock:
            raise ExecutorError("clock_mismatch", "executor e journal devem compartilhar o relógio")
        self.permissions, self.reasoning_effort = permissions, reasoning_effort
        self.platform = platform or sys.platform
        self.state_directory = Path(state_directory).absolute()
        self.workspace_root = Path(workspace_root).absolute()
        if self.state_directory.is_relative_to(self.workspace_root) or self.workspace_root.is_relative_to(self.state_directory):
            raise ExecutorError("invalid_workspace", "estado deve ficar fora dos workspaces")
        ensure_directory(self.state_directory)
        ensure_directory(self.workspace_root)
        self.detector = QuotaLimitDetector(self.cli, local_timezone=profile["local_timezone"],
                                          default_cooldown_ms=profile["default_cooldown_ms"])
        self._processes: dict[str, subprocess.Popen] = {}

    def _now(self) -> str:
        # DeterministicClock.now não avança, inclusive com until já vencido.
        return self.clock.now() if hasattr(self.clock, "now") else self.clock()

    def _reference(self, kind: str) -> Record:
        return {"id": f"{kind}-{self.cli}", "version": self.adapter_version if kind == "executor" else self.runtime_version,
                "content_digest": canonical_digest([kind, self.cli, self.adapter_version, self.runtime_version])}

    def describe(self) -> ExecutorDescription:
        controls = tuple({"id": name, "support": "unknown", "evidence_refs": []}
                         for name in (*CONTROL_IDS, AUTHORITATIVE_NOT_FOUND_CONTROL))
        return ExecutorDescription(self._reference("executor"), self.adapter_version, self._reference("runtime"),
                                   self.runtime_version, controls, ("cost", "tokens", "resolved_model_id", "provider_context_coverage"))

    def _directory(self, key: str) -> Path:
        return confined_path(self.state_directory, stable_id("call", key))

    def _metadata(self, intent: Record) -> tuple[Path, Record]:
        directory = self._directory(intent["dispatch_idempotency_key"])
        try:
            meta = _read_state(directory / "state.json")
        except FileNotFoundError as error:
            raise ExecutorError("dispatch_not_known", "intenção sem estado durável") from error
        if meta["intent"] != intent:
            raise ExecutorError("idempotency_conflict", "intenção diverge da original")
        if validate_instance(meta["intent"], "dispatch_intent"):
            raise ExecutorError("invalid_executor_state", "intenção durável inválida")
        if meta["profile"] != self.profile or meta["runtime_version"] != self.runtime_version:
            raise ExecutorError("invalid_executor_state", "assinatura ou versão difere do estado")
        expected_work = confined_path(self.workspace_root, meta["workspace_id"])
        fingerprint = canonical_digest([intent, meta["workspace_id"], meta["outputs"], meta["requested_model"],
                                        self.profile, self.runtime_version, self.permissions.workspace_write,
                                        self.permissions.command_prefixes])
        if meta["workspace"] != str(expected_work) or meta["fingerprint"] != fingerprint or meta["phase"] not in {"prepared", "spawning", "launched"}:
            raise ExecutorError("invalid_executor_state", "workspace, fingerprint ou fase divergente")
        return directory, meta

    def _receipt(self, directory: Path, meta: Record) -> Record:
        try:
            receipt = _read_state(directory / "receipt.json")
        except FileNotFoundError:
            return {"phase": meta["phase"]}
        if receipt["fingerprint"] != meta["fingerprint"]:
            raise ExecutorError("invalid_executor_state", "receipt de outra execução")
        if receipt["phase"] not in {"launching", "running", "not_started", "terminal", "unknown"}:
            raise ExecutorError("invalid_executor_state", "fase de receipt inválida")
        if receipt["phase"] == "terminal" and (type(receipt.get("returncode")) is not int or
                                               receipt.get("stop_reason") not in {None, "cancelled", "timeout", "output_limit"}):
            raise ExecutorError("invalid_executor_state", "receipt terminal inválido")
        return receipt

    def _prompt(self, bundle: bytes, workspace: WorkspaceHandle) -> str:
        # Bundle é opaco ao executor; transporte UTF-8 estrito, sem reinterpretar instruções.
        try:
            context = bundle.decode("utf-8")
        except UnicodeError as error:
            raise ExecutorError("invalid_bundle", "CLI exige bundle UTF-8") from error
        return canonicalize({"context_bundle": context, "expected_output": list(workspace.expected_output),
                             "output_protocol": 'Finalize com ORQ_FINAL:{"outputs":{"<output_id>":<JSON conforme contrato>}}. '
                                                'Alternativamente grave JSON em .orq-output/<output_id>.json e finalize com ORQ_FINAL:{"outputs":{}}.'}).decode()

    def _argv(self, workspace: str, model: str, prompt: str, final_file: str, timeout_ms: int) -> list[str]:
        raise NotImplementedError

    def dispatch(self, intent: Record, bundle: bytes, workspace: WorkspaceHandle) -> DispatchAck:
        if validate_instance(intent, "dispatch_intent"):
            raise ExecutorError("invalid_dispatch_intent", "intenção inválida")
        if not isinstance(bundle, bytes) or bytes_digest(bundle) != intent["bundle_hash"]:
            raise ExecutorError("bundle_digest_mismatch", "bundle diverge da intenção")
        if intent["run_id"] != self.store.journal.run_id or workspace.base_ref != intent["workspace_base_ref"]:
            raise ExecutorError("workspace_mismatch", "run ou base divergente")
        if intent["executor_ref"] != self.describe().executor_ref:
            raise ExecutorError("executor_mismatch", "intenção pertence a outro executor")
        if not workspace.requested_model_id or workspace.requested_model_id.startswith("-"):
            raise ExecutorError("invalid_workspace", "modelo solicitado deve ser explícito")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", workspace.workspace_id):
            raise ExecutorError("invalid_workspace", "workspace_id inválido")
        if not workspace.expected_output or len({o["output_id"] for o in workspace.expected_output}) != len(workspace.expected_output):
            raise ExecutorError("invalid_output_spec", "outputs vazios ou duplicados")
        for output in workspace.expected_output:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", output["output_id"]) or self.contracts.validator(output["content_contract_ref"]) is None:
                raise ExecutorError("unsupported_content_contract", "contrato local desconhecido ou output inválido")
        fingerprint = canonical_digest([intent, workspace.workspace_id, list(workspace.expected_output),
                                        workspace.requested_model_id, self.profile, self.runtime_version,
                                        self.permissions.workspace_write, self.permissions.command_prefixes])
        directory = self._directory(intent["dispatch_idempotency_key"])
        now = self._now()
        if directory.exists():
            _, meta = self._metadata(intent)
            if meta["fingerprint"] != fingerprint:
                raise ExecutorError("idempotency_conflict", "entradas do despacho divergentes")
            # Nunca redespacha após crash, mesmo que não haja acknowledgement.
            return DispatchAck(meta["phase"] == "launched", None, now)
        work = confined_path(self.workspace_root, workspace.workspace_id)
        prompt = self._prompt(bundle, workspace)
        cli_work = wsl_path(work, workspace=True) if self._wsl else str(work)
        final = work / ".orq-last-message"
        argv = self._argv(cli_work, workspace.requested_model_id, prompt,
                          wsl_path(final) if self._wsl else str(final), intent["attempt_timeout_ms"])
        timeout_ms = min(intent["attempt_timeout_ms"], max(0, int((parse_utc(intent["deadline_at"]) - parse_utc(now)).total_seconds() * 1000)))
        meta = {"intent": copy.deepcopy(intent), "profile": self.profile, "fingerprint": fingerprint,
                "runtime_version": self.runtime_version, "phase": "prepared", "workspace": str(work), "workspace_id": workspace.workspace_id,
                "outputs": list(workspace.expected_output), "requested_model": workspace.requested_model_id}
        directory.mkdir()
        try:
            fsync_directory(directory.parent)
            write_record(directory / "state.json", meta)
        except BaseException:
            if not (directory / "state.json").exists():
                directory.rmdir()
                fsync_directory(directory.parent)
            raise
        work.mkdir()  # workspace novo; não reutiliza estado de outra tentativa
        ensure_directory(work / ".orq-output")
        self._prepare_permissions(work)
        try:
            if timeout_ms == 0:
                write_record(directory / "receipt.json", {"fingerprint": fingerprint, "phase": "not_started"})
                self._restore_permissions(work)
                return DispatchAck(False, None, now)
            config = {"argv": argv, "cwd": cli_work, "timeout_ms": timeout_ms, "fingerprint": fingerprint,
                      "budget_deadline_ms": time.time_ns() // 1000000 + timeout_ms}
            write_record(directory / "launch.json", config)
            meta["phase"] = "spawning"
            write_record(directory / "state.json", meta)  # antes de qualquer efeito externo
            supervisor = Path(__file__).with_name("_supervisor.py").absolute()
            command = (["wsl.exe", "--cd", cli_work, "--", "python3", wsl_path(supervisor), wsl_path(directory)]
                       if self._wsl else [sys.executable, str(supervisor), str(directory)])
            try:
                process = subprocess.Popen(command, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, shell=False, env=safe_environment(),
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError:
                write_record(directory / "receipt.json", {"fingerprint": fingerprint, "phase": "not_started"})
                self._restore_permissions(work)
                return DispatchAck(False, None, now)
            self._processes[str(directory)] = process
            meta["phase"] = "launched"
            write_record(directory / "state.json", meta)
            return DispatchAck(True, None, now)
        except BaseException:
            # Se spawn pode ter ocorrido, a retomada segue incerta; nunca grava not_started.
            raise

    @property
    def _wsl(self) -> bool:
        return self.cli == "kiro-cli" and self.platform == "win32"

    def _prepare_permissions(self, workspace: Path) -> None:
        pass

    def _restore_permissions(self, workspace: Path) -> None:
        pass

    def _status(self, directory: Path, meta: Record, *, receipt: Record | None = None) -> ExecutionObservation:
        receipt = self._receipt(directory, meta) if receipt is None else receipt
        now = self._now()
        phase = receipt["phase"]
        if phase == "terminal":
            self._restore_permissions(Path(meta["workspace"]))
            process = self._processes.get(str(directory))
            if process:
                process.wait(timeout=5)
            status = "stopped" if receipt["stop_reason"] else "finished" if receipt["returncode"] == 0 else "failed"
            return ExecutionObservation(status, "confirmed", now)
        if phase == "not_started":
            self._restore_permissions(Path(meta["workspace"]))
        process = self._processes.get(str(directory))
        if phase in {"launched", "launching", "running"} and process and process.poll() is None:
            return ExecutionObservation("running", "not_terminated", now)
        return ExecutionObservation("unknown", "unknown", now)

    def observe(self, intent: Record, until: str) -> ExecutionObservation:
        directory, meta = self._metadata(intent)
        seconds = max(0., (parse_utc(until) - parse_utc(self._now())).total_seconds())
        end = time.monotonic() + seconds
        while True:
            observation = self._status(directory, meta)
            if observation.status in {"finished", "failed", "stopped"}:
                return observation
            process = self._processes.get(str(directory))
            if process is not None and process.poll() is not None:
                process.wait(timeout=5)
                return self._status(directory, meta)
            if process is None or time.monotonic() >= end:
                return observation
            time.sleep(min(.02, max(0., end - time.monotonic())))

    def cancel(self, intent: Record, reason: str) -> CancelObservation:
        directory, meta = self._metadata(intent)
        write_record(directory / "cancel", {"reason": reason[:512]})
        end = time.monotonic() + 5
        while True:
            observation = self._status(directory, meta)
            if observation.termination_certainty == "confirmed" or time.monotonic() >= end:
                return CancelObservation(observation.termination_certainty if observation.status != "running" else "unknown", self._now())
            time.sleep(.02)

    def reconcile(self, dispatch_idempotency_key: str, external_call_id: str | None) -> ReconciliationObservation:
        directory = self._directory(dispatch_idempotency_key)
        if not (directory / "state.json").exists():
            return ReconciliationObservation("unknown", None, "unknown", self._now())
        meta = _read_state(directory / "state.json")
        if meta["intent"]["dispatch_idempotency_key"] != dispatch_idempotency_key:
            raise ExecutorError("invalid_executor_state", "chave durável divergente")
        self._metadata(meta["intent"])
        receipt = self._receipt(directory, meta)
        snapshot = canonicalize({"state": meta, "receipt": receipt})
        self.store.put(snapshot)
        evidence = ({"artifact_id": stable_id("state", [dispatch_idempotency_key, bytes_digest(snapshot)]),
                     "content_digest": bytes_digest(snapshot)},)
        session = self._parse(directory, meta).session if receipt["phase"] == "terminal" else None
        if external_call_id is not None and session != external_call_id:
            raise ExecutorError("external_call_mismatch", "sessão não comprovada pelo estado")
        if receipt["phase"] in {"prepared", "not_started"}:
            self._restore_permissions(Path(meta["workspace"]))
            return ReconciliationObservation("not_found", None, "confirmed", self._now(), evidence, True)
        observation = self._status(directory, meta, receipt=receipt)
        outcome = {"finished": "found_finished", "failed": "found_failed", "stopped": "found_stopped", "running": "found_running"}.get(observation.status, "unknown")
        return ReconciliationObservation(outcome, session, observation.termination_certainty, self._now(), evidence)

    def _parse(self, directory: Path, meta: Record) -> ParsedOutput:
        parsed = ParsedOutput()
        for name in ("stdout", "stderr"):
            path = directory / name
            if not path.exists():
                continue
            text = _read_limited(path).decode("utf-8", errors="replace")
            frames = ((line.strip(), None) for line in text.splitlines()) if name == "stdout" and self.cli in {"agy", "kiro-cli"} else _frames(text)
            for line, event in frames:
                if not line or line.startswith(("[tool]", "logging before google.Init")):
                    continue
                if event is None:
                    # Somente stderr é canal diagnóstico livre. Stdout pode conter arquivos.
                    if name == "stderr":
                        parsed.diagnostics.append(line)
                    elif self.cli in {"agy", "kiro-cli"} and line.startswith("ORQ_FINAL:"):
                        parsed.final = line[len("ORQ_FINAL:"):]
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                if kind in {"error", "turn.failed"}:
                    parsed.final, parsed.failed = None, True
                    detail = event.get("error")
                    message = event.get("message") or (detail.get("message") if isinstance(detail, dict) else None)
                    if isinstance(message, str):
                        parsed.diagnostics.append(message)
                elif self.cli == "claude" and kind == "result":
                    if event.get("is_error") or event.get("subtype", "success") != "success":
                        parsed.final, parsed.failed = None, True
                        details = event.get("errors", [event.get("result", "")])
                        parsed.diagnostics.extend(e for e in (details if isinstance(details, list) else [details]) if isinstance(e, str))
                    elif isinstance(event.get("result"), str) and event["result"].strip():
                        parsed.final = event["result"]
                    parsed.session = event.get("session_id")
                    parsed.model = event.get("model")
                    self._tokens(parsed, event.get("usage"))
                elif self.cli == "codex":
                    if kind == "thread.started":
                        parsed.session = event.get("thread_id")
                    elif kind == "turn.completed":
                        self._tokens(parsed, event.get("usage"))
                        parsed.model = event.get("model")
                    elif kind == "item.completed" and isinstance(event.get("item"), dict) and event["item"].get("type") == "agent_message":
                        text = event["item"].get("text")
                        if isinstance(text, str) and text.strip():
                            parsed.final = text
        # -o é controlado pela CLI, mas não serve como fonte de erro/cota.
        if self.cli == "codex" and not parsed.final and not parsed.failed:
            final = confined_path(Path(meta["workspace"]), ".orq-last-message")
            if final.is_file():
                parsed.final = _read_limited(final).decode("utf-8", errors="replace").strip() or None
        for key in ("model", "session"):
            value = getattr(parsed, key)
            if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                setattr(parsed, key, None)
        return parsed

    @staticmethod
    def _tokens(parsed: ParsedOutput, usage) -> None:
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens"):
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    setattr(parsed, key, value)

    def _error(self, intent: Record, code: str, message: str, now: str, *, quota_state=None, unavailable=False) -> Record:
        # Cota e indisponibilidade não-cota (rede/login) confirmadas pelo detector
        # permitem nova tentativa; o Router exclui o destino na próxima decisão.
        retryable = quota_state is not None or unavailable
        record = error_record(run_id=intent["run_id"], scope_type="attempt", scope_ref=intent["attempt_id"],
                              phase="collect", category="provider", code="execution_failed" if quota_state else code, message=message[:4096],
                              timestamp=now, retryability="retryable" if retryable else "unknown",
                              termination_certainty="confirmed")
        if quota_state:
            record["code"] = code
            record["quota_state"] = quota_state
            return seal_record(record, "error_record")
        return record

    def collect(self, intent: Record) -> ExecutorResult:
        directory, meta = self._metadata(intent)
        if (directory / "result.json").exists():
            result = _read_state(directory / "result.json")
            files = tuple(ProducedFile(file["output_id"], self.store.read(file["storage_locator"]),
                                       file["artifact_type"], file["content_contract_ref"], file["declared_digest"],
                                       file["storage_locator"]) for file in result["files"])
            return ExecutorResult(files, tuple(result["usage"]), tuple(result["errors"]),
                                  meta["requested_model"], result["model"], "unknown")
        receipt = self._receipt(directory, meta)
        if receipt["phase"] != "terminal":
            raise ExecutorError("result_unavailable", "término local não confirmado")
        observation = self._status(directory, meta)
        parsed = self._parse(directory, meta)
        now = self._now()
        try:
            envelope = json.loads((parsed.final or "").removeprefix("ORQ_FINAL:"))
            final_response = isinstance(envelope, dict) and isinstance(envelope.get("outputs"), dict)
        except ValueError:
            final_response = False
        detection = self.detector.detect(returncode=receipt["returncode"], final_response=final_response,
                                         diagnostics=tuple(parsed.diagnostics), observed_at=now)
        errors, files = [], []
        if detection.reason == "quota_exhausted":
            state = self.detector.state_record(detection, profile=self.profile, intent=intent, observed_at=now)
            errors.append(self._error(intent, "quota_limit_reached", detection.excerpt, now, quota_state=state))
        elif observation.status != "finished" or not parsed.final:
            reason = detection.reason if detection.reason != "none" else receipt["stop_reason"] or "permission_or_empty_output"
            code = ("permission_or_empty_output" if self.cli == "agy" and not parsed.final and
                    any("no output produced" in line.lower() for line in parsed.diagnostics) else "execution_failed")
            errors.append(self._error(intent, code, reason, now, unavailable=code == "execution_failed" and
                                      detection.reason in {"network_unavailable", "authentication_unavailable"}))
        if not errors:
            pending = []
            try:
                message = parsed.final.removeprefix("ORQ_FINAL:")
                envelope = json.loads(message)
                outputs = envelope["outputs"]
                if not isinstance(outputs, dict) or set(outputs) - {o["output_id"] for o in meta["outputs"]}:
                    raise ValueError("outputs desconhecidos")
                for spec in meta["outputs"]:
                    path = confined_path(Path(meta["workspace"]), ".orq-output", spec["output_id"] + ".json")
                    content = canonicalize(outputs[spec["output_id"]]) if spec["output_id"] in outputs else _read_limited(path)
                    validator = self.contracts.validator(spec["content_contract_ref"])
                    if validator is None or not validator.is_valid(json.loads(content)):
                        raise ValueError("contrato de conteúdo inválido")
                    pending.append((spec, content))
            except (ValueError, KeyError, OSError, StorageError, ExecutorError) as error:
                errors.append(self._error(intent, "output_contract_mismatch", str(error), now))
            if not errors:
                files = [ProducedFile(spec["output_id"], content, spec["artifact_type"], spec["content_contract_ref"],
                                      bytes_digest(content), self.store.put(content)) for spec, content in pending]
        measurements = [{"metric_id": key, "quantity": str(value), "unit": "token", "status": "reported",
                         "source_ref": self._reference("runtime")}
                        for key, value in (("input_tokens", parsed.input_tokens), ("output_tokens", parsed.output_tokens))
                        if value is not None]
        usage = seal_record({
            "schema_version": "0.1", "usage_id": stable_id("usage", intent["dispatch_idempotency_key"]),
            "run_id": intent["run_id"], "scope_type": "attempt", "scope_ref": intent["attempt_id"],
            "subscription_id": self.profile["subscription_id"], "provider_ref": None,
            "observed_at": now, "recorded_at": now, "measurements": measurements,
            "cost_lines": [{"status": "unavailable", "amount": None, "currency": None,
                            "pricing_ref": None, "method": "personal_subscription"}],
            "coverage": {"reported": [m["metric_id"] for m in measurements], "estimated": [], "unavailable": ["cost"]}, "supersedes_usage_id": None,
        }, "usage_record")
        # Extensão fora do protocolo 6D: o runner 7E grava os registros retornados.
        consumption = seal_record({
            "schema_version": "0.1", "quota_consumption_id": stable_id("consumption", intent["dispatch_idempotency_key"]),
            **{key: intent[key] for key in ("run_id", "task_id", "attempt_id")},
            **{key: self.profile[key] for key in ("subscription_id", "family_id", "quota_pool_id")},
            "usage_record_ref": {"id": usage["usage_id"], "version": "0.1", "content_digest": canonical_digest(usage)},
            "messages_consumed": 1, "input_tokens": parsed.input_tokens, "output_tokens": parsed.output_tokens,
            "credit_multiplier": None, "quota_outcome": "quota_exhausted_on_call" if detection.reason == "quota_exhausted" else "accepted" if not errors else "unknown",
            "recorded_at": now,
        }, "quota_consumption_record")
        if validate_quota_consumption_record(consumption, usage_record=usage):
            raise ExecutorError("invalid_record", "consumo não corresponde ao uso")
        write_record(directory / "consumption.json", {"record": consumption})
        write_record(directory / "result.json", {
            "usage": [usage], "errors": errors, "model": parsed.model,
            "files": [{"output_id": file.output_id, "artifact_type": file.artifact_type,
                       "content_contract_ref": file.content_contract_ref, "declared_digest": file.declared_digest,
                       "storage_locator": file.storage_locator} for file in files],
        })
        return ExecutorResult(tuple(files), (usage,), tuple(errors), meta["requested_model"], parsed.model, "unknown")

    def quota_consumption(self, intent: Record) -> Record:
        directory, _ = self._metadata(intent)
        return copy.deepcopy(_read_state(directory / "consumption.json")["record"])


class CodexAdapter(CLIAdapter):
    cli = "codex"

    def _argv(self, workspace, model, prompt, final_file, timeout_ms):
        return ["codex", "exec", "-C", workspace, "-s", "workspace-write" if self.permissions.workspace_write else "read-only",
                "-m", model, "-c", f'model_reasoning_effort="{self.reasoning_effort}"', "--json", "-o", final_file,
                "--skip-git-repo-check", prompt]


class ClaudeCodeAdapter(CLIAdapter):
    cli = "claude"

    def _argv(self, workspace, model, prompt, final_file, timeout_ms):
        allowed = [f"Read({workspace}/**)"]
        if self.permissions.workspace_write:
            allowed += [f"Edit({workspace}/**)", f"Write({workspace}/**)"]
        allowed += ["Bash(" + " ".join(prefix) + ":*)" for prefix in self.permissions.command_prefixes]
        denied = ["WebFetch", "WebSearch"] + ([] if self.permissions.command_prefixes else ["Bash"])
        return ["claude", "-p", prompt, "--output-format", "json", "--model", model,
                "--permission-mode", "acceptEdits" if self.permissions.workspace_write else "plan",
                "--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(denied)]


class AgyAdapter(CLIAdapter):
    cli = "agy"

    def _argv(self, workspace, model, prompt, final_file, timeout_ms):
        return ["agy", "-p", prompt, "--model", model, "--mode", "accept-edits" if self.permissions.workspace_write else "plan",
                "--output-format", "text", "--print-timeout", str(max(1, (timeout_ms + 999) // 1000))]

    def _prepare_permissions(self, workspace: Path) -> None:
        path = workspace / ".gemini" / "antigravity-cli" / "settings.json"
        ensure_directory(path.parent)
        allow = [f"read_file({workspace}/**)"]
        if self.permissions.workspace_write:
            allow += [f"write_file({workspace}/**)", f"edit_file({workspace}/**)"]
        allow += ["command(" + " ".join(prefix) + ")" for prefix in self.permissions.command_prefixes]
        # Arquivo novo do workspace isolado. Nunca lê/modifica settings do HOME.
        with path.open("xb") as stream:
            stream.write(canonicalize({"permissions": {"allow": allow}}))
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(path.parent)

    def _restore_permissions(self, workspace: Path) -> None:
        path = confined_path(workspace, ".gemini", "antigravity-cli", "settings.json")
        if path.exists():
            path.unlink()
            fsync_directory(path.parent)


class KiroAdapter(CLIAdapter):
    cli = "kiro-cli"

    def _argv(self, workspace, model, prompt, final_file, timeout_ms):
        tools = ["fs_read"] + (["fs_write"] if self.permissions.workspace_write else [])
        if self.permissions.command_prefixes:
            tools.append("execute_bash")
        return ["kiro-cli", "chat", "--no-interactive", "--trust-workspace", "--trust-tools=" + ",".join(tools),
                "--model", model, prompt]
