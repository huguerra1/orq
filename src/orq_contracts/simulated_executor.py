"""Executor local roteirizado, sem rede, journal ou laço de execução."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence, Set
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .artifact_store import ArtifactStore, ArtifactStoreError
from .canonical import CanonicalizationError, canonical_digest, canonicalize
from .executor import (
    CancelObservation, DispatchAck, DispatchIntent, ExecutionObservation, ExecutorDescription,
    ExecutorError, ExecutorResult, ProducedFile, ReconciliationObservation, Record,
    WorkspaceHandle, bytes_digest, error_record, seal_record, stable_id,
)
from .schemas import load_schemas, validator_for
from .validator import validate_instance, validate_workflow


SIMULATED_OUTPUT_CONTRACT_ID = "simulated-output"
SIMULATED_OUTPUT_CONTRACT_VERSION = "0.1"
# Mesmos identificadores usados em RoutingRequest.required_control_ids e RoutingTargetCatalog.controls.
CONTROL_IDS = (
    "non_interactive", "sandbox", "timeout", "cancellation", "structured_output",
    "usage_reporting", "model_identification", "call_identification",
)
AUTHORITATIVE_NOT_FOUND_CONTROL = "reconcile_not_found_authoritative"
EXECUTOR_STATE_KIND = "simulated_executor_state"
_SCHEMA_BASE = "https://orq.local/schemas/v0.1/"
_NOT_EXECUTED = frozenset({"reject_confirmed", "not_executed"})
_TERMINALS = (None, "finished", "failed", "stopped")
_STATE_FIELDS = frozenset({"schema_version", "kind", "run_id", "script_ref", "calls", "state_hash"})
_CALL_FIELDS = frozenset({
    "intent", "workspace", "fingerprint", "ack", "external_call_id", "started_at", "executed",
    "terminal", "failure", "result",
})
_WORKSPACE_FIELDS = frozenset({"workspace_id", "base_ref", "expected_output", "requested_model_id"})


class ScriptError(ExecutorError):
    """Roteiro estruturalmente inválido, divergente ou incompleto para a consulta."""


def simulated_output_contract() -> tuple[Record, Record]:
    """Referência versionada (digest JCS do schema) e schema do envelope de ``from_output_spec``.

    OutputSpecs atendidos pelo simulado declaram exatamente esta referência; o avaliador
    registra o par em ContentContractRegistry para checar ``content_contract_valid``.
    """
    schema = copy.deepcopy(load_schemas()["simulated_output"])
    reference = {
        "id": SIMULATED_OUTPUT_CONTRACT_ID, "version": SIMULATED_OUTPUT_CONTRACT_VERSION,
        "content_digest": canonical_digest(schema),
    }
    return reference, schema


def parse_utc(value: str) -> datetime:
    try:
        if not isinstance(value, str) or not value.endswith("Z"):
            raise ValueError
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except (ValueError, OverflowError) as error:
        raise ExecutorError("invalid_clock", "instante UTC inválido") from error


def format_utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


class DeterministicClock:
    """now() não avança; clock() retorna o instante e avança step_ms.

    observe avança até o primeiro término ou até until, sem espera real e nunca para trás.
    A mesma instância é compartilhada com o journal e o harness (obrigatório no executor e no avaliador).
    """

    def __init__(self, start: str, step_ms: int):
        self._instant = parse_utc(start)
        if type(step_ms) is not int or step_ms < 1:
            raise ExecutorError("invalid_clock", "step_ms deve ser inteiro positivo")
        self.step_ms = step_ms

    def now(self) -> str:
        return format_utc(self._instant)

    def __call__(self) -> str:
        value = self.now()
        try:
            self._instant += timedelta(milliseconds=self.step_ms)
        except OverflowError as error:
            raise ExecutorError("invalid_clock", "relógio excede intervalo UTC") from error
        return value

    def advance_to(self, instant: str) -> None:
        self._instant = max(self._instant, parse_utc(instant))


def _unique_object(pairs: list[tuple[str, Any]]) -> Record:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScriptError("invalid_script", "chave JSON duplicada")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("constante JSON inválida")


def _check_behavior(behavior: Record, where: str) -> None:
    """Combinações que o simulado não consegue honrar são recusadas na carga, sem correção silenciosa."""
    dispatch, execution, reconcile = behavior["dispatch"], behavior["execution"], behavior["reconcile"]
    if dispatch in _NOT_EXECUTED and reconcile.startswith("found_"):
        raise ScriptError("invalid_script", f"{where}: reconcile {reconcile} sem execução iniciada")
    if dispatch not in _NOT_EXECUTED and reconcile == "not_found":
        raise ScriptError("invalid_script", f"{where}: not_found nega execução iniciada")
    if reconcile == "found_finished" and execution.startswith("fail_"):
        raise ScriptError("invalid_script", f"{where}: found_finished contradiz {execution}")
    if reconcile == "found_failed" and execution == "finish":
        raise ScriptError("invalid_script", f"{where}: found_failed contradiz finish")


def _seal_state(body: Record) -> Record:
    body["state_hash"] = canonical_digest({k: v for k, v in body.items() if k != "state_hash"})
    return body


def _invalid_state(message: str) -> ExecutorError:
    return ExecutorError("invalid_executor_state", message)


class SimulationScript:
    def __init__(self, document: Record, *, expected_digest: Record):
        issues = validate_instance(document, "simulation_script")
        if issues:
            raise ScriptError("invalid_script", f"{issues[0].path}: {issues[0].code}")
        try:
            digest = canonical_digest(document)
            DeterministicClock(**document["clock"])()
        except (CanonicalizationError, ExecutorError) as error:
            raise ScriptError("invalid_script", "JSON canônico ou relógio inválido") from error
        self._document = copy.deepcopy(document)
        self._check_semantics()
        if digest != expected_digest:
            raise ScriptError("script_digest_mismatch", "digest do roteiro diverge da referência")
        self._reference = {"id": document["script_id"], "version": document["version"], "content_digest": digest}

    def _check_semantics(self) -> None:
        _check_behavior(self._document["default"], "default")
        for task_id, attempts in self._document["attempts"].items():
            for number in attempts:
                _check_behavior(self.for_attempt(task_id, int(number)), f"attempts.{task_id}.{number}")
        hook_ids, triggers = set(), set()
        for hook in self._document["hooks"]:
            trigger = (canonicalize(hook["scope"]), hook["event_type"])
            if hook["hook_id"] in hook_ids or trigger in triggers:
                raise ScriptError("invalid_script", "hook_id ou (escopo, evento) repetido")
            hook_ids.add(hook["hook_id"])
            triggers.add(trigger)

    @classmethod
    def from_bytes(cls, content: bytes, *, expected_digest: Record) -> SimulationScript:
        try:
            document = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object,
                                  parse_constant=_invalid_constant)
        except (ValueError, UnicodeError) as error:
            raise ScriptError("invalid_script", "roteiro deve ser JSON UTF-8 sem ambiguidades") from error
        return cls(document, expected_digest=expected_digest)

    @property
    def reference(self) -> Record:
        return copy.deepcopy(self._reference)

    @property
    def authoritative_not_found(self) -> bool:
        return self._document["reconcile_not_found_authoritative"]

    @property
    def hooks(self) -> tuple[Record, ...]:
        return tuple(copy.deepcopy(self._document["hooks"]))

    def make_clock(self) -> DeterministicClock:
        return DeterministicClock(**self._document["clock"])

    def for_attempt(self, task_id: str, attempt_number: int) -> Record:
        if type(attempt_number) is not int or attempt_number < 1:
            raise ScriptError("invalid_script_lookup", "attempt_number deve ser positivo")
        behavior = copy.deepcopy(self._document["default"])
        override = self._document["attempts"].get(task_id, {}).get(str(attempt_number), {})
        verifier = {**behavior.get("verifier", {}), **override.get("verifier", {})}
        behavior.update(copy.deepcopy(override))
        behavior["verifier"] = copy.deepcopy(verifier)
        return behavior

    def verdict(self, script_key: str, evaluation_number: int, *, task_id: str | None = None,
                attempt_number: int | None = None) -> str:
        if type(evaluation_number) is not int or evaluation_number < 1:
            raise ScriptError("invalid_script_lookup", "evaluation_number deve ser positivo")
        verifier = self._document["workflow_verifier"] if task_id is None else self.for_attempt(
            task_id, attempt_number
        )["verifier"]
        if script_key not in verifier:
            raise ScriptError("script_key_missing", "script_key ausente no verificador")
        value = verifier[script_key]
        if isinstance(value, list):
            if evaluation_number > len(value):
                raise ScriptError("evaluation_script_exhausted", "avaliação além do fim da lista")
            return value[evaluation_number - 1]
        return value

    def validate_against_workflow(self, workflow: Record) -> None:
        """Confere o roteiro contra o WorkflowSpec antes da admissão (antes de qualquer evento).

        Limites da tarefa são o mínimo dos valores presentes na TaskSpec e no WorkflowSpec;
        ausência de ambos gera ``script_limits_unknown``. Listas por tentativa cobrem todas as
        avaliações permitidas; a avaliação global é única e exige apenas um elemento.
        """
        if validate_workflow(workflow):
            raise ScriptError("invalid_workflow", "WorkflowSpec estrutural ou semanticamente inválido")
        contract, _ = simulated_output_contract()
        tasks = {task["task_id"]: task for task in workflow["tasks"]}
        workflow_limits = workflow["constraints"]

        def limit(name: str, task: Record | None) -> int:
            values = [limits[name] for limits in (workflow_limits, task.get("constraints", {}) if task else {})
                      if name in limits]
            if not values:
                where = task["task_id"] if task else "workflow"
                raise ScriptError("script_limits_unknown", f"{name} indefinido para {where}")
            return min(values)

        def check_keys(verifier: Record, criteria: list[Record], task: Record | None, where: str) -> None:
            for criterion in criteria:
                if criterion["method"] != "simulated_verdict":
                    continue
                key = criterion["pass_rule"].get("script_key")
                if not isinstance(key, str):
                    raise ScriptError("invalid_pass_rule", f"{where}: simulated_verdict sem script_key")
                if key not in verifier:
                    raise ScriptError("script_key_missing", f"{where}: script_key {key} ausente")
                value = verifier[key]
                if isinstance(value, list) and len(value) < (limit("max_evaluations_per_attempt", task) if task else 1):
                    raise ScriptError("evaluation_script_incomplete", f"{where}: lista {key} menor que o limite")

        for task_id in self._document["attempts"]:
            if task_id not in tasks:
                raise ScriptError("unknown_task_id", f"attempts.{task_id} fora do workflow")
        for hook in self._document["hooks"]:
            scope = hook["scope"]
            if scope["kind"] == "task_attempt" and (
                scope["task_id"] not in tasks
                or scope["attempt_number"] > limit("max_attempts", tasks[scope["task_id"]])
            ):
                raise ScriptError("unknown_hook_scope", f"hook {hook['hook_id']} fora do workflow")
        for task_id, task in tasks.items():
            if any(output["content_contract_ref"] != contract for output in task["expected_output"]):
                raise ScriptError("unsupported_content_contract",
                                  f"{task_id}: OutputSpec deve declarar o envelope simulated-output")
            max_attempts = limit("max_attempts", task)
            for number in self._document["attempts"].get(task_id, {}):
                if int(number) > max_attempts:
                    raise ScriptError("attempt_out_of_range", f"attempts.{task_id}.{number} excede max_attempts")
            output_ids = {output["output_id"] for output in task["expected_output"]}
            for number in range(1, max_attempts + 1):
                behavior = self.for_attempt(task_id, number)
                where = f"{task_id}/{number}"
                if isinstance(behavior["outputs"], dict) and not set(behavior["outputs"]) <= output_ids:
                    raise ScriptError("unknown_output_id", f"{where}: roteiro altera output não declarado")
                check_keys(behavior["verifier"], task["evaluation_criteria"], task, where)
        check_keys(self._document["workflow_verifier"], workflow["evaluation_criteria"], None, "workflow")


class HookController:
    """Disparo único de crash_after/cancel_after, controlado pelo harness.

    O escopo é explícito: ``run``, ``task_attempt`` (task_id + attempt_number) ou ``workflow``
    (avaliação global). ``due`` devolve o hook ainda não disparado para o evento e marca o disparo.
    O harness grava ``fired`` de forma durável **antes** de simular o crash e o repassa ao
    controlador da retomada; assim o mesmo hook nunca dispara de novo (S35 sem laço).
    O executor e o journal nunca leem hooks.
    """

    def __init__(self, script: SimulationScript, *, fired: Sequence[str] | Set[str] = ()):
        self._hooks = script.hooks
        known = {hook["hook_id"] for hook in self._hooks}
        if isinstance(fired, (str, bytes)) or not isinstance(fired, (Sequence, Set)):
            raise ScriptError("unknown_hook_id", "fired deve ser sequência ou conjunto de IDs")
        fired = sorted(fired) if isinstance(fired, Set) and all(isinstance(item, str) for item in fired) else list(fired)
        if any(not isinstance(item, str) or item not in known for item in fired) or len(set(fired)) != len(fired):
            raise ScriptError("unknown_hook_id", "fired contém hook desconhecido ou repetido")
        self._fired = fired
        self._scope_validator = validator_for("simulation_script").evolve(
            schema={"$ref": _SCHEMA_BASE + "simulation-script.schema.json#/$defs/hookScope"})
        self._event_validator = validator_for("journal_event").evolve(
            schema={"$ref": _SCHEMA_BASE + "journal-event.schema.json#/properties/event_type"})

    @property
    def fired(self) -> tuple[str, ...]:
        return tuple(self._fired)

    def due(self, event_type: str, scope: Record) -> Record | None:
        if not self._event_validator.is_valid(event_type):
            raise ScriptError("invalid_hook_event", "tipo de evento de hook inválido")
        if not self._scope_validator.is_valid(scope):
            raise ScriptError("invalid_hook_scope", "escopo de hook inválido")
        for hook in self._hooks:
            if hook["hook_id"] not in self._fired and hook["event_type"] == event_type and hook["scope"] == scope:
                self._fired.append(hook["hook_id"])
                return copy.deepcopy(hook)
        return None


@dataclass
class _Call:
    intent: DispatchIntent
    workspace: WorkspaceHandle
    behavior: Record
    fingerprint: Record
    ack: DispatchAck
    external_call_id: str | None
    started_at: str
    executed: bool
    terminal: str | None = None
    failure: Record | None = None
    result: ExecutorResult | None = None
    result_record: Record | None = None


class SimulatedExecutor:
    """Executor roteirizado.

    ``clock`` é obrigatório e deve ser o mesmo objeto do journal do store.
    Estado durável: ``state`` (registro de ``initial_state``/``export_state``) e ``persist``
    (chamado com o novo registro antes de cada operação retornar) andam juntos. Só com estado
    durável e ``reconcile_not_found_authoritative`` uma chave desconhecida vira ``not_found``
    autoritativo; sem isso, chave desconhecida é ``unknown``.
    """

    adapter_version = "0.1"

    def __init__(self, script: SimulationScript, store: ArtifactStore, *, clock: DeterministicClock,
                 persist: Callable[[Record], None] | None = None, state: Record | None = None):
        if not isinstance(clock, DeterministicClock):
            raise ExecutorError("invalid_clock", "DeterministicClock injetado é obrigatório")
        if store.journal.clock is not clock:
            raise ExecutorError("clock_mismatch", "executor e journal devem compartilhar o mesmo relógio")
        if (persist is None) != (state is None) or (persist is not None and not callable(persist)):
            raise _invalid_state("persist (chamável) e state são fornecidos juntos")
        self.script = script
        self.store = store
        self.clock = clock
        self._persist_callback = persist
        self._calls: dict[str, _Call] = {}
        if state is not None:
            self._calls = self._restore(state)
            self._persist()

    @staticmethod
    def initial_state(script: SimulationScript, run_id: str) -> Record:
        """Registro vazio, persistido pelo construtor antes de declarar o executor durável."""
        return _seal_state({
            "schema_version": "0.1", "kind": EXECUTOR_STATE_KIND, "run_id": run_id,
            "script_ref": script.reference, "calls": {},
        })

    @property
    def durable(self) -> bool:
        return self._persist_callback is not None

    @property
    def dispatch_count(self) -> int:
        return len(self._calls)

    @property
    def execution_count(self) -> int:
        return sum(call.executed for call in self._calls.values())

    def export_state(self) -> Record:
        """Registro JSON serializável, com state_hash JCS, suficiente para reconstruir o executor."""
        return _seal_state({
            "schema_version": "0.1", "kind": EXECUTOR_STATE_KIND, "run_id": self.store.journal.run_id,
            "script_ref": self.script.reference,
            "calls": {key: self._call_record(call) for key, call in sorted(self._calls.items())},
        })

    @staticmethod
    def _call_record(call: _Call) -> Record:
        result = call.result_record
        if call.result is not None:
            r = call.result
            result = {
                "produced_files": [{
                    "output_id": f.output_id, "artifact_type": f.artifact_type,
                    "content_contract_ref": f.content_contract_ref, "declared_digest": f.declared_digest,
                    "storage_locator": f.storage_locator,
                } for f in r.produced_files],
                "usage": list(r.usage), "errors": list(r.errors),
                "requested_model_id": r.requested_model_id, "resolved_model_id": r.resolved_model_id,
                "provider_context_coverage": r.provider_context_coverage,
            }
        return copy.deepcopy({
            "intent": call.intent,
            "workspace": {
                "workspace_id": call.workspace.workspace_id, "base_ref": call.workspace.base_ref,
                "expected_output": list(call.workspace.expected_output),
                "requested_model_id": call.workspace.requested_model_id,
            },
            "fingerprint": call.fingerprint,
            "ack": {
                "acknowledged": call.ack.acknowledged, "external_call_id": call.ack.external_call_id,
                "observed_at": call.ack.observed_at, "errors": list(call.ack.errors),
            },
            "external_call_id": call.external_call_id, "started_at": call.started_at,
            "executed": call.executed, "terminal": call.terminal, "failure": call.failure, "result": result,
        })

    def _restore(self, state: Record) -> dict[str, _Call]:
        try:
            if not isinstance(state, dict) or set(state) != _STATE_FIELDS:
                raise _invalid_state("campos do estado divergentes")
            if canonical_digest({k: v for k, v in state.items() if k != "state_hash"}) != state["state_hash"]:
                raise ExecutorError("executor_state_digest_mismatch", "estado persistido diverge do state_hash")
            if state["schema_version"] != "0.1" or state["kind"] != EXECUTOR_STATE_KIND or not isinstance(
                state["calls"], dict
            ):
                raise _invalid_state("versão ou tipo do estado inválido")
            if state["run_id"] != self.store.journal.run_id:
                raise ExecutorError("run_mismatch", "estado pertence a outro run")
            if state["script_ref"] != self.script.reference:
                raise ExecutorError("script_mismatch", "estado gerado por outro roteiro")
            return {key: self._call_from_record(key, raw) for key, raw in state["calls"].items()}
        except ExecutorError:
            raise
        except (KeyError, TypeError, AttributeError, ValueError, CanonicalizationError) as error:
            raise _invalid_state("estado persistido malformado") from error

    def _call_from_record(self, key: str, raw: Record) -> _Call:
        if not isinstance(raw, dict) or set(raw) != _CALL_FIELDS:
            raise _invalid_state("campos da chamada divergentes")
        intent = copy.deepcopy(raw["intent"])
        self._check_intent(intent)
        if intent["dispatch_idempotency_key"] != key or intent["run_id"] != self.store.journal.run_id:
            raise _invalid_state("chave ou run da chamada divergente")
        ws = raw["workspace"]
        if not isinstance(ws, dict) or set(ws) != _WORKSPACE_FIELDS:
            raise _invalid_state("workspace divergente")
        workspace = WorkspaceHandle(ws["workspace_id"], copy.deepcopy(ws["base_ref"]),
                                    tuple(copy.deepcopy(ws["expected_output"])), ws["requested_model_id"])
        try:
            self._check_workspace(intent, workspace)
        except ExecutorError as error:
            raise _invalid_state("workspace inválido no estado") from error
        fingerprint = self._fingerprint(intent, workspace)
        if fingerprint != raw["fingerprint"]:
            raise _invalid_state("fingerprint da chamada divergente")
        behavior = self.script.for_attempt(intent["task_id"], intent["attempt_number"])
        executed = behavior["dispatch"] not in _NOT_EXECUTED
        external_id = stable_id("sim-call", key) if executed else None
        if raw["executed"] is not executed or raw["external_call_id"] != external_id or raw["terminal"] not in _TERMINALS:
            raise _invalid_state("execução, ID externo ou término incompatível com o roteiro")
        try:
            started = parse_utc(raw["started_at"])
        except ExecutorError as error:
            raise _invalid_state("started_at inválido") from error
        ack_raw = raw["ack"]
        mode = behavior["dispatch"]
        expected_ack = {
            "acknowledged": mode == "ack", "external_call_id": external_id if mode == "ack" else None,
            "observed_at": raw["started_at"], "errors": [],
        }
        if mode == "reject_confirmed":
            expected_ack["errors"] = [self._error(intent, "dispatch", "dispatch_failed", "transport",
                                                 raw["started_at"], "retryable", external_id)]
        if ack_raw != expected_ack or type(ack_raw["acknowledged"]) is not bool:
            raise _invalid_state("ack incompatível com o despacho")
        ack = DispatchAck(ack_raw["acknowledged"], ack_raw["external_call_id"], ack_raw["observed_at"],
                          tuple(copy.deepcopy(ack_raw["errors"])))
        terminal, failure = raw["terminal"], raw["failure"]
        if (not executed and terminal is not None) or (terminal == "failed") != (failure is not None):
            raise _invalid_state("término ou falha sem execução compatível")
        allowed_terminal = {
            None: True,
            "finished": behavior["execution"] == "finish" or behavior["reconcile"] == "found_finished",
            "failed": behavior["execution"].startswith("fail_") or behavior["reconcile"] == "found_failed",
            "stopped": behavior["on_cancel"] == "confirmed" or behavior["reconcile"] == "found_stopped",
        }
        if not allowed_terminal[terminal]:
            raise _invalid_state("término contradiz o roteiro")
        if failure is not None:
            try:
                failure_time = parse_utc(failure["occurred_at"])
            except ExecutorError as error:
                raise _invalid_state("instante da falha inválido") from error
            retryability = {"fail_retryable": "retryable", "fail_non_retryable": "non_retryable"}.get(
                behavior["execution"], "unknown")
            if failure_time < started or failure != self._error(
                intent, "observe", "execution_failed", "provider", failure["occurred_at"], retryability, external_id
            ):
                raise _invalid_state("ErrorRecord incompatível com a falha")
        call = _Call(intent, workspace, behavior, fingerprint, ack, external_id, raw["started_at"], executed,
                     terminal, copy.deepcopy(failure), result_record=copy.deepcopy(raw["result"]))
        if call.result_record is not None:
            self._validate_result_record(call)
        return call

    def _validate_result_record(self, call: _Call) -> None:
        """Valida metadados sem reler bytes; corrupção do store pertence à avaliação."""
        result = call.result_record
        if not call.executed or call.terminal is None or not isinstance(result, dict) or set(result) != {
            "produced_files", "usage", "errors", "requested_model_id", "resolved_model_id", "provider_context_coverage",
        }:
            raise _invalid_state("resultado sem término ou campos inválidos")
        if result["errors"] != ([call.failure] if call.failure else []) or (
            result["requested_model_id"] != call.workspace.requested_model_id
            or result["resolved_model_id"] != call.behavior["resolved_model_id"]
            or result["provider_context_coverage"] != "platform_only"
        ):
            raise _invalid_state("resultado diverge da chamada")
        usage = result["usage"]
        if not isinstance(usage, list) or len(usage) != 1 or validate_instance(usage[0], "usage_record") or (
            usage[0]["run_id"] != call.intent["run_id"] or usage[0]["scope_type"] != "attempt"
            or usage[0]["scope_ref"] != call.intent["attempt_id"]
            or usage[0]["record_hash"] != canonical_digest({k: v for k, v in usage[0].items() if k != "record_hash"})
        ):
            raise _invalid_state("uso inválido no resultado")
        files = result["produced_files"]
        if not isinstance(files, list) or (call.terminal != "finished" and files):
            raise _invalid_state("arquivos sem término bem-sucedido")
        outputs = {output["output_id"]: output for output in call.workspace.expected_output}
        seen = set()
        common = validator_for("common")
        for file in files:
            if not isinstance(file, dict) or set(file) != {
                "output_id", "artifact_type", "content_contract_ref", "declared_digest", "storage_locator",
            } or not isinstance(file["output_id"], str) or file["output_id"] not in outputs or file["output_id"] in seen:
                raise _invalid_state("arquivo inválido no resultado")
            seen.add(file["output_id"])
            if file["artifact_type"] != outputs[file["output_id"]]["artifact_type"] or not all(
                common.evolve(schema={"$ref": _SCHEMA_BASE + "common.schema.json#/$defs/" + definition}).is_valid(file[field])
                for field, definition in (("content_contract_ref", "versionedRef"), ("declared_digest", "digest"))
            ) or not validator_for("journal_event").evolve(schema={
                "$ref": _SCHEMA_BASE + "journal-event.schema.json#/$defs/artifact_ingested/properties/locator"
            }).is_valid(file["storage_locator"]):
                raise _invalid_state("metadados de arquivo inválidos")

    def _read_result(self, record: Record) -> ExecutorResult:
        try:
            files = tuple(ProducedFile(
                file["output_id"], self.store.read(file["storage_locator"]), file["artifact_type"],
                copy.deepcopy(file["content_contract_ref"]), copy.deepcopy(file["declared_digest"]), file["storage_locator"],
            ) for file in record["produced_files"])
        except ArtifactStoreError as error:
            raise ExecutorError("executor_state_artifact_unavailable", "artefato coletado ilegível") from error
        return ExecutorResult(files, tuple(copy.deepcopy(record["usage"])), tuple(copy.deepcopy(record["errors"])),
                              record["requested_model_id"], record["resolved_model_id"], record["provider_context_coverage"])

    @contextmanager
    def _update(self, call: _Call):
        """Só confirma mutações após persist; o relógio compartilhado permanece monotônico."""
        previous = call.terminal, call.failure, call.result
        try:
            yield
            self._persist()
        except Exception:
            call.terminal, call.failure, call.result = previous
            raise

    def _persist(self) -> None:
        if self._persist_callback is None:
            return
        record = self.export_state()
        try:
            self._persist_callback(record)
        except Exception as error:
            # Sem registro durável, o executor deixa de afirmar ausência (not_found autoritativo).
            self._persist_callback = None
            raise ExecutorError("executor_state_unavailable", "estado do executor não persistido") from error

    def describe(self) -> ExecutorDescription:
        reference = self.script.reference
        authoritative = self.script.authoritative_not_found and self.durable
        controls = [{"id": name, "support": "supported", "evidence_refs": [reference]} for name in CONTROL_IDS]
        controls.append({
            "id": AUTHORITATIVE_NOT_FOUND_CONTROL,
            "support": "supported" if authoritative else "unsupported",
            "evidence_refs": [reference] if authoritative else [],
        })
        declaration = validator_for("routing_target_catalog").evolve(
            schema={"$ref": _SCHEMA_BASE + "routing-target-catalog.schema.json#/$defs/declaration"})
        if any(not declaration.is_valid(control) for control in controls):
            raise ExecutorError("invalid_capability_declaration", "declaração fora do RoutingTargetCatalog")
        ref = {"id": "executor-simulated", "version": "0.1",
               "content_digest": canonical_digest({"adapter": "simulated", "version": "0.1"})}
        runtime = {"id": "runtime-simulated", "version": "0.1",
                   "content_digest": canonical_digest({"runtime": "simulated", "version": "0.1"})}
        return ExecutorDescription(ref, self.adapter_version, runtime, "0.1", tuple(controls),
                                   ("cost", "resolved_model_id", "provider_context_coverage"))

    @staticmethod
    def _check_intent(intent: DispatchIntent) -> None:
        issues = validate_instance(intent, "dispatch_intent")
        if issues or canonical_digest({k: v for k, v in intent.items() if k != "record_hash"}) != intent.get("record_hash"):
            raise ExecutorError("invalid_dispatch_intent", "intenção inválida ou digest divergente")

    @staticmethod
    def _fingerprint(intent: DispatchIntent, workspace: WorkspaceHandle) -> Record:
        return canonical_digest([intent, workspace.workspace_id, workspace.base_ref,
                                 list(workspace.expected_output), workspace.requested_model_id])

    @staticmethod
    def _check_workspace(intent: DispatchIntent, workspace: WorkspaceHandle) -> None:
        if workspace.base_ref != intent["workspace_base_ref"]:
            raise ExecutorError("workspace_mismatch", "base do workspace diverge da intenção")
        if workspace.requested_model_id is not None and (
            not isinstance(workspace.requested_model_id, str) or not 1 <= len(workspace.requested_model_id) <= 4096
        ):
            raise ExecutorError("invalid_workspace", "requested_model_id deve ser texto ou null")
        outputs = workspace.expected_output
        output_validator = validator_for("common").evolve(
            schema={"$ref": _SCHEMA_BASE + "common.schema.json#/$defs/outputSpec"})
        if not outputs or any(not output_validator.is_valid(output) for output in outputs) or len(
            {output["output_id"] for output in outputs}
        ) != len(outputs):
            raise ExecutorError("invalid_output_spec", "outputs devem ser válidos, não vazios e únicos")
        contract, _ = simulated_output_contract()
        if any(output["content_contract_ref"] != contract for output in outputs):
            raise ExecutorError("unsupported_content_contract", "simulado só produz o envelope simulated-output")

    def dispatch(self, intent: DispatchIntent, bundle: bytes, workspace: WorkspaceHandle) -> DispatchAck:
        self._check_intent(intent)
        if not isinstance(bundle, bytes) or bytes_digest(bundle) != intent["bundle_hash"]:
            raise ExecutorError("bundle_digest_mismatch", "bundle recebido diverge da intenção")
        if intent["run_id"] != self.store.journal.run_id:
            raise ExecutorError("run_mismatch", "store pertence a outro run")
        self._check_workspace(intent, workspace)
        outputs = workspace.expected_output
        fingerprint = self._fingerprint(intent, workspace)
        key = intent["dispatch_idempotency_key"]
        if key in self._calls:
            if self._calls[key].fingerprint != fingerprint:
                raise ExecutorError("idempotency_conflict", "mesma chave com entradas diferentes")
            return copy.deepcopy(self._calls[key].ack)
        behavior = self.script.for_attempt(intent["task_id"], intent["attempt_number"])
        if isinstance(behavior["outputs"], dict) and not set(behavior["outputs"]).issubset(
            output["output_id"] for output in outputs
        ):
            raise ScriptError("unknown_output_id", "roteiro altera output não declarado")
        now = self.clock()
        mode = behavior["dispatch"]
        executed = mode not in _NOT_EXECUTED
        external_id = stable_id("sim-call", key) if executed else None
        errors = ()
        if mode == "reject_confirmed":
            errors = (self._error(intent, "dispatch", "dispatch_failed", "transport", now,
                                  "retryable", external_id),)
        ack = DispatchAck(mode == "ack", external_id if mode == "ack" else None, now, errors)
        self._calls[key] = _Call(copy.deepcopy(intent), copy.deepcopy(workspace), behavior, fingerprint,
                                 ack, external_id, now, executed)
        try:
            self._persist()
        except ExecutorError:
            # Escrita antecipada: sem estado persistido, o efeito simulado não é efetivado.
            del self._calls[key]
            raise
        return copy.deepcopy(ack)

    def _get_call(self, intent: DispatchIntent) -> _Call:
        call = self._calls.get(intent["dispatch_idempotency_key"])
        if call is None:
            raise ExecutorError("dispatch_not_known", "intenção não despachada neste executor")
        if intent != call.intent:
            raise ExecutorError("idempotency_conflict", "intenção diverge do despacho original")
        return call

    @staticmethod
    def _error(intent: DispatchIntent, phase: str, code: str, category: str,
               now: str, retryability: str, external_id: str | None) -> Record:
        return error_record(run_id=intent["run_id"], scope_type="attempt", scope_ref=intent["attempt_id"],
                            phase=phase, category=category, code=code, message="Falha simulada: " + code,
                            timestamp=now, retryability=retryability, termination_certainty="confirmed",
                            external_call_id=external_id)

    def _fail_call(self, call: _Call, now: str) -> None:
        call.terminal = "failed"
        if call.failure is None:
            retryability = {"fail_retryable": "retryable", "fail_non_retryable": "non_retryable"}.get(
                call.behavior["execution"], "unknown")
            call.failure = self._error(call.intent, "observe", "execution_failed", "provider", now,
                                       retryability, call.external_call_id)

    @staticmethod
    def _finish_at(call: _Call) -> datetime:
        try:
            return parse_utc(call.started_at) + timedelta(milliseconds=call.behavior["duration_ms"])
        except OverflowError as error:
            raise ScriptError("invalid_script", "duration_ms excede intervalo UTC") from error

    def _settle(self, call: _Call, instant: str) -> None:
        """Aplica o término roteirizado se a duração já passou no instante dado."""
        if not call.executed or call.terminal is not None or call.behavior["execution"] == "hang":
            return
        if self._finish_at(call) <= parse_utc(instant):
            if call.behavior["execution"] == "finish":
                call.terminal = "finished"
            else:
                self._fail_call(call, instant)

    @staticmethod
    def _observation(call: _Call, now: str) -> ExecutionObservation:
        if not call.executed:
            return ExecutionObservation("unknown", "unknown", now)
        if call.terminal == "stopped":
            return ExecutionObservation("stopped", "confirmed", now)
        if call.terminal:
            return ExecutionObservation(call.terminal, "confirmed", now,
                                        (copy.deepcopy(call.failure),) if call.failure else ())
        return ExecutionObservation("running", "not_terminated", now)

    def observe(self, intent: DispatchIntent, until: str) -> ExecutionObservation:
        """Prazo já vencido (until <= relógio) devolve a observação no instante atual, sem avançar."""
        call = self._get_call(intent)
        limit = max(parse_utc(until), parse_utc(self.clock.now()))
        if call.executed and call.terminal is None and call.behavior["execution"] != "hang":
            self.clock.advance_to(format_utc(min(limit, self._finish_at(call))))
        elif call.terminal is None:
            self.clock.advance_to(format_utc(limit))
        now = self.clock.now()
        with self._update(call):
            self._settle(call, now)
        return self._observation(call, now)

    def cancel(self, intent: DispatchIntent, reason: str) -> CancelObservation:
        call = self._get_call(intent)
        now = self.clock()
        # Duração já decorrida sem observe: o término roteirizado prevalece sobre a parada.
        certainty = "unknown"
        with self._update(call):
            self._settle(call, now)
            if call.executed and (call.terminal or call.behavior["on_cancel"] == "confirmed"):
                call.terminal = call.terminal or "stopped"
                certainty = "confirmed"
        return CancelObservation(certainty, now)

    def reconcile(self, dispatch_idempotency_key: str,
                  external_call_id: str | None) -> ReconciliationObservation:
        call = self._calls.get(dispatch_idempotency_key)
        if call and external_call_id is not None and external_call_id != call.external_call_id:
            raise ExecutorError("external_call_mismatch", "ID externo diverge da intenção")
        now = self.clock()
        can_deny = self.script.authoritative_not_found and self.durable
        if call is None:
            # Chave desconhecida só é ausência comprovada com estado durável e controle declarado.
            if can_deny:
                return ReconciliationObservation("not_found", None, "confirmed", now, (self.script.reference,), True)
            return ReconciliationObservation("unknown", None, "unknown", now)
        outcome = call.behavior["reconcile"]
        if call.executed and call.terminal:
            outcome = {"finished": "found_finished", "failed": "found_failed", "stopped": "found_stopped"}[call.terminal]
        if (outcome.startswith("found_") and not call.executed) or (outcome == "not_found" and call.executed):
            raise ScriptError("invalid_script", "roteiro contraditório")  # Recusado na carga; defesa.
        authoritative = outcome == "not_found" and can_deny
        certainty = "unknown"
        with self._update(call):
            if outcome in {"found_finished", "found_failed", "found_stopped"}:
                certainty = "confirmed"
                if outcome == "found_failed":
                    self._fail_call(call, now)
                else:
                    call.terminal = "finished" if outcome == "found_finished" else "stopped"
            elif outcome == "found_running":
                certainty = "not_terminated"
            elif authoritative:
                certainty = "confirmed"
        return ReconciliationObservation(
            outcome, call.external_call_id, certainty, now,
            (self.script.reference,) if outcome != "unknown" else (), authoritative,
            (copy.deepcopy(call.failure),) if outcome == "found_failed" and call.failure else (),
        )

    def collect(self, intent: DispatchIntent) -> ExecutorResult:
        call = self._get_call(intent)
        if call.result is not None:
            return copy.deepcopy(call.result)
        if call.result_record is not None:
            return self._read_result(call.result_record)
        with self._update(call):
            result = self._collect(call)
        return result

    def _collect(self, call: _Call) -> ExecutorResult:
        intent = call.intent
        self._settle(call, self.clock.now())
        if not call.executed or call.terminal is None:
            raise ExecutorError("result_unavailable", "execução sem término confirmado")
        now = self.clock()
        produced = []
        envelope_validator = validator_for("simulated_output")
        if call.terminal == "finished":
            for output in call.workspace.expected_output:
                mode = call.behavior["outputs"].get(output["output_id"]) if isinstance(call.behavior["outputs"], dict) else None
                if mode == "omit":
                    continue
                contract = copy.deepcopy(output["content_contract_ref"])
                if mode == "wrong_contract":
                    contract["content_digest"]["value"] = self._wrong_digest(contract["content_digest"]["value"])
                envelope = {
                    "schema_version": "0.1", "task_id": intent["task_id"],
                    "attempt_number": intent["attempt_number"], "output_id": output["output_id"],
                    "artifact_type": output["artifact_type"], "content_contract_ref": contract,
                    "input_bindings": intent["input_bindings"], "bundle_hash": intent["bundle_hash"],
                }
                if not envelope_validator.is_valid(envelope):
                    raise ExecutorError("invalid_simulated_output", "envelope fora do schema simulated-output")
                content = canonicalize(envelope)
                declared = bytes_digest(content)
                if mode == "declared_digest_mismatch":
                    declared["value"] = self._wrong_digest(declared["value"])
                produced.append(ProducedFile(output["output_id"], content, output["artifact_type"], contract,
                                             declared, self.store.put(content)))
        cost = call.behavior["usage"]["cost"]
        coverage = {status: ["cost"] if cost["status"] == status else []
                    for status in ("reported", "estimated", "unavailable")}
        usage = seal_record({
            "schema_version": "0.1", "usage_id": stable_id("usage", intent["dispatch_idempotency_key"]),
            "run_id": intent["run_id"], "scope_type": "attempt", "scope_ref": intent["attempt_id"],
            "provider_ref": None, "observed_at": now, "recorded_at": now, "measurements": [],
            "cost_lines": [{**cost, "pricing_ref": None, "method": "simulated_" + cost["status"]}],
            "coverage": coverage, "supersedes_usage_id": None,
        }, "usage_record")
        call.result = ExecutorResult(tuple(produced), (usage,), (call.failure,) if call.failure else (),
                                     call.workspace.requested_model_id, call.behavior["resolved_model_id"],
                                     "platform_only")
        return copy.deepcopy(call.result)

    @staticmethod
    def _wrong_digest(value: str) -> str:
        return ("1" if value[0] == "0" else "0") + value[1:]
