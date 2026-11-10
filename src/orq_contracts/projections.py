"""Replay puro do journal e plano de retomada, sem acesso a disco ou executores.

O WorkflowSpec é um snapshot de entrada: o journal persiste sua referência, não
seus bytes. project_journal confere essa referência antes de criar tarefas.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, localcontext
from typing import Any, Iterable

from .canonical import canonical_digest
from .state_machine import (
    ATTEMPT_TERMINAL, RUN_TERMINAL, TASK_TERMINAL, StateMachineError,
    require, retry_allowed, timestamp, validate_edge, validate_transition,
)
from .validator import (
    validate_context_manifest, validate_instance, validate_planning_record,
    validate_routing_decision, validate_workflow,
)


class ProjectionError(StateMachineError):
    """Evento ou vínculo inconsistente no replay; veja ``code`` e ``event_id``."""

    def __init__(self, code: str, message: str, event_id: str | None = None) -> None:
        self.event_id = event_id
        super().__init__(code, message)


@dataclass
class AggregateProjection:
    id: str
    state: str | None = None
    usage_record_ids: list[str] = field(default_factory=list)
    error_record_ids: list[str] = field(default_factory=list)
    as_of_global_sequence: int = 0
    as_of_event_id: str | None = None


@dataclass
class TaskProjection(AggregateProjection):
    spec: dict = field(default_factory=dict)
    attempt_ids: list[str] = field(default_factory=list)
    accepted_bindings: list[dict] = field(default_factory=list)
    accepted_event_id: str | None = None


@dataclass
class AttemptProjection(AggregateProjection):
    header: dict = field(default_factory=dict)
    intent: dict = field(default_factory=dict)
    dispatch_sequence: int = 0
    produced_artifacts: list[dict] = field(default_factory=list)
    output_bindings: list[dict] = field(default_factory=list)
    external_call_id: str | None = None
    termination_certainty: str = "unknown"
    resolved_model_id: str | None = None
    effect_proven: bool = False
    reconciliation_deadline: str | None = None
    failure_error_id: str | None = None

    @property
    def undeclared_artifacts(self) -> list[dict]:
        bound = {b["artifact"]["artifact_id"] for b in self.output_bindings}
        return [a for a in self.produced_artifacts if a["artifact_id"] not in bound]


@dataclass
class EvaluationProjection(AggregateProjection):
    request: dict = field(default_factory=dict)
    report: dict | None = None


@dataclass
class ReservationProjection:
    reservation: dict
    state: str = "open"
    usage_ids: list[str] = field(default_factory=list)
    release_reason: str | None = None


@dataclass
class RunProjection(AggregateProjection):
    intent: dict | None = None
    manifest: dict | None = None
    manifest_sequence: int = 0
    workflow: dict | None = None
    tasks: dict[str, TaskProjection] = field(default_factory=dict)
    attempts: dict[str, AttemptProjection] = field(default_factory=dict)
    evaluations: dict[str, EvaluationProjection] = field(default_factory=dict)
    reservations: dict[str, ReservationProjection] = field(default_factory=dict)
    errors: dict[str, dict] = field(default_factory=dict)
    usages: dict[str, dict] = field(default_factory=dict)
    artifacts: dict[str, dict] = field(default_factory=dict)
    records: dict[str, dict] = field(default_factory=dict)
    events: dict[str, dict] = field(default_factory=dict)
    transition_ids: dict[str, str] = field(default_factory=dict)
    cancel_requested: list[dict] = field(default_factory=list)
    run_deadlines: list[dict] = field(default_factory=list)
    published_summary: dict | None = None
    published_cutoff: int | None = None
    reconcile_not_found_authoritative: bool = False
    stopping_cause: str | None = None

    def aggregate(self, kind: str, id: str) -> AggregateProjection:
        if kind == "run":
            require(id == self.id, "aggregate_mismatch", "ID do run diverge")
            return self
        collection = {"task": self.tasks, "attempt": self.attempts, "evaluation": self.evaluations}.get(kind, {})
        require(id in collection, "aggregate_not_created", f"{kind}/{id} ainda não criado")
        return collection[id]

    def aggregate_state(self, kind: str, id: str) -> str | None:
        return self.aggregate(kind, id).state

    def history(self, kind: str, id: str, type: str) -> list[dict]:
        return [e for e in self.events.values() if e["aggregate_type"] == kind and
                e["aggregate_id"] == id and e["event_type"] == type]

    def latest_attempt(self, task_id: str) -> AttemptProjection | None:
        ids = self.tasks[task_id].attempt_ids
        return self.attempts[ids[-1]] if ids else None

    def task_evaluations(self, task_id: str) -> list[EvaluationProjection]:
        a = self.latest_attempt(task_id)
        return [e for e in self.evaluations.values() if a and e.request["scope"].get("attempt_id") == a.id]

    def latest_report(self, task_id: str) -> dict | None:
        evaluations = self.task_evaluations(task_id)
        return evaluations[-1].report if evaluations else None

    @property
    def global_report(self) -> dict | None:
        return next((e.report for e in self.evaluations.values() if e.request["scope"]["kind"] == "workflow"), None)

    def limit(self, name: str, task_id: str | None = None) -> int:
        resolved = self.manifest["resolved_limits"][name]
        if task_id:
            return min(resolved, self.tasks[task_id].spec["constraints"].get(name, resolved))
        return resolved

    def time_remaining(self, now: str) -> bool:
        if not self.manifest or not self.manifest["resolved_limits"]:
            return False
        return timestamp(now) < timestamp(self.intent["created_at"]) + timedelta(milliseconds=self.limit("max_run_duration_ms"))

    def outputs_complete(self, a: AttemptProjection) -> bool:
        return {o["output_id"] for o in self.tasks[a.header["task_id"]].spec["expected_output"]} == {
            b["output_id"] for b in a.output_bindings}

    def failed_dependency(self, task_id: str) -> bool:
        return any(self.tasks[d].state in {"failed", "blocked", "indeterminate"} or self.failed_dependency(d)
                   for d in self.tasks[task_id].spec["dependencies"])

    def reconciliation_expired(self, task_id: str, now: str) -> bool:
        a = self.latest_attempt(task_id)
        if a and a.state == "unknown" and a.reconciliation_deadline:
            return timestamp(now) >= timestamp(a.reconciliation_deadline)
        evals = self.task_evaluations(task_id)
        starts = self.history("task", task_id, "state_transition")
        if evals and (evals[-1].report is None or evals[-1].report["execution_status"] == "indeterminate") and starts:
            deadline = min(timestamp(starts[-1]["recorded_at"]) + timedelta(milliseconds=self.limit("reconciliation_timeout_ms")),
                           timestamp(self.intent["created_at"]) + timedelta(milliseconds=self.limit("max_run_duration_ms")))
            return timestamp(now) >= deadline
        return False

    @property
    def active_work(self) -> bool:
        return any(a.state not in ATTEMPT_TERMINAL for a in self.attempts.values()) or any(
            e.report is None or e.report["execution_status"] == "indeterminate" for e in self.evaluations.values())

    @property
    def pending_dispatch_intents(self) -> dict[str, dict]:
        return {a.intent["dispatch_intent_id"]: a.intent for a in self.attempts.values()
                if not a.effect_proven and a.termination_certainty != "confirmed"}

    @property
    def output_bindings(self) -> list[dict]:
        """Outputs finais do WorkflowSpec, remapeando IDs locais aceitos."""
        result = []
        for output in (self.workflow or {}).get("outputs", []):
            task = self.tasks.get(output["source_task_id"])
            if task:
                binding = next((b for b in task.accepted_bindings if b["output_id"] == output["source_output_id"]), None)
                if binding:
                    result.append({"output_id": output["output_id"], "artifact": deepcopy(binding["artifact"])})
        return result

    @property
    def cost_summary(self) -> dict:
        leaves = self.latest_usages()
        lines = [line for u in leaves for line in u["cost_lines"]]
        currency = (self.manifest or {}).get("budget_snapshot")
        currencies = {line["currency"] for line in lines}
        currency = currency["currency"] if currency else (next(iter(currencies)) if currencies else None)
        require(not currencies or currencies == {currency}, "currency_mismatch", "run aceita uma moeda")
        # Precisão cresce com os operandos; o contexto Decimal padrão não pode arredondar custo.
        with localcontext() as ctx:
            ctx.prec = 32 + sum(len(line["amount"] or "") for line in lines)
            totals = {s: sum((Decimal(l["amount"]) for l in lines if l["status"] == s), Decimal(0))
                      for s in ("reported", "estimated")}
        unavailable = sum(any(l["status"] == "unavailable" for l in u["cost_lines"]) for u in leaves)
        # Um despacho possivelmente executado sem UsageRecord também é lacuna.
        # Avaliador/planner/router locais não são chamadas debitáveis presumidas.
        unavailable += sum(not self.attempt_has_usage(a) and not self.dispatch_proven_not_executed(a)
                           for a in self.attempts.values())
        estimated = any(l["status"] == "estimated" for l in lines)
        return {"currency": currency, **{s: format(n, "f") for s, n in totals.items()},
                "unavailable_count": unavailable, "total_cost_exact": not unavailable and not estimated}

    def latest_usages(self) -> list[dict]:
        superseded = {u["supersedes_usage_id"] for u in self.usages.values()}
        return [u for id, u in self.usages.items() if id not in superseded]

    def attempt_has_usage(self, a: AttemptProjection) -> bool:
        return any((u["scope_type"] == "attempt" and u["scope_ref"] == a.id) or
                   (u["scope_type"] == "call" and u["scope_ref"] == a.external_call_id)
                   for u in self.latest_usages())

    def dispatch_proven_not_executed(self, a: AttemptProjection) -> bool:
        # AT04 depende de prova volátil; o evento sozinho não prova custo zero.
        failure = self.errors.get(a.failure_error_id)
        if failure and failure["code"] in {"dispatch_failed", "dispatch_not_executed"}:
            return True  # Erro confirmado validado por AT03/AT13.
        errors = self.history("attempt", a.id, "error_attached")
        if a.state == "queued" and not a.effect_proven:
            return any(e["payload"]["code"] == "dispatch_failed" and
                       e["payload"]["termination_certainty"] == "confirmed" for e in errors)
        observations = self.history("attempt", a.id, "reconciliation_observed")
        if observations and self.reconcile_not_found_authoritative:
            last = observations[-1]
            return bool(last["payload"]["outcome"] == "not_found" and last["payload"]["evidence_refs"] and any(
                e["global_sequence"] > last["global_sequence"] and e["payload"]["code"] == "dispatch_not_executed" and
                e["payload"]["termination_certainty"] == "confirmed" for e in errors))
        return False

    @property
    def unassigned_reservations(self) -> dict[str, ReservationProjection]:
        used = {rid for a in self.attempts.values() for rid in a.intent["budget_reservation_ids"]} | {
            rid for e in self.evaluations.values() for rid in e.request["budget_reservation_ids"]}
        return {rid: r for rid, r in self.reservations.items() if rid not in used}

    @property
    def budget_gaps(self) -> tuple[tuple[str, str], ...]:
        """Escopos sem estimativa conservadora; o runner decide o gate opcional."""
        return self._budget_commitment()[1]

    @property
    def budget_committed(self) -> Decimal | None:
        """Comprometimento conservador; None com lacuna e teto não obrigatório."""
        total, gaps = self._budget_commitment()
        mandatory = (self.manifest or {}).get("budget_snapshot") or {}
        require(not gaps or not mandatory.get("cap_mandatory"), "budget_unknown", "comprometimento sem estimativa")
        return None if gaps else total

    def _budget_commitment(self) -> tuple[Decimal, tuple[tuple[str, str], ...]]:
        summary = self.cost_summary
        with localcontext() as ctx:
            ctx.prec = 32 + sum(len(str(r.reservation["amount"])) for r in self.reservations.values()) + sum(
                len(l["amount"] or "") for u in self.usages.values() for l in u["cost_lines"])
            total = Decimal(summary["reported"]) + Decimal(summary["estimated"])
            covered: set[tuple[str, str]] = set()
            gaps: set[tuple[str, str]] = set()
            for r in self.reservations.values():
                scope = (r.reservation["scope_type"], r.reservation["scope_ref"])
                missing = any((u["scope_type"], u["scope_ref"]) == scope and any(
                    l["status"] == "unavailable" for l in u["cost_lines"]) for u in self.latest_usages())
                if r.state == "open" or (r.state == "settled" and missing):
                    if r.reservation["amount"] is None:
                        gaps.add(scope)
                    else:
                        total += Decimal(r.reservation["amount"])
                        covered.add(scope)
            gaps.update((u["scope_type"], u["scope_ref"]) for u in self.latest_usages() if
                        any(l["status"] == "unavailable" for l in u["cost_lines"]) and
                        (u["scope_type"], u["scope_ref"]) not in covered)
            gaps.update(("attempt", a.id) for a in self.attempts.values() if not (
                self.attempt_has_usage(a) or self.dispatch_proven_not_executed(a) or ("attempt", a.id) in covered))
            return total, tuple(sorted(gaps))


def _bindings(bindings: list[dict]) -> dict[str, dict]:
    result = {b["output_id"]: b["artifact"] for b in bindings}
    require(len(result) == len(bindings), "duplicate_output_binding", "output_id repetido")
    return result


def record_ref(record: dict, id_field: str) -> dict:
    return {"id": record[id_field], "version": record["schema_version"],
            "content_digest": record.get("record_hash") or record.get("selection_hash") or canonical_digest(record)}


def _record_hash(record: dict, field: str = "record_hash") -> None:
    if field in record:
        require(record[field] == canonical_digest({k: v for k, v in record.items() if k != field}),
                "record_hash_mismatch", f"{field} diverge")


def project_journal(events: Iterable[dict[str, Any]], *, workflow: dict | None = None,
                    as_of_global_sequence: int | None = None,
                    reconcile_not_found_authoritative: bool = False) -> RunProjection:
    """Reconstrói um prefixo inteiro, usando os mesmos dicts de RunJournal.events.

    Não ordena nem deduplica entradas: o journal já deduplica no append. Verifica
    também envelope/hash/causalidade para sequências fornecidas sem RunJournal.
    O booleano de not_found deve vir de controles verificados do executor; padrão
    conservador False. Evidência versionada continua obrigatória no evento.
    """
    projection = RunProjection("", workflow=deepcopy(workflow),
                               reconcile_not_found_authoritative=reconcile_not_found_authoritative)
    sequences: dict[tuple[str, str], int] = {}
    keys: set[str] = set()
    previous_hash = None
    for original in events:
        event = deepcopy(original)
        if as_of_global_sequence is not None and event.get("global_sequence", 0) > as_of_global_sequence:
            break
        try:
            require(not validate_instance(event, "journal_event"), "event_schema_invalid", "schema/payload inválido")
            seq, kind, id = event["global_sequence"], event["aggregate_type"], event["aggregate_id"]
            require(seq == len(projection.events) + 1 and event["event_id"] == f"event-{seq}",
                    "journal_integrity_error", "sequência/ID global inválido")
            require(event["previous_event_hash"] == previous_hash and event["event_hash"] == canonical_digest(
                {k: v for k, v in event.items() if k != "event_hash"}), "journal_integrity_error", "cadeia/hash diverge")
            require(event["aggregate_sequence"] == sequences.get((kind, id), 0) + 1,
                    "journal_integrity_error", "sequência do agregado diverge")
            require(event["causation_event_id"] is None or event["causation_event_id"] in projection.events,
                    "journal_integrity_error", "causa não é anterior")
            key = event["idempotency_key"]
            require(key is None or key not in keys, "idempotency_conflict", "chave repetida no journal")
            if key is not None:
                keys.add(key)
            if seq == 1:
                require(kind == "run" and event["event_type"] == "run_intent_recorded", "aggregate_not_created", "primeiro evento deve criar run")
                projection.id = event["run_id"]
            require(event["run_id"] == projection.id and (kind != "run" or id == projection.id),
                    "aggregate_mismatch", "run do evento diverge")
            _apply(projection, event)
            projection.events[event["event_id"]] = event
            projection.as_of_global_sequence, projection.as_of_event_id = seq, event["event_id"]
            agg = projection.aggregate(kind, id)
            agg.as_of_global_sequence, agg.as_of_event_id = seq, event["event_id"]
            sequences[kind, id] = event["aggregate_sequence"]
            previous_hash = event["event_hash"]
        except StateMachineError as error:
            raise ProjectionError(error.code, error.message, event.get("event_id")) from error
    if as_of_global_sequence is not None:
        if projection.as_of_global_sequence != as_of_global_sequence:
            raise ProjectionError("cutoff_invalid", "cutoff não existe")
    return projection


def _apply(s: RunProjection, e: dict) -> None:
    type, kind, id, p = e["event_type"], e["aggregate_type"], e["aggregate_id"], e["payload"]
    if s.published_summary is not None:
        require(type in {"usage_attached", "late_observation"}, "run_already_closed", "somente observações/uso tardios")
    _record_hash(p)
    if "run_id" in p:
        require(p["run_id"] == s.id, "aggregate_mismatch", "run_id do registro diverge")
    if type == "run_intent_recorded":
        require(s.intent is None, "duplicate_id", "RunIntent já existe")
        validate_edge("run", s.state, None, "created", event_type=type)
        s.intent, s.state = p, "created"
        s.records[p["run_id"]] = p
        return
    if type == "dispatch_requested":
        _dispatch(s, e)
        return
    if type == "evaluation_requested":
        _evaluation_request(s, e)
        return
    agg = s.aggregate(kind, id)
    if type == "state_transition":
        row = validate_transition(s, e)
        if row.id == "RN07":
            s.stopping_cause = "cancelled" if s.cancel_requested else "failed"
        if row.id in {"AT03", "AT07", "AT13"}:
            errors = [s.events[x] for x in p["evidence_event_ids"] if
                      s.events[x]["aggregate_type"] == kind and s.events[x]["aggregate_id"] == id and
                      s.events[x]["event_type"] == "error_attached" and
                      s.events[x]["payload"]["termination_certainty"] == "confirmed"]
            agg.failure_error_id = max(errors, key=lambda error: error["global_sequence"])["payload"]["error_id"]
        agg.state = p["to"]
        s.transition_ids[e["event_id"]] = row.id
        if kind == "attempt" and agg.state in ATTEMPT_TERMINAL:
            agg.termination_certainty = "confirmed"
    elif type == "planning_recorded":
        require(s.manifest is None and s.state == "running", "event_out_of_order", "planejamento fora da preparação")
        require(not validate_planning_record(p), "planning_record_invalid", "planejamento inconsistente")
        require(p["planning_request_snapshot"]["run_id"] == s.id and
                p["planning_request_snapshot"]["project_id"] == s.intent["project_id"],
                "reference_mismatch", "pedido de planejamento de outro run/projeto")
        _unique_record(s, p, "planning_record_id")
    elif type == "run_manifest_closed":
        _manifest(s, e)
    elif type in {"knowledge_selection_recorded", "routing_decision_recorded", "context_manifest_recorded"}:
        require(agg.state == "ready" and s.state == "running" and not s.cancel_requested,
                "event_out_of_order", "admissão exige tarefa ready e run ativo")
        require(p["task_id"] == id and p["workflow_ref"] == s.manifest["workflow_snapshot_ref"],
                "aggregate_mismatch", "tarefa/workflow do registro diverge")
        require(p["candidate_attempt_number"] == len(agg.attempt_ids) + 1,
                "attempt_number_not_contiguous", "candidato não é próximo número")
        field = {"knowledge_selection_recorded": "selection_id", "routing_decision_recorded": "routing_decision_id",
                 "context_manifest_recorded": "context_manifest_id"}[type]
        if type == "knowledge_selection_recorded":
            _record_hash(p, "selection_hash")
            require(p["catalog_snapshot_ref"] == s.manifest["knowledge_catalog_snapshot_ref"], "knowledge_catalog_mismatch", "catálogo deve ser fixo")
        elif type == "routing_decision_recorded":
            require(not validate_routing_decision(p), "routing_decision_invalid", "decisão inconsistente")
            require(p["catalog_snapshot_ref"] == s.manifest["routing_catalog_ref"] and
                    p["execution_policy_snapshot_ref"] == s.manifest["execution_policy_snapshot_ref"],
                    "reference_mismatch", "snapshots de roteamento divergentes")
            policy = p["routing_policy_snapshot"]
            require(s.manifest["routing_policy_ref"] == {"id": policy["routing_policy_id"], "version": policy["version"],
                    "content_digest": canonical_digest(policy)}, "reference_mismatch", "política de roteamento divergente")
        else:
            require(not validate_context_manifest(p), "context_manifest_invalid", "contexto inconsistente")
            decision = _resolve(s, p["routing_decision_ref"], "routing_decision_id")
            require(decision["task_id"] == id and decision["candidate_attempt_number"] == p["candidate_attempt_number"] and
                    decision["effective_selection"] and decision["effective_selection"]["selected_target_id"] == p["target_ref"]["id"] and
                    any(c["target_ref"] == p["target_ref"] and c["eligible"] for c in decision["candidates"]),
                    "target_mismatch", "contexto diverge da decisão")
        _unique_record(s, p, field)
    elif type == "artifact_ingested":
        _artifact(s, e)
    elif type == "output_bound":
        require(agg.state in {"running", "unknown"}, "event_out_of_order", "binding fora da execução")
        require(p["output_id"] not in _bindings(agg.output_bindings), "duplicate_output_binding", "binding já existe")
        descriptor = s.artifacts.get(p["artifact"]["artifact_id"])
        require(descriptor and descriptor["content_digest"] == p["artifact"]["content_digest"] and
                descriptor["producer"].get("attempt_id") == id, "output_binding_mismatch", "artefato não pertence à tentativa")
        output = next((o for o in s.tasks[agg.header["task_id"]].spec["expected_output"] if o["output_id"] == p["output_id"]), None)
        require(output and output["content_contract_ref"] == descriptor["content_contract_ref"] and
                output["artifact_type"] == descriptor["artifact_type"], "output_contract_mismatch", "contrato do output diverge")
        agg.output_bindings.append(p)
    elif type == "outputs_accepted":
        require(s.state == "running" and not s.cancel_requested and agg.state == "evaluating" and not agg.accepted_event_id,
                "outputs_acceptance_invalid", "aceite fora de evaluating/run ativo ou repetido")
        ev = s.evaluations.get(p["evaluation_id"])
        a = s.latest_attempt(id)
        require(p["task_id"] == id and ev and ev.report and ev.report["verdict"] == "pass" and
                ev.request["scope"].get("attempt_id") == (a.id if a else None),
                "outputs_without_pass", "aceite exige pass da tentativa atual")
        require(_bindings(p["output_bindings"]) == _bindings(ev.report["subject_bindings"]) == _bindings(a.output_bindings),
                "evaluation_bindings_mismatch", "aceite difere dos bindings avaliados")
        agg.accepted_bindings, agg.accepted_event_id = p["output_bindings"], e["event_id"]
    elif type == "evaluation_report_recorded":
        _evaluation_report(s, e)
    elif type in {"usage_attached", "error_attached"}:
        _attachment(s, e)
    elif type.startswith("budget_"):
        _budget(s, e)
    elif type == "cancel_requested":
        if kind == "run":
            s.cancel_requested.append(e)
    elif type == "deadline_exceeded":
        if kind == "run":
            s.run_deadlines.append(e)
        else:
            require(p["deadline_at"] == agg.intent["deadline_at"], "deadline_mismatch", "deadline diverge da intenção")
        require(timestamp(p["observed_at"]) >= timestamp(p["deadline_at"]), "deadline_not_exceeded", "prazo ainda não passou")
    elif type in {"dispatch_acknowledged", "execution_started", "termination_confirmed", "result_collected",
                  "reconciliation_requested", "reconciliation_observed"}:
        _observation(s, e)
    elif type == "admission_rejected":
        require(agg.state == "ready", "event_out_of_order", "rejeição exige ready")
        require(p["error_record_ids"] and all(x in s.errors and x in agg.error_record_ids for x in p["error_record_ids"]),
                "reference_mismatch", "rejeição exige erro anexado à tarefa")
        require(all(r.state == "released" for r in s.unassigned_reservations.values()),
                "admission_reservations_open", "liberar reservas da admissão antes de rejeitar")
    elif type == "run_closed":
        _close(s, e)
    # journal_tail_discarded e late_observation são fatos sem mudança de estado.


def _unique_record(s: RunProjection, p: dict, id_field: str) -> None:
    require(p[id_field] not in s.records, "duplicate_id", "ID de registro repetido")
    s.records[p[id_field]] = p


def _resolve(s: RunProjection, ref: dict, id_field: str) -> dict:
    record = s.records.get(ref["id"])
    require(record and record_ref(record, id_field) == ref, "reference_mismatch", "referência não resolve o registro/digest")
    return record


def _manifest(s: RunProjection, e: dict) -> None:
    p = e["payload"]
    require(s.manifest is None and s.state == "running", "manifest_already_closed", "manifesto fora da preparação ou repetido")
    require(p["run_intent_ref"] == record_ref(s.intent, "run_id") and p["project_id"] == s.intent["project_id"],
            "reference_mismatch", "manifesto difere do RunIntent")
    require(p["objective_snapshot"]["objective"] == s.intent["objective"] and
            p["objective_snapshot"]["objective_hash"] == canonical_digest(s.intent["objective"]) and
            p["experiment_metadata"] == s.intent["experiment_metadata"], "reference_mismatch", "snapshot do pedido diverge")
    if p["planning_record_ref"]:
        plan = _resolve(s, p["planning_record_ref"], "planning_record_id")
    else:
        plan = None
    if p["manifest_status"] == "ready":
        require(plan and plan["status"] == "accepted" and plan["proposed_workflow_ref"] == p["workflow_snapshot_ref"],
                "planning_not_accepted", "ready exige plano aceito")
        require(plan["planning_request_snapshot"]["objective"] == s.intent["objective"],
                "reference_mismatch", "plano de outro objetivo")
        require(s.workflow is not None, "workflow_snapshot_required", "forneça WorkflowSpec para resolver tarefas")
        require(not validate_workflow(s.workflow), "workflow_snapshot_invalid", "workflow inválido")
        ref = {"id": s.workflow["workflow_id"], "version": s.workflow["version"], "content_digest": canonical_digest(s.workflow)}
        require(ref == p["workflow_snapshot_ref"], "workflow_snapshot_mismatch", "workflow não corresponde ao manifesto")
        require(s.workflow["project_id"] == p["project_id"] and s.workflow["objective"] == s.intent["objective"],
                "reference_mismatch", "workflow de outro projeto/objetivo")
        require(all(p["resolved_limits"][name] <= value for name, value in s.workflow["constraints"].items()
                    if name in p["resolved_limits"] and isinstance(value, int)), "resolved_limits_invalid", "limite resolvido relaxa o workflow")
        require(not s.cancel_requested, "event_out_of_order", "cancelamento precedeu ready")
        for spec in s.workflow["tasks"]:
            s.tasks[spec["task_id"]] = TaskProjection(spec["task_id"], "pending", spec=spec,
                as_of_global_sequence=e["global_sequence"], as_of_event_id=e["event_id"])
        for binding in p["external_input_bindings"]:
            artifact = s.artifacts.get(binding["artifact"]["artifact_id"])
            require(artifact and artifact["producer"].get("input_id") == binding["input_id"] and
                    artifact["content_digest"] == binding["artifact"]["content_digest"], "input_binding_mismatch", "entrada externa não ingerida")
    elif p["manifest_status"] == "cancelled_before_ready":
        require(s.cancel_requested, "event_out_of_order", "manifesto cancelado exige pedido anterior")
    for assertion in p["knowledge_status_assertions"]:
        _record_hash(assertion, "assertion_digest")
    s.manifest, s.manifest_sequence = p, e["global_sequence"]


def _dispatch(s: RunProjection, e: dict) -> None:
    h, d = e["payload"]["attempt_header"], e["payload"]["dispatch_intent"]
    _record_hash(h)
    _record_hash(d)
    require(not any(a.intent["dispatch_intent_id"] == d["dispatch_intent_id"] for a in s.attempts.values()),
            "dispatch_intent_reused", "intenção aparece em mais de um dispatch_requested")
    require(h["attempt_id"] not in s.attempts, "duplicate_id", "tentativa já existe")
    task = s.tasks.get(d["task_id"])
    require(task and task.state == "ready" and s.state == "running" and not s.cancel_requested,
            "dispatch_not_admissible", "despacho exige ready/run ativo")
    require(h["attempt_number"] == d["attempt_number"] == len(task.attempt_ids) + 1,
            "attempt_number_not_contiguous", "número não é próximo da tarefa")
    require(d["attempt_number"] <= s.limit("max_attempts", task.id), "attempts_exhausted", "max_attempts excedido")
    require(h["previous_attempt_id"] == (task.attempt_ids[-1] if task.attempt_ids else None),
            "previous_attempt_mismatch", "cadeia de tentativas diverge")
    if task.attempt_ids:
        require(retry_allowed(s, task.id, e["recorded_at"]), "retry_not_allowed", "retry sem término confirmado/política/limite")
    require(not s.active_work and not any(t.state in {"running", "evaluating", "waiting_evaluation", "reconciling"}
                                        for t in s.tasks.values()), "concurrency_exceeded", "slot sequencial ocupado")
    require(h["attempt_id"] == e["aggregate_id"] and d["run_id"] == h["run_id"] == s.id,
            "aggregate_mismatch", "identidade da tentativa diverge")
    common = ("attempt_id", "task_id", "workflow_ref", "routing_decision_ref", "context_manifest_ref",
              "workspace_base_ref", "input_bindings", "budget_reservation_ids", "dispatch_intent_id",
              "dispatch_idempotency_key", "deadline_at", "created_at")
    require(all(h[k] == d[k] for k in common), "dispatch_header_mismatch", "cabeçalho diverge da intenção")
    require(d["dispatch_idempotency_key"] == f"{s.id}/{d['dispatch_intent_id']}" and
            e["idempotency_key"] == d["dispatch_idempotency_key"], "dispatch_key_mismatch", "chave de despacho diverge")
    require(d["workflow_ref"] == s.manifest["workflow_snapshot_ref"], "reference_mismatch", "workflow divergente")
    decision = _resolve(s, d["routing_decision_ref"], "routing_decision_id")
    context = _resolve(s, d["context_manifest_ref"], "context_manifest_id")
    require(decision["task_id"] == context["task_id"] == task.id and
            decision["candidate_attempt_number"] == context["candidate_attempt_number"] == d["attempt_number"],
            "aggregate_mismatch", "decisão/contexto de outra tentativa")
    require(decision["effective_selection"] and decision["effective_selection"]["selected_target_id"] == d["target_ref"]["id"] and
            any(c["target_ref"] == d["target_ref"] and c["eligible"] for c in decision["candidates"]) and
            context["target_ref"] == d["target_ref"] == h["execution_target_snapshot"]["target_ref"],
            "target_mismatch", "target_ref divergente")
    require(context["bundle_hash"] == d["bundle_hash"], "bundle_hash_mismatch", "bundle_hash divergente")
    require(context["routing_decision_ref"] == d["routing_decision_ref"] and
            h["execution_target_snapshot"]["executor_ref"] == d["executor_ref"] == s.manifest["executor_snapshot"]["executor_ref"],
            "reference_mismatch", "contexto/executor divergente")
    for ref in context["knowledge_selection_refs"]:
        selection = _resolve(s, ref, "selection_id")
        require(selection["status"] in {"satisfied", "partial"} and selection["task_id"] == task.id and
                selection["candidate_attempt_number"] == d["attempt_number"], "knowledge_selection_invalid", "seleção não admissível")
    _knowledge_status(s, context, d)
    _check_inputs(s, task, d["input_bindings"])
    require(s.time_remaining(e["recorded_at"]) and timestamp(e["recorded_at"]) < timestamp(d["deadline_at"]),
            "run_deadline", "sem tempo para despacho")
    require(d["attempt_timeout_ms"] <= s.limit("attempt_timeout_ms", task.id), "deadline_mismatch", "timeout excede limite")
    end = min(timestamp(d["created_at"]) + timedelta(milliseconds=d["attempt_timeout_ms"]),
              timestamp(s.intent["created_at"]) + timedelta(milliseconds=s.limit("max_run_duration_ms")))
    require(timestamp(d["deadline_at"]) == end, "deadline_mismatch", "deadline não corresponde aos limites")
    require(d["budget_reservation_ids"], "reservation_missing", "despacho sem reserva")
    for rid in d["budget_reservation_ids"]:
        r = s.reservations.get(rid)
        require(r and r.state == "open" and ((r.reservation["scope_type"] == "attempt" and
                r.reservation["scope_ref"] == h["attempt_id"]) or r.reservation["scope_type"] == "evaluation"),
                "reservation_scope_mismatch", "reserva ausente, consumida ou de outro escopo")
        require(not any(rid in a.intent["budget_reservation_ids"] for a in s.attempts.values()), "reservation_reused", "reserva de outro despacho")
    require(any(s.reservations[r].reservation["scope_type"] == "attempt" for r in d["budget_reservation_ids"]),
            "reservation_missing", "reserva da execução ausente")
    budget = s.manifest["budget_snapshot"]
    if budget["cap_mandatory"]:
        estimates = [x for x in budget["cost_estimates"] if x["scope"] == "attempt" and x["subject_ref"] == d["target_ref"]]
        require(estimates, "budget_unknown", "teto exige estimativa do destino")
        reserved = next(s.reservations[r].reservation for r in d["budget_reservation_ids"]
                        if s.reservations[r].reservation["scope_type"] == "attempt")
        require(Decimal(reserved["amount"]) >= max(Decimal(x["amount"]) for x in estimates),
                "budget_under_reserved", "reserva abaixo da estimativa conservadora")
    validate_edge("attempt", None, None, "queued", event_type="dispatch_requested")
    a = AttemptProjection(h["attempt_id"], "queued", header=h, intent=d, dispatch_sequence=e["global_sequence"])
    s.attempts[a.id] = a
    task.attempt_ids.append(a.id)


def _check_inputs(s: RunProjection, task: TaskProjection, bindings: list[dict]) -> None:
    require(len({b["input_id"] for b in bindings}) == len(bindings), "input_binding_mismatch", "input duplicado")
    for b in bindings:
        spec = next((i for i in task.spec["inputs"] if i["input_id"] == b["input_id"]), None)
        require(spec, "input_binding_mismatch", "input não declarado")
        descriptor = s.artifacts.get(b["artifact"]["artifact_id"])
        require(descriptor and descriptor["artifact_type"] == spec["artifact_type"] and
                (not spec.get("content_contract_ref") or descriptor["content_contract_ref"] == spec["content_contract_ref"]),
                "input_binding_mismatch", "tipo/contrato incompatível com o input")
        basis = s.events.get(b["basis"]["event_id"])
        if b["basis"]["kind"] == "outputs_accepted":
            require(basis and basis["event_type"] == "outputs_accepted" and
                    basis["aggregate_id"] in task.spec["dependencies"] and spec["source"]["kind"] == "task_output" and
                    spec["source"]["task_id"] == basis["aggregate_id"] and any(
                        x["artifact"] == b["artifact"] and x["output_id"] == spec["source"]["output_id"]
                        for x in basis["payload"]["output_bindings"]),
                    "input_binding_mismatch", "entrada exige output aceito de dependência direta")
        else:
            require(basis and basis["global_sequence"] == s.manifest_sequence and spec["source"]["kind"] == "external_input" and any(
                x["artifact"] == b["artifact"] and x["input_id"] == spec["source"]["input_id"] for x in s.manifest["external_input_bindings"]),
                "input_binding_mismatch", "entrada externa não autorizada")
    require(all(s.tasks[d].accepted_event_id for d in task.spec["dependencies"]),
            "inputs_unavailable", "dependência ainda sem aceite")
    required = {i["input_id"] for i in task.spec["inputs"] if i.get("required", True)}
    require(required <= {b["input_id"] for b in bindings}, "inputs_unavailable", "input obrigatório ausente")


def _knowledge_status(s: RunProjection, context: dict, intent: dict) -> None:
    check = next(c for c in intent["admission_checks"] if c["control"] == "knowledge_status")
    for item in context["items"]:
        unit = item["knowledge_provenance"]["unit_ref"]
        assertions = [a for a in s.manifest["knowledge_status_assertions"] if
            a["source_ref"] == {"source_id": unit["source_id"], "revision": unit["revision"], "content_digest": unit["source_digest"]}
            and timestamp(a["effective_at"]) <= timestamp(check["observed_at"])]
        if assertions:
            latest = max(timestamp(a["effective_at"]) for a in assertions)
            require(all(a["status"] == "active" for a in assertions if timestamp(a["effective_at"]) == latest),
                    "knowledge_revoked", "asserção do manifesto revogou a fonte usada")


def _artifact(s: RunProjection, e: dict) -> None:
    p, kind, id = e["payload"], e["aggregate_type"], e["aggregate_id"]
    producer = p["producer"]
    require(p["artifact_id"] not in s.artifacts, "duplicate_id", "artefato já registrado")
    require(p["locator"] == "orq-store:sha256/" + p["content_digest"]["value"], "artifact_digest_mismatch", "locator diverge do digest")
    if kind == "attempt":
        a = s.attempts[id]
        require(producer["kind"] == "attempt_output" and producer["run_id"] == s.id and
                producer["task_id"] == a.header["task_id"] and producer["attempt_id"] == id,
                "artifact_producer_mismatch", "produtor diverge da tentativa")
        require(a.state in {"running", "unknown"}, "event_out_of_order", "artefato fora da execução")
        require(s.history("attempt", id, "result_collected"), "event_out_of_order", "artefato de saída exige resultado coletado")
        a.produced_artifacts.append(p)
    elif kind == "evaluation":
        require(producer["kind"] == "evaluation_output" and producer["evaluation_id"] == id,
                "artifact_producer_mismatch", "produtor diverge da avaliação")
    else:
        require(producer["kind"] in {"external_input", "orchestrator_materialization", "imported_history"},
                "artifact_producer_mismatch", "produtor inválido para run")
        if producer["kind"] == "external_input":
            require(producer["run_id"] == s.id and s.manifest is None, "artifact_producer_mismatch", "entrada externa fora da preparação")
    s.artifacts[p["artifact_id"]] = p


def _evaluation_request(s: RunProjection, e: dict) -> None:
    p, id = e["payload"], e["aggregate_id"]
    require(s.state == "running" and not s.cancel_requested, "evaluation_not_admissible", "run não admite avaliação")
    require(id == p["evaluation_id"], "aggregate_mismatch", "ID da avaliação diverge")
    require(id not in s.evaluations, "duplicate_id", "avaliação já existe")
    require(not any(ev.report is None or ev.report["execution_status"] == "indeterminate" for ev in s.evaluations.values()),
            "concurrency_exceeded", "avaliação anterior sem término")
    scope = p["scope"]
    if scope["kind"] == "workflow":
        require(all(t.state == "succeeded" for t in s.tasks.values()) and s.manifest is not None,
                "evaluation_not_admissible", "global exige tarefas aceitas")
        previous = [ev for ev in s.evaluations.values() if ev.request["scope"]["kind"] == "workflow"]
        require(not previous and p["evaluation_number"] == 1, "evaluations_exhausted", "avaliação global única")
        bindings, criteria = s.output_bindings, s.workflow["evaluation_criteria"]
    else:
        task = s.tasks.get(scope["task_id"])
        a = s.latest_attempt(task.id) if task else None
        require(task and a and a.id == scope["attempt_id"] and a.state == "completed" and
                task.state in {"evaluating", "waiting_evaluation"}, "evaluation_not_admissible", "avaliação exige tentativa completed")
        previous = s.task_evaluations(task.id)
        require(p["evaluation_number"] == len(previous) + 1, "evaluation_number_not_contiguous", "número de avaliação diverge")
        require(p["evaluation_number"] <= s.limit("max_evaluations_per_attempt", task.id), "evaluations_exhausted", "limite de avaliações excedido")
        require(not previous or previous[-1].report and previous[-1].report["execution_status"] != "indeterminate" and
                previous[-1].report["verdict"] in {"inconclusive", "not_run"},
                "reevaluation_not_allowed", "não repetir pass/fail válido")
        bindings, criteria = a.output_bindings, task.spec["evaluation_criteria"]
    require(_bindings(p["subject_bindings"]) == _bindings(bindings), "evaluation_bindings_mismatch", "subjects divergentes")
    require(p["criteria_snapshot"] == {"criteria": criteria, "criteria_hash": canonical_digest(criteria)},
            "evaluation_criteria_mismatch", "critérios diferentes do snapshot")
    require(p["evaluator_snapshot"] == s.manifest["evaluator_snapshot"], "reference_mismatch", "avaliador divergente")
    for rid in p["budget_reservation_ids"]:
        r = s.reservations.get(rid)
        require(r and r.state == "open" and r.reservation["scope_type"] == "evaluation" and r.reservation["scope_ref"] == id,
                "reservation_scope_mismatch", "reserva de avaliação inválida")
    s.evaluations[id] = EvaluationProjection(id, "requested", request=p)


def _evaluation_report(s: RunProjection, e: dict) -> None:
    p, id = e["payload"], e["aggregate_id"]
    ev = s.evaluations[id]
    scope = ev.request["scope"]
    if scope["kind"] == "task_attempt":
        require(s.tasks[scope["task_id"]].state in {"evaluating", "reconciling"},
                "evaluation_reconciliation_closed", "relatório fora de evaluating/reconciling; usar late_observation")
    require(ev.report is None, "evaluation_already_reported", "relatório já existe")
    require(p["evaluation_id"] == id and all(p[k] == ev.request[k] for k in (
        "evaluation_number", "scope", "criteria_snapshot", "evaluator_snapshot")),
        "evaluation_request_mismatch", "relatório diverge da intenção")
    require(_bindings(p["subject_bindings"]) == _bindings(ev.request["subject_bindings"]),
            "evaluation_bindings_mismatch", "digests diferentes dos avaliados")
    criteria = {c["criterion_id"]: c for c in p["criteria_snapshot"]["criteria"]}
    results = {r["criterion_id"]: r for r in p["criterion_results"]}
    require(len(results) == len(p["criterion_results"]) and results.keys() == criteria.keys(),
            "evaluation_results_mismatch", "um resultado por critério obrigatório/opcional")
    for id, c in criteria.items():
        result = results[id]
        require(result["method"] == c["method"] and result["required"] == c["required"] and
                result["requirement_ids"] == c.get("requirement_ids", c.get("workflow_requirement_ids")),
                "evaluation_results_mismatch", "resultado diverge do critério")
        require(c["method"] in p["evaluator_snapshot"]["supported_methods"] or result["status"] == "not_run",
                "evaluation_verdict_invalid", "método não suportado exige not_run")
    required = [r["status"] for r in results.values() if r["required"]]
    if p["execution_status"] == "completed":
        expected = "fail" if "fail" in required else "inconclusive" if any(r != "pass" for r in required) else "pass"
        require(p["verdict"] == expected, "evaluation_verdict_invalid", "agregação do veredicto diverge")
    else:
        require(p["verdict"] in {"not_run", "inconclusive"}, "evaluation_verdict_invalid", "execução não concluída")
    require(all(x in ev.error_record_ids for x in p["error_record_ids"]), "reference_mismatch", "erro de avaliação não anexado")
    require(not p["usage_record_ids"] and all(x in ev.error_record_ids for r in p["criterion_results"] for x in r["error_record_ids"]),
            "reference_mismatch", "avaliador local não inventa uso nem erros")
    ev.report, ev.state = p, p["execution_status"]
    s.records[p["evaluation_id"]] = p


def _attachment(s: RunProjection, e: dict) -> None:
    p, kind, id = e["payload"], e["aggregate_type"], e["aggregate_id"]
    usage = e["event_type"] == "usage_attached"
    field, collection = ("usage_id", s.usages) if usage else ("error_id", s.errors)
    require(p[field] not in collection, "duplicate_id", "registro anexado repetido")
    scope, ref = p["scope_type"], p["scope_ref"]
    if scope in {"run", "task", "attempt", "evaluation"}:
        require(scope == kind and ref == id, "record_scope_mismatch", "escopo diverge do agregado")
    elif scope == "planning":
        require(kind == "run" and any(r.get("planning_record_id") == ref for r in s.records.values()),
                "record_scope_mismatch", "planejamento desconhecido")
    elif scope == "routing":
        require(kind in {"task", "run"} and any(r.get("routing_decision_id") == ref and
                (kind == "run" or r["task_id"] == id) for r in s.records.values()),
                "record_scope_mismatch", "roteamento desconhecido")
    elif scope == "call":
        require(kind == "attempt" and s.attempts[id].external_call_id == ref,
                "record_scope_mismatch", "chamada externa desconhecida")
    else:
        require(False, "record_scope_mismatch", "escopo não suportado nesta fase")
    if usage:
        previous = p["supersedes_usage_id"]
        same_scope = [u for u in s.usages.values() if (u["scope_type"], u["scope_ref"]) == (scope, ref)]
        require(not same_scope if previous is None else previous in s.usages and
                s.usages[previous] in same_scope and not any(u["supersedes_usage_id"] == previous for u in s.usages.values()),
                "usage_supersedes_invalid", "correção deve estender cadeia do mesmo escopo")
        currency = (s.manifest or {}).get("budget_snapshot")
        if currency:
            require(all(l["currency"] == currency["currency"] for l in p["cost_lines"]), "currency_mismatch", "moeda diferente do run")
        s.aggregate(kind, id).usage_record_ids.append(p[field])
    else:
        s.aggregate(kind, id).error_record_ids.append(p[field])
        if (kind == "attempt" and p["termination_certainty"] == "confirmed" and
                s.attempts[id].state == "queued" and p["code"] == "dispatch_failed"):
            s.attempts[id].termination_certainty = "confirmed"
    collection[p[field]] = p


def _budget(s: RunProjection, e: dict) -> None:
    p, type = e["payload"], e["event_type"]
    rid = p["reservation_id"]
    if type == "budget_reserved":
        require(rid not in s.reservations, "duplicate_id", "reserva já existe")
        require(s.state == "running" and s.manifest and not s.cancel_requested, "event_out_of_order", "run não admite reserva")
        budget = s.manifest["budget_snapshot"]
        require(p["currency"] == budget["currency"], "currency_mismatch", "moeda da reserva diverge")
        require(not budget["cap_mandatory"] or p["amount"] is not None, "budget_unknown", "teto obrigatório sem estimativa")
        s.reservations[rid] = ReservationProjection(p)
        if budget["max_cost"] is not None:
            committed = s.budget_committed
            require(committed is None or committed <= Decimal(budget["max_cost"]), "budget_exceeded", "reserva excede teto")
    else:
        r = s.reservations.get(rid)
        require(r and r.state == "open", "reservation_not_open", "reserva inexistente ou encerrada")
        if type == "budget_settled":
            require(p["usage_ids"], "reservation_usage_missing", "liquidação sem uso real")
            require(all(id in s.usages and s.usages[id]["scope_type"] == r.reservation["scope_type"] and
                        s.usages[id]["scope_ref"] == r.reservation["scope_ref"] for id in p["usage_ids"]),
                    "reservation_scope_mismatch", "uso não corresponde à reserva")
            require(not any(set(p["usage_ids"]) & set(other.usage_ids) for other in s.reservations.values()),
                    "usage_already_settled", "uso liquidado duas vezes")
            r.state, r.usage_ids = "settled", p["usage_ids"]
        else:
            scope, ref = r.reservation["scope_type"], r.reservation["scope_ref"]
            if scope == "attempt" and ref in s.attempts:
                require(s.attempts[ref].termination_certainty == "confirmed" and s.dispatch_proven_not_executed(s.attempts[ref]),
                        "reservation_release_invalid", "execução ativa/incerta ou com uso deve permanecer/liquidar")
            if scope == "evaluation" and ref in s.evaluations:
                report = s.evaluations[ref].report
                require(report and report["execution_status"] != "indeterminate", "reservation_release_invalid", "avaliação sem término confirmado")
            r.state, r.release_reason = "released", p["reason"]


def _observation(s: RunProjection, e: dict) -> None:
    type, p, id = e["event_type"], e["payload"], e["aggregate_id"]
    a = s.attempts[id]
    require(a.state not in ATTEMPT_TERMINAL, "attempt_already_terminal", "usar late_observation após término")
    require(s.tasks[a.header["task_id"]].state != "indeterminate", "attempt_reconciliation_closed", "usar late_observation após reconciliação esgotada")
    if type == "reconciliation_requested":
        require(a.state == "unknown", "event_out_of_order", "reconciliação exige unknown")
        require(p["dispatch_idempotency_key"] == a.intent["dispatch_idempotency_key"] and
                e["idempotency_key"] != a.intent["dispatch_idempotency_key"], "reconciliation_key_mismatch", "reconciliação usa chave original só no payload")
        require(p["external_call_id"] == a.external_call_id, "external_call_mismatch", "chamada de outra intenção")
        deadline = timestamp(a.reconciliation_deadline) if a.reconciliation_deadline else min(
            timestamp(e["recorded_at"]) + timedelta(milliseconds=s.limit("reconciliation_timeout_ms")),
            timestamp(s.intent["created_at"]) + timedelta(milliseconds=s.limit("max_run_duration_ms")))
        require(timestamp(p["deadline_at"]) == deadline, "reconciliation_deadline_mismatch", "prazo inicial da intenção é fixo")
        if a.reconciliation_deadline is None:
            a.reconciliation_deadline = p["deadline_at"]
    elif type == "reconciliation_observed":
        require(a.reconciliation_deadline is not None, "event_out_of_order", "observação sem requisição de reconciliação")
        if p["outcome"] in {"found_finished", "found_failed", "found_stopped"} or (
                p["outcome"] == "not_found" and s.reconcile_not_found_authoritative and p["evidence_refs"]):
            a.termination_certainty = "confirmed"
            a.effect_proven = True
        if p["outcome"] == "found_running":
            require(a.termination_certainty != "confirmed", "termination_evidence_conflict", "execução ativa após término confirmado")
            a.effect_proven = True
            a.termination_certainty = "not_terminated"
        _external_call(a, p["external_call_id"])
    elif type == "dispatch_acknowledged":
        require(a.state == "queued", "event_out_of_order", "ack fora de queued")
        a.effect_proven = True
        _external_call(a, p["external_call_id"])
    elif type == "execution_started":
        require(a.termination_certainty != "confirmed", "termination_evidence_conflict", "início após término confirmado")
        a.effect_proven, a.termination_certainty = True, "not_terminated"
    elif type == "termination_confirmed":
        a.termination_certainty, a.effect_proven = "confirmed", True
    elif type == "result_collected":
        require(a.termination_certainty == "confirmed", "event_out_of_order", "resultado sem término confirmado")
        require(not s.history("attempt", id, type), "duplicate_result", "resultado já coletado")
        a.resolved_model_id = p["resolved_model_id"]


def _external_call(a: AttemptProjection, value: str | None) -> None:
    require(a.external_call_id is None or value is None or value == a.external_call_id,
            "external_call_mismatch", "identidade externa mudou")
    if value is not None:
        a.external_call_id = value


def _close(s: RunProjection, e: dict) -> None:
    p = e["payload"]
    require(s.state in RUN_TERMINAL and all(t.state in TASK_TERMINAL for t in s.tasks.values()),
            "run_close_invalid", "fechamento exige agregados terminais")
    cutoff = p["as_of_global_sequence"]
    require(0 < cutoff <= s.as_of_global_sequence and p["event_hash"] == s.events[f"event-{cutoff}"]["event_hash"],
            "cutoff_invalid", "cutoff deve identificar prefixo anterior")
    require(all(event["event_type"] in {"usage_attached", "late_observation"} for event in s.events.values()
                if event["global_sequence"] > cutoff), "cutoff_invalid", "somente observações/uso podem seguir o cutoff")
    prefix = s if cutoff == s.as_of_global_sequence else project_journal(s.events.values(), workflow=s.workflow,
        as_of_global_sequence=cutoff, reconcile_not_found_authoritative=s.reconcile_not_found_authoritative)
    require(prefix.state in RUN_TERMINAL and all(t.state in TASK_TERMINAL for t in prefix.tasks.values()),
            "cutoff_invalid", "cutoff anterior ao término")
    summary = p["run_summary"]
    require(p["summary_hash"] == canonical_digest(summary), "summary_hash_mismatch", "digest do resumo diverge")
    require(summary["state"] == prefix.state and summary["tasks"] == [{"task_id": t.id, "state": t.state} for t in prefix.tasks.values()] and
            _bindings(summary["output_bindings"]) == _bindings(prefix.output_bindings) and
            set(summary["usage_record_ids"]) == set(prefix.usages) and set(summary["error_record_ids"]) == set(prefix.errors),
            "run_summary_mismatch", "resumo diverge da projeção")
    actual = prefix.cost_summary
    require(all(summary["cost_summary"][k] == actual[k] for k in ("currency", "unavailable_count", "total_cost_exact")) and
            all(Decimal(summary["cost_summary"][k]) == Decimal(actual[k]) for k in ("reported", "estimated")),
            "cost_summary_mismatch", "custo do resumo diverge ou apaga unknown")
    s.published_summary, s.published_cutoff = summary, cutoff


@dataclass(frozen=True)
class RecoveryAction:
    kind: str
    aggregate_id: str
    details: dict = field(default_factory=dict)


def recovery_actions(s: RunProjection) -> tuple[RecoveryAction, ...]:
    """Próxima etapa segura após crash; o chamador grava intenções antes de I/O.

    Retorna um lote de uma etapa: após aplicá-lo, reprojetar antes de avançar.
    Não autoriza dispatch de uma intenção existente, mesmo sem ack ou após fsync
    que falhou no escritor anterior. Tempo/novo orçamento são gates de admissão.
    """
    if s.published_summary is not None:
        return (RecoveryAction("closed", s.id),)
    if s.state in RUN_TERMINAL:
        return (RecoveryAction("close_run", s.id),)
    must_stop = bool(s.cancel_requested or s.run_deadlines or any(t.state in {"failed", "blocked", "indeterminate"}
        for t in s.tasks.values()) or any(e["code"] == "idempotency_conflict" for e in s.errors.values()))
    if s.state == "running" and must_stop:
        return (RecoveryAction("stop_run", s.id),)
    actions: list[RecoveryAction] = []
    for rid, r in s.unassigned_reservations.items():
        if r.state == "open":
            actions.append(RecoveryAction("release_reservation", rid, {"reason": "admission_aborted"}))
    for a in s.attempts.values():
        if a.state in {"queued", "running", "unknown"}:
            if s.tasks[a.header["task_id"]].state == "indeterminate":
                continue
            if a.termination_certainty == "confirmed":
                actions.append(RecoveryAction("resume_attempt_projection", a.id))
                continue
            acknowledged = a.state == "queued" and bool(s.history("attempt", a.id, "dispatch_acknowledged"))
            details = {
                "transition_to": "unknown" if a.state != "unknown" else None,
                "reason": "dispatch_uncertain" if acknowledged else "crash", "dispatch_intent_id": a.intent["dispatch_intent_id"],
                "dispatch_idempotency_key": a.intent["dispatch_idempotency_key"],
                "external_call_id": a.external_call_id,
                "task_transition": "ready_to_running" if s.tasks[a.header["task_id"]].state == "ready" else None}
            if acknowledged:
                details["required_error"] = {"code": "dispatch_failed", "termination_certainty": "unknown"}
            actions.append(RecoveryAction("reconcile_dispatch", a.id, details))
    for ev in s.evaluations.values():
        scope = ev.request["scope"]
        if scope["kind"] == "task_attempt" and s.tasks[scope["task_id"]].state in TASK_TERMINAL:
            continue
        if ev.report is None:
            actions.append(RecoveryAction("record_interrupted_evaluation", ev.id, {
                "execution_status": "failed", "verdict": "not_run"}))
    if actions:
        return tuple(actions)
    if s.state == "stopping":
        return (RecoveryAction("finish_stopping", s.id),)
    if s.manifest is None:
        return (RecoveryAction("resume_preparation", s.id),)
    for task in s.tasks.values():
        if task.state in {"running", "evaluating", "waiting_evaluation", "reconciling"}:
            return (RecoveryAction("resume_task", task.id),)
    for task in s.tasks.values():
        if task.state in {"ready", "retry_wait"} or task.state == "pending" and all(
                s.tasks[d].accepted_event_id for d in task.spec["dependencies"]):
            return (RecoveryAction("resume_admission", task.id),)
    if s.tasks and all(t.state == "succeeded" for t in s.tasks.values()):
        return (RecoveryAction("finalize_global_evaluation" if s.global_report else "request_global_evaluation", s.id),)
    return (RecoveryAction("stop_no_progress", s.id),)
