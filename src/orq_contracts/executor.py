"""Interface síncrona de executor; observações não decidem estados ou retry."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from .canonical import canonical_digest
from .validator import validate_instance


Record: TypeAlias = dict[str, Any]
DispatchIntent: TypeAlias = Record
TerminationCertainty: TypeAlias = Literal["confirmed", "not_terminated", "unknown"]


class ExecutorError(ValueError):
    """Erro de integração, com código estável e certeza explícita."""

    def __init__(self, code: str, message: str, *, retryability: str = "unknown",
                 termination_certainty: TerminationCertainty = "unknown"):
        self.code = code
        self.retryability = retryability
        self.termination_certainty = termination_certainty
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class WorkspaceHandle:
    """Metadados fornecidos pelo runner; não concede acesso ao filesystem.

    requested_model_id é copiado de execution_target_snapshot.model_ref.id.
    expected_output preserva os OutputSpecs da tarefa, sem ler o bundle.
    """

    workspace_id: str
    base_ref: Record
    expected_output: tuple[Record, ...]
    requested_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutorDescription:
    """controls usa a forma de declaração do RoutingTargetCatalog: {id, support, evidence_refs}."""

    executor_ref: Record
    adapter_version: str
    runtime_ref: Record
    runtime_version: str
    controls: tuple[Record, ...]
    expected_telemetry: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DispatchAck:
    acknowledged: bool
    external_call_id: str | None
    observed_at: str
    errors: tuple[Record, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    """status ``stopped``: parada confirmada sem resultado utilizável.

    Como em ``found_stopped``, o runner decide entre timed_out, cancelled e failed
    pelo pedido de parada anterior; ``stopped`` sempre vem com certeza ``confirmed``.
    """

    status: Literal["running", "finished", "failed", "stopped", "unknown"]
    termination_certainty: TerminationCertainty
    observed_at: str
    errors: tuple[Record, ...] = ()


@dataclass(frozen=True, slots=True)
class CancelObservation:
    termination_certainty: TerminationCertainty
    observed_at: str


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    outcome: Literal["found_running", "found_finished", "found_failed", "found_stopped", "not_found", "unknown"]
    external_call_id: str | None
    termination_certainty: TerminationCertainty
    observed_at: str
    evidence_refs: tuple[Record, ...] = ()
    authoritative: bool = False
    errors: tuple[Record, ...] = ()


@dataclass(frozen=True, slots=True)
class ProducedFile:
    output_id: str
    content: bytes
    artifact_type: str
    content_contract_ref: Record
    declared_digest: Record
    storage_locator: str


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    produced_files: tuple[ProducedFile, ...]
    usage: tuple[Record, ...]
    errors: tuple[Record, ...]
    requested_model_id: str | None
    resolved_model_id: str | None
    provider_context_coverage: Literal["platform_only", "unknown"]


@runtime_checkable
class ExecutorAdapter(Protocol):
    def describe(self) -> ExecutorDescription: ...
    def dispatch(self, intent: DispatchIntent, bundle: bytes, workspace: WorkspaceHandle) -> DispatchAck: ...
    def observe(self, intent: DispatchIntent, until: str) -> ExecutionObservation: ...
    def cancel(self, intent: DispatchIntent, reason: str) -> CancelObservation: ...
    def reconcile(self, dispatch_idempotency_key: str,
                  external_call_id: str | None) -> ReconciliationObservation: ...
    def collect(self, intent: DispatchIntent) -> ExecutorResult: ...


def bytes_digest(content: bytes) -> Record:
    return {"algorithm": "sha256", "value": hashlib.sha256(content).hexdigest()}


def stable_id(prefix: str, value: Any) -> str:
    """ID portável mesmo quando IDs de entrada já têm 128 caracteres."""
    return prefix + "-" + canonical_digest(value)["value"]


def seal_record(record: Record, schema_name: str) -> Record:
    record["record_hash"] = canonical_digest({k: v for k, v in record.items() if k != "record_hash"})
    issues = validate_instance(record, schema_name)
    if issues:
        raise ExecutorError("invalid_record", f"{schema_name}: {issues[0].path}: {issues[0].code}")
    return record


def error_record(*, run_id: str, scope_type: str, scope_ref: str, phase: str,
                 category: str, code: str, message: str, timestamp: str,
                 retryability: str, termination_certainty: TerminationCertainty,
                 external_call_id: str | None = None, discriminator: Any = None) -> Record:
    return seal_record({
        "schema_version": "0.1",
        "error_id": stable_id("error", [run_id, scope_type, scope_ref, phase, code, discriminator]),
        "run_id": run_id, "scope_type": scope_type, "scope_ref": scope_ref,
        "phase": phase, "category": category, "code": code, "message": message,
        "occurred_at": timestamp, "recorded_at": timestamp,
        "retryability": retryability, "termination_certainty": termination_certainty,
        "external_call_ref": external_call_id, "cause_error_refs": [], "diagnostic_artifact_refs": [],
    }, "error_record")
