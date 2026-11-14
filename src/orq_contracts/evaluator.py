"""Avaliação local por métodos fechados, ligada aos bytes dos subject_bindings."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator, SchemaError
from referencing import Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from .artifact_store import LOCATOR_PREFIX, ArtifactStore, ArtifactStoreError
from .canonical import canonical_digest, canonicalize
from .executor import Record, ExecutorError, error_record, seal_record
from .schemas import schema_registry, validator_for
from .simulated_executor import DeterministicClock, SimulationScript
from .validator import validate_instance


SUPPORTED_METHODS = (
    "output_binding_present", "digest_matches", "content_contract_valid", "simulated_verdict",
)
# Falhas do store que significam bytes ausentes ou corrompidos; o store é imutável.
_INTEGRITY_STORE_CODES = frozenset({"artifact_digest_mismatch", "artifact_not_found", "artifact_too_large"})
_DESCRIPTOR_SCHEMA = "https://orq.local/schemas/v0.1/journal-event.schema.json#/$defs/artifact_ingested"


class EvaluationError(ExecutorError):
    """Entrada inválida ou falha de integração do avaliador local."""


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    """Descritor ingerido: o mesmo payload gravado no evento ``artifact_ingested``.

    O avaliador não aceita locator ou contrato soltos do chamador. Antes dos critérios,
    confere ``artifact_id``, ``content_digest`` e locator do descritor contra o binding.
    """

    descriptor: Record

    def __post_init__(self) -> None:
        validator = validator_for("journal_event").evolve(schema={"$ref": _DESCRIPTOR_SCHEMA})
        if not isinstance(self.descriptor, dict) or not validator.is_valid(self.descriptor):
            raise EvaluationError("invalid_artifact_descriptor", "descritor fora do payload artifact_ingested")
        object.__setattr__(self, "descriptor", copy.deepcopy(self.descriptor))

    @property
    def artifact_id(self) -> str:
        return self.descriptor["artifact_id"]

    @property
    def content_digest(self) -> Record:
        return copy.deepcopy(self.descriptor["content_digest"])

    @property
    def storage_locator(self) -> str:
        return self.descriptor["locator"]

    @property
    def content_contract_ref(self) -> Record:
        return copy.deepcopy(self.descriptor["content_contract_ref"])


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    report: Record | None
    errors: tuple[Record, ...]
    termination_certainty: str
    observed_at: str


class ContentContractRegistry:
    """Somente schemas locais explícitos, identificados por ID, versão e digest.

    O Registry de referencing não tem callback de recuperação remota.
    Uma referência desconhecida nunca é aprovada por heurística.
    ``format`` é só anotação (sem FormatChecker), para o resultado não depender
    dos pacotes opcionais instalados em cada máquina.
    """

    def __init__(self) -> None:
        self._schemas: dict[bytes, Record] = {}

    @staticmethod
    def _key(reference: Record) -> bytes:
        return canonicalize(reference)

    def register(self, reference: Record, schema: Record) -> None:
        ref_validator = validator_for("common").evolve(schema={
            "$ref": "https://orq.local/schemas/v0.1/common.schema.json#/$defs/versionedRef"})
        if not ref_validator.is_valid(reference):
            raise EvaluationError("invalid_content_contract", "referência de contrato inválida")
        if not isinstance(schema, dict):
            raise EvaluationError("invalid_content_contract", "schema local deve ser objeto JSON")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise EvaluationError("invalid_content_contract", "schema de conteúdo inválido") from error
        if canonical_digest(schema) != reference["content_digest"]:
            raise EvaluationError("content_contract_digest_mismatch", "schema diverge da referência")
        for existing in self._schemas.values():
            if schema.get("$id") and existing.get("$id") == schema["$id"] and existing != schema:
                raise EvaluationError("content_contract_conflict", "mesmo $id com schemas diferentes")
        self._schemas[self._key(reference)] = copy.deepcopy(schema)

    def validator(self, reference: Record) -> Draft202012Validator | None:
        schema = self._schemas.get(self._key(reference))
        if schema is None:
            return None
        registry = schema_registry().with_resources(
            (item["$id"], Resource.from_contents(item, default_specification=DRAFT202012))
            for item in self._schemas.values() if "$id" in item
        )
        return Draft202012Validator(schema, registry=registry)


def _json_object(pairs: list[tuple[str, Any]]) -> Record:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("chave JSON duplicada")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("constante JSON inválida")


class DeterministicEvaluator:
    """``clock`` é obrigatório e deve ser o mesmo objeto do journal do store."""

    def __init__(self, store: ArtifactStore, script: SimulationScript, *, clock: DeterministicClock,
                 contracts: ContentContractRegistry | None = None):
        if not isinstance(clock, DeterministicClock):
            raise EvaluationError("invalid_clock", "DeterministicClock injetado é obrigatório")
        if store.journal.clock is not clock:
            raise EvaluationError("clock_mismatch", "avaliador e journal devem compartilhar o mesmo relógio")
        self.store = store
        self.script = script
        self.contracts = contracts if contracts is not None else ContentContractRegistry()
        self.clock = clock

    @property
    def snapshot(self) -> Record:
        identity = {"kind": "deterministic", "version": "0.1", "supported_methods": list(SUPPORTED_METHODS)}
        return {**identity, "evaluator_ref": {
            "id": "evaluator-deterministic", "version": "0.1", "content_digest": canonical_digest(identity),
        }}

    def evaluate(self, *, run_id: str, evaluation_id: str, evaluation_number: int,
                 scope: Record, subject_bindings: Sequence[Record], criteria: Sequence[Record],
                 artifacts: Mapping[str, ArtifactEvidence], attempt_number: int | None = None) -> EvaluationResult:
        """IDs/escopo são alocados pelo chamador em evaluation_requested.

        attempt_number é obrigatório no escopo task_attempt, sem inferência pelo ID.
        workflow usa os bindings finais já aceitos, mapeados pelo runner.
        ``artifacts`` mapeia artifact_id para o descritor ingerido de cada binding.
        Todo binding é relido e conferido antes dos critérios; divergência ou ausência
        gera ``fail`` com ErrorRecord ``integrity_mismatch``.
        unknown_termination devolve report=None: o runner mantém a reconciliação.
        """
        report = {
            "schema_version": "0.1", "evaluation_id": evaluation_id, "evaluation_number": evaluation_number,
            "run_id": run_id, "scope": copy.deepcopy(scope), "subject_bindings": copy.deepcopy(list(subject_bindings)),
            "patch_application_refs": [], "criteria_snapshot": {
                "criteria": copy.deepcopy(list(criteria)), "criteria_hash": canonical_digest(criteria),
            },
            "evaluator_snapshot": self.snapshot, "context_manifest_ref": None,
            "started_at": self.clock.now(), "completed_at": self.clock.now(), "execution_status": "completed",
            "criterion_results": [{
                "criterion_id": "preflight", "required": True, "requirement_ids": ["preflight"],
                "method": "simulated_verdict", "status": "not_run", "evidence_refs": [],
                "observations": [], "error_record_ids": [],
            }],
            "verdict": "inconclusive", "evidence_refs": [], "usage_record_ids": [], "error_record_ids": [],
            "limitations": [], "unknowns": [],
        }
        report["record_hash"] = canonical_digest(report)
        issues = validate_instance(report, "evaluation_report")
        if issues:
            raise EvaluationError("invalid_evaluation_request", f"{issues[0].path}: {issues[0].code}")
        if run_id != self.store.journal.run_id:
            raise EvaluationError("run_mismatch", "store pertence a outro run")
        if len({b["output_id"] for b in subject_bindings}) != len(subject_bindings) or len(
            {c["criterion_id"] for c in criteria}
        ) != len(criteria):
            raise EvaluationError("invalid_evaluation_request", "IDs de outputs/critérios devem ser únicos")
        if scope["kind"] == "task_attempt" and (type(attempt_number) is not int or attempt_number < 1):
            raise EvaluationError("invalid_evaluation_request", "attempt_number obrigatório para tarefa")
        # Resolve o roteiro antes de I/O ou de consumir o relógio.
        scripted = {}
        for criterion in criteria:
            if criterion["method"] == "simulated_verdict":
                scripted[criterion["criterion_id"]] = self.script.verdict(
                    criterion["pass_rule"]["script_key"], evaluation_number,
                    task_id=scope.get("task_id"), attempt_number=attempt_number,
                )
        evidences: dict[str, ArtifactEvidence] = {}
        for binding in subject_bindings:
            evidence = artifacts.get(binding["artifact"]["artifact_id"])
            if not isinstance(evidence, ArtifactEvidence) or evidence.artifact_id != binding["artifact"]["artifact_id"]:
                raise EvaluationError("artifact_descriptor_missing", "binding sem descritor ingerido")
            evidences[binding["output_id"]] = evidence
        contents, broken = self._verify_bindings(subject_bindings, evidences, run_id=run_id, scope=scope)
        started = self.clock()
        if "unknown_termination" in scripted.values():
            return EvaluationResult(None, (), "unknown", started)
        report["started_at"] = started
        report["criterion_results"] = []
        errors = [error_record(
            run_id=run_id, scope_type="evaluation", scope_ref=evaluation_id, phase="evaluation",
            category="integrity", code="integrity_mismatch", message=message, timestamp=started,
            retryability="non_retryable", termination_certainty="confirmed", discriminator=[output_id],
        ) for output_id, message in broken.items()]
        bindings = {binding["output_id"]: binding for binding in subject_bindings}
        integrity_ids = [error["error_id"] for error in errors]
        for criterion in criteria:
            status, observations, evidence, error_ids = self._criterion(
                criterion, bindings, evidences, contents, broken, integrity_ids, scripted,
            )
            report["criterion_results"].append({
                "criterion_id": criterion["criterion_id"], "required": criterion["required"],
                "requirement_ids": list(criterion.get("requirement_ids", criterion.get("workflow_requirement_ids", []))),
                "method": criterion["method"], "status": status, "evidence_refs": evidence,
                "observations": observations, "error_record_ids": error_ids,
            })
            if status == "not_run":
                report["limitations"].append("Critério não executado: " + criterion["criterion_id"])
        required = [result["status"] for result in report["criterion_results"] if result["required"]]
        report["verdict"] = "fail" if "fail" in required else (
            "pass" if all(status == "pass" for status in required) else "inconclusive")
        report["completed_at"] = self.clock()
        report["error_record_ids"] = integrity_ids
        for result in report["criterion_results"]:
            for evidence in result["evidence_refs"]:
                if evidence not in report["evidence_refs"]:
                    report["evidence_refs"].append(evidence)
        seal_record(report, "evaluation_report")
        return EvaluationResult(report, tuple(errors), "confirmed", report["completed_at"])

    def _verify_bindings(self, subject_bindings: Sequence[Record],
                         evidences: Mapping[str, ArtifactEvidence], *,
                         run_id: str, scope: Record) -> tuple[dict[str, bytes], dict[str, str]]:
        """Relê todos os bindings; devolve bytes íntegros e mensagens de integridade por output_id."""
        contents: dict[str, bytes] = {}
        broken: dict[str, str] = {}
        for binding in subject_bindings:
            output_id, artifact = binding["output_id"], binding["artifact"]
            evidence = evidences[output_id]
            if evidence.content_digest != artifact["content_digest"] or (
                evidence.storage_locator != LOCATOR_PREFIX + artifact["content_digest"]["value"]
            ):
                broken[output_id] = "Descritor ingerido diverge do binding."
                continue
            producer = evidence.descriptor["producer"]
            if producer["run_id"] != run_id or (scope["kind"] == "task_attempt" and (
                producer["kind"] != "attempt_output" or producer.get("task_id") != scope["task_id"]
                or producer.get("attempt_id") != scope["attempt_id"]
            )):
                broken[output_id] = "Produtor do artefato diverge do escopo da avaliação."
                continue
            try:
                content = self.store.read(evidence.storage_locator)
                if len(content) != evidence.descriptor["size_bytes"]:
                    broken[output_id] = "Tamanho dos bytes diverge do descritor ingerido."
                else:
                    contents[output_id] = content
            except ArtifactStoreError as error:
                if error.code not in _INTEGRITY_STORE_CODES:
                    raise EvaluationError("artifact_read_failed", "armazenamento recusou a leitura") from error
                broken[output_id] = ("Artefato vinculado ausente no armazenamento imutável."
                                     if error.code == "artifact_not_found" else "Bytes do artefato divergem do binding.")
        return contents, broken

    def _criterion(self, criterion: Record, bindings: Mapping[str, Record],
                   evidences: Mapping[str, ArtifactEvidence], contents: Mapping[str, bytes],
                   broken: Mapping[str, str], integrity_ids: list[str],
                   scripted: Mapping[str, str]) -> tuple[str, list, list, list]:
        method = criterion["method"]
        if method not in SUPPORTED_METHODS:
            return "not_run", ["Método não suportado."], [], []
        if broken:
            # Nenhum veredicto (nem roteirizado) se aplica a bytes não verificados.
            return ("fail", ["Integridade dos subject_bindings falhou: " + ", ".join(broken)],
                    [copy.deepcopy(bindings[output_id]["artifact"]) for output_id in broken], list(integrity_ids))
        if method == "simulated_verdict":
            output_ids = criterion.get("output_ids", ())
            evidence = [copy.deepcopy(bindings[output_id]["artifact"]) for output_id in output_ids if output_id in bindings]
            missing = [output_id for output_id in output_ids if output_id not in bindings]
            if missing:
                return "fail", ["Output sem binding: " + output_id for output_id in missing], evidence, []
            status = scripted[criterion["criterion_id"]]
            return status, ["roteiro: " + status], evidence + [self.script.reference], []
        statuses, observations, evidence = [], [], []
        for output_id in criterion["pass_rule"]["output_ids"]:
            binding = bindings.get(output_id)
            if binding is None:
                statuses.append("fail")
                observations.append("Output sem binding: " + output_id)
                continue
            evidence.append(copy.deepcopy(binding["artifact"]))
            if method in {"output_binding_present", "digest_matches"}:
                # Bytes já relidos e conferidos contra o digest do binding.
                statuses.append("pass")
                continue
            validator = self.contracts.validator(evidences[output_id].content_contract_ref)
            if validator is None:
                statuses.append("not_run")
                observations.append("Contrato de conteúdo não registrado: " + output_id)
                continue
            try:
                document = json.loads(contents[output_id].decode("utf-8"), object_pairs_hook=_json_object,
                                      parse_constant=_invalid_constant)
                canonicalize(document)  # Perfil I-JSON; rejeita overflow numérico e valores não finitos.
                statuses.append("pass" if validator.is_valid(document) else "fail")
            except (ValueError, UnicodeError, RecursionError):
                statuses.append("fail")
                observations.append("JSON inválido: " + output_id)
            except Unresolvable:
                statuses.append("not_run")
                observations.append("Referência de schema não registrada: " + output_id)
        status = "fail" if "fail" in statuses else ("not_run" if "not_run" in statuses else "pass")
        return status, observations, evidence, []
