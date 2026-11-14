"""Runner sequencial local: o journal autoriza efeitos e determina o estado.

``policy`` contém routing_policy, resolved_limits e budget_snapshot. Catálogos,
workflow e pedido são snapshots fornecidos pelo chamador. Hooks pertencem ao
harness: on_event é chamado somente depois do append durável e da projeção.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, localcontext
import json
from pathlib import Path
from typing import Callable

from ._storage import StorageError, confined_path, ensure_directory, publish_bytes, reject_pending_files
from .artifact_store import ArtifactStore
from .canonical import canonical_digest, canonicalize
from .context import ContextError, materialize_knowledge_context
from .evaluator import ArtifactEvidence, ContentContractRegistry, DeterministicEvaluator
from .executor import ExecutorError, Record, WorkspaceHandle, bytes_digest, error_record, seal_record, stable_id
from .journal import JournalError, RunJournal
from .planner import FixedTemplatePlanner, plan_and_validate
from .projections import RunProjection, project_event, project_journal, record_ref, recovery_actions
from .router import DecisionEngine, RoutingError, route_and_finalize
from .selection import select_knowledge
from .simulated_executor import (
    AUTHORITATIVE_NOT_FOUND_CONTROL, DeterministicClock, SimulationScript,
    SimulatedExecutor, format_utc, parse_utc, simulated_output_contract,
)
from .state_machine import RUN_TERMINAL, TASK_TERMINAL, retry_allowed
from .vault import VaultError


def reference(document: Record, field: str, hash_field: str | None = None) -> Record:
    return {"id": document[field], "version": document.get("version", document.get("schema_version")),
            "content_digest": document[hash_field] if hash_field else canonical_digest(document)}


def deterministic_id(prefix: str, *parts: object) -> str:
    candidate = "-".join(str(p) for p in (prefix, *parts))
    return candidate if len(candidate) <= 128 else stable_id(prefix, list(parts))


class SequentialRunner:
    """Uma instância possui o lock até close(); run() retorna a projeção final.

    create é exclusivo; open é retomada explícita e nunca remove locks órfãos.
    clock é compartilhado por journal, executor e avaliador. id_factory deve ser
    determinística e preservar IDs na retomada; eventos seguem event-<sequência>.
    """

    @classmethod
    def create(cls, store_root: Path, *, workflow: Record, request: Record,
               catalog: Record, policy: Record, script: SimulationScript,
               knowledge_catalog: Record, vault_root: Path, clock: DeterministicClock,
               id_factory: Callable = deterministic_id, on_event: Callable | None = None,
               decision_engine: DecisionEngine | None = None,
               external_inputs: dict[str, bytes] | None = None) -> SequentialRunner:
        script.validate_against_workflow(workflow)
        journal = RunJournal.create(store_root, request["run_id"], clock=clock)
        return cls._build(journal, workflow, request, catalog, policy, script, knowledge_catalog,
                          vault_root, clock, id_factory, on_event, decision_engine, external_inputs, False)

    @classmethod
    def open(cls, store_root: Path, *, workflow: Record, request: Record,
             catalog: Record, policy: Record, script: SimulationScript,
             knowledge_catalog: Record, vault_root: Path, clock: DeterministicClock,
             id_factory: Callable = deterministic_id, on_event: Callable | None = None,
             decision_engine: DecisionEngine | None = None,
             external_inputs: dict[str, bytes] | None = None) -> SequentialRunner:
        script.validate_against_workflow(workflow)
        def prepare_recovery(events):
            times = [parse_utc(e["recorded_at"]) for e in events]
            directory = confined_path(Path(store_root), f"runs/{request['run_id']}/simulator")
            for path in sorted(directory.glob("state-*.json")):
                checkpoint = json.loads(confined_path(directory, path.name).read_bytes())
                if checkpoint["checkpoint_hash"] != canonical_digest({k: v for k, v in checkpoint.items()
                                                                      if k != "checkpoint_hash"}):
                    raise ExecutorError("executor_state_unavailable", "checkpoint corrompido")
                times.append(parse_utc(checkpoint["clock"]))
            if times:
                clock.advance_to(format_utc(max(times) +
                                            timedelta(milliseconds=clock.step_ms)))
        journal = RunJournal.open(store_root, request["run_id"], clock=clock, prepare_recovery=prepare_recovery)
        return cls._build(journal, workflow, request, catalog, policy, script, knowledge_catalog,
                          vault_root, clock, id_factory, on_event, decision_engine, external_inputs, True)

    @classmethod
    def _build(cls, journal, workflow, request, catalog, policy, script, knowledge_catalog,
               vault_root, clock, id_factory, on_event, decision_engine, external_inputs, resumed):
        self = cls()
        self.journal, self.clock, self.script = journal, clock, script
        self.workflow, self.request, self.catalog, self.policy, self.knowledge_catalog = deepcopy(
            (workflow, request, catalog, policy, knowledge_catalog))
        self.vault_root, self.ids, self.on_event = Path(vault_root), id_factory, on_event
        self.decision_engine, self.external_inputs = decision_engine, deepcopy(external_inputs or {})
        self.resumed = resumed
        self._undispatched: set[str] = set()  # prova volátil de AT04
        try:
            if not isinstance(clock, DeterministicClock) or journal.clock is not clock:
                raise ExecutorError("clock_mismatch", "runner e journal exigem o mesmo relógio injetado")
            self.store = ArtifactStore(journal)
            self._state_directory = confined_path(journal.directory, "simulator")
            ensure_directory(self._state_directory)
            reject_pending_files(self._state_directory, error_type=StorageError)
            checkpoints = sorted(self._state_directory.glob("state-*.json"))
            self._checkpoint_number = len(checkpoints)
            identity = canonical_digest([workflow, request, catalog, policy, knowledge_catalog, script.reference,
                                         {id: bytes_digest(content) for id, content in self.external_inputs.items()}])
            state = SimulatedExecutor.initial_state(script, journal.run_id)
            if checkpoints:
                checkpoint = json.loads(confined_path(self._state_directory, checkpoints[-1].name).read_bytes())
                if checkpoint["checkpoint_hash"] != canonical_digest({k: v for k, v in checkpoint.items()
                                                                      if k != "checkpoint_hash"}):
                    raise ExecutorError("executor_state_unavailable", "checkpoint corrompido")
                if checkpoint["input_hash"] != identity:
                    raise ExecutorError("snapshot_mismatch", "snapshots da retomada divergentes")
                state = checkpoint["executor_state"]
                clock.advance_to(checkpoint["clock"])
            elif resumed:
                raise ExecutorError("executor_state_unavailable", "retomada exige estado durável do simulado")
            if journal.events:
                clock.advance_to(format_utc(parse_utc(journal.events[-1]["recorded_at"]) +
                                            timedelta(milliseconds=clock.step_ms)))
            self._input_hash = identity
            self.executor = SimulatedExecutor(script, self.store, clock=clock, state=state, persist=self._persist_executor)
            contracts = ContentContractRegistry()
            contracts.register(*simulated_output_contract())
            self.evaluator = DeterministicEvaluator(self.store, script, clock=clock, contracts=contracts)
            self._authoritative = any(c["id"] == AUTHORITATIVE_NOT_FOUND_CONTROL and c["support"] == "supported"
                                      for c in self.executor.describe().controls)
            self.projection = self._project(journal.events)
            return self
        except BaseException:
            journal.close()
            raise

    def _persist_executor(self, state: Record) -> None:
        # Checkpoints imutáveis: publicação exclusiva + fsync pelo armazenamento 6B.
        checkpoint = {"input_hash": self._input_hash, "clock": self.clock.now(), "executor_state": state}
        checkpoint["checkpoint_hash"] = canonical_digest(checkpoint)
        number = self._checkpoint_number + 1
        path = confined_path(self._state_directory, f"state-{number:012d}.json")
        if not publish_bytes(path, canonicalize(checkpoint)):
            raise ExecutorError("executor_state_unavailable", "checkpoint já existe")
        self._checkpoint_number = number

    def close(self) -> None:
        self.journal.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _project(self, events) -> RunProjection:
        return project_journal(events, workflow=self.workflow,
                               reconcile_not_found_authoritative=self._authoritative)

    def append(self, event_type: str, payload: Record, kind: str = "run", id: str | None = None,
               *, key: str | None = None, actor: str = "orchestrator") -> Record:
        """Valida o próximo prefixo antes do fsync; repetições não repetem hooks."""
        id = id or self.journal.run_id
        events = self.journal.events
        if key is not None:
            old = next((e for e in events if e["idempotency_key"] == key), None)
            if old:
                if (old["event_type"], old["aggregate_type"], old["aggregate_id"], old["payload"]) == (
                        event_type, kind, id, payload):
                    return old
                self._error("run", self.journal.run_id, "idempotency_conflict", phase="persistence")
                if self.projection.state == "running":
                    self._transition("run", self.journal.run_id, "stopping", "idempotency_conflict")
                raise JournalError("idempotency_conflict", "chave já usada com outro evento/payload")
        aggregate_sequence = 1 + sum(e["aggregate_type"] == kind and e["aggregate_id"] == id for e in events)
        seq = len(events) + 1
        event = {"schema_version": "0.1", "event_id": f"event-{seq}", "run_id": self.journal.run_id,
                 "global_sequence": seq, "aggregate_type": kind, "aggregate_id": id,
                 "aggregate_sequence": aggregate_sequence, "event_type": event_type,
                 "recorded_at": self.clock.now(), "occurred_at": None,
                 "actor": {"kind": actor, "ref": "orq-runner"}, "correlation_id": id,
                 "causation_event_id": events[-1]["event_id"] if events else None,
                 "idempotency_key": key, "payload": deepcopy(payload), "previous_event_hash": self.journal.final_digest}
        event["event_hash"] = canonical_digest(event)
        projected = project_event(self.projection, event)
        written = self.journal.append(event_type=event_type, aggregate_type=kind, aggregate_id=id,
                                      expected_aggregate_sequence=aggregate_sequence, payload=payload,
                                      actor=event["actor"], causation_event_id=event["causation_event_id"],
                                      idempotency_key=key)
        self.projection = projected
        if self.on_event:
            self.on_event(self, written)
        return written

    def _transition(self, kind: str, id: str, to: str, reason: str) -> Record:
        s = self.projection
        # Evidências do prefixo, explicitamente citadas; os validadores 6C filtram por escopo.
        evidence = [e["event_id"] for e in s.events.values() if e["event_type"] in {
            "state_transition", "dispatch_requested", "dispatch_acknowledged", "execution_started",
            "error_attached", "termination_confirmed", "result_collected", "artifact_ingested", "output_bound",
            "evaluation_requested", "evaluation_report_recorded", "outputs_accepted", "admission_rejected",
            "reconciliation_observed", "cancel_requested", "deadline_exceeded", "run_manifest_closed"}]
        return self.append("state_transition", {"from": s.aggregate_state(kind, id), "to": to,
                                               "reason": reason, "evidence_event_ids": evidence}, kind, id)

    def cancel(self, reason: str = "run_cancelled") -> Record:
        return self.append("cancel_requested", {"reason": reason}, key=self.journal.run_id + "/cancel", actor="human")

    def _error(self, kind, id, code, *, phase="admission", retryability="non_retryable", certainty="confirmed"):
        record = error_record(run_id=self.journal.run_id, scope_type=kind, scope_ref=id, phase=phase,
                              category="integrity" if code == "integrity_mismatch" else "internal",
                              code=code, message=code, timestamp=self.clock.now(),
                              retryability=retryability, termination_certainty=certainty,
                              discriminator=len(self.journal.events))
        return self._attach_error(record, kind, id)

    def _attach_error(self, record, kind, id):
        return self.append("error_attached", record, kind, id, key=record["error_id"], actor="executor")

    def _end(self) -> str:
        s = self.projection
        return format_utc(parse_utc(s.intent["created_at"]) + timedelta(milliseconds=s.limit("max_run_duration_ms")))

    def _deadline(self, milliseconds: int) -> str:
        return format_utc(min(parse_utc(self.clock.now()) + timedelta(milliseconds=milliseconds), parse_utc(self._end())))

    def _prepare(self):
        s = self.projection
        if s.intent is None:
            self.append("run_intent_recorded", seal_record({
                "schema_version": "0.1", "run_id": self.request["run_id"], "project_id": self.request["project_id"],
                "objective": self.request["objective"], "requested_by": {"kind": "human", "ref": "local-user"},
                "experiment_metadata": {}, "created_at": self.clock.now(),
                "idempotency_key": self.request["run_id"] + "/create",
            }, "run_intent"))
        if self.projection.state == "created":
            self._transition("run", self.journal.run_id, "running", "preparation_started")
        s = self.projection
        if s.manifest:
            return
        plan = next((r for r in s.records.values() if "planning_record_id" in r), None)
        if not s.cancel_requested and plan is None:
            planner = FixedTemplatePlanner([self.workflow], {
                "id": "planner-fixed", "version": "0.1", "content_digest": canonical_digest("fixed_template")})
            plan = plan_and_validate(planner, self.request, completed_at=self.clock.now()).record
            self.append("planning_recorded", plan)
        status = "cancelled_before_ready" if self.projection.cancel_requested else (
            "ready" if plan and plan["status"] == "accepted" else "planning_failed")
        ready = status == "ready"
        inputs = []
        if ready:
            for spec in self.workflow["external_inputs"]:
                if spec["input_id"] not in self.external_inputs:
                    if spec.get("required", True):
                        raise ExecutorError("inputs_unavailable", "entrada externa obrigatória ausente")
                    continue
                aid = self.ids("external", spec["input_id"])
                if aid not in self.projection.artifacts:
                    self._ingest(self.external_inputs[spec["input_id"]], aid, spec["artifact_type"],
                                 spec["content_contract_ref"], {"kind": "external_input", "run_id": s.id,
                                 "input_id": spec["input_id"], "source_ref": {
                                     "id": self.ids("input", spec["input_id"]), "version": "1",
                                     "content_digest": bytes_digest(self.external_inputs[spec["input_id"]])}}, "run", s.id)
                inputs.append({"input_id": spec["input_id"], "artifact": self._artifact_ref(self.projection.artifacts[aid])})
        if self.projection.cancel_requested:
            status, ready, inputs = "cancelled_before_ready", False, []
        manifest = {
            "schema_version": "0.1", "run_id": s.id, "run_intent_ref": record_ref(s.intent, "run_id"),
            "manifest_status": status, "created_at": s.intent["created_at"], "closed_at": self.clock.now(),
            "project_id": s.intent["project_id"], "objective_snapshot": {
                "objective": s.intent["objective"], "objective_hash": canonical_digest(s.intent["objective"])},
            "planning_record_ref": record_ref(plan, "planning_record_id") if plan else None,
            "workflow_snapshot_ref": reference(self.workflow, "workflow_id") if ready else None,
            "execution_policy_snapshot_ref": self.workflow["execution_policy_ref"] if ready else None,
            "resolved_limits": self.policy["resolved_limits"] if ready else None,
            "budget_snapshot": self.policy["budget_snapshot"] if ready else None,
            "routing_catalog_ref": reference(self.catalog, "catalog_id", "catalog_hash") if ready else None,
            "routing_policy_ref": reference(self.policy["routing_policy"], "routing_policy_id") if ready else None,
            "knowledge_catalog_snapshot_ref": reference(self.knowledge_catalog, "catalog_id", "catalog_hash") if ready else None,
            "knowledge_status_assertions": self.policy.get("knowledge_status_assertions", []) if ready else [],
            "external_input_bindings": inputs,
            "executor_snapshot": {"executor_ref": self.executor.describe().executor_ref, "adapter_version": "0.1",
                                  "script_ref": self.script.reference} if ready else None,
            "evaluator_snapshot": self.evaluator.snapshot if ready else None,
            "orchestrator_snapshot": {"version": "0.1", "config_ref": {
                "id": "runner-config", "version": "0.1", "content_digest": canonical_digest(self.policy)},
                "clock": "injected"}, "scheduling_policy": {"max_concurrency": 1, "tie_break": "workflow_task_order"},
            "experiment_metadata": s.intent["experiment_metadata"],
            "preparation_coverage": {"completed": ["planning", "limits", "catalogs", "inputs"] if ready else [],
                                     "missing": [] if ready else ["execution"], "reason": None if ready else status},
            "preparation_usage_refs": [], "preparation_error_refs": [],
        }
        self.append("run_manifest_closed", seal_record(manifest, "run_manifest"))
        if not ready:
            self._transition("run", s.id, "cancelled" if status == "cancelled_before_ready" else "failed", status)

    @staticmethod
    def _artifact_ref(descriptor):
        return {k: descriptor[k] for k in ("artifact_id", "content_digest")}

    def _ingest(self, content, artifact_id, artifact_type, contract, producer, kind, id):
        locator = self.store.put(content)
        descriptor = {"schema_version": "0.1", "artifact_id": artifact_id, "artifact_type": artifact_type,
                      "content_contract_ref": contract, "content_digest": bytes_digest(content),
                      "size_bytes": len(content), "media_type": "application/json", "locator": locator,
                      "producer": producer, "created_at": self.clock.now(), "derived_from": [],
                      "data_classification": "internal"}
        self.append("artifact_ingested", descriptor, kind, id)
        return descriptor

    def _reject(self, task_id, reason, code=None):
        for rid, r in list(self.projection.unassigned_reservations.items()):
            if r.state == "open":
                self.append("budget_released", {"reservation_id": rid, "reason": "admission_aborted"})
        error_code = code or {"attempts_exhausted": "execution_failed", "run_deadline": "timeout",
            "inputs_unavailable": "context_rejected", "knowledge_unsatisfied": "context_rejected",
            "policy_rejected": "context_rejected", "low_confidence": "no_eligible_target",
            "invalid_response": "no_eligible_target", "provider_error": "no_eligible_target",
            "preflight_rejected": "no_eligible_target"}.get(reason, reason)
        error = self._error("task", task_id, error_code)
        self.append("admission_rejected", {"reason": reason, "error_record_ids": [error["payload"]["error_id"]]}, "task", task_id)
        self._transition("task", task_id, "failed", "admission_rejected")

    def _inputs(self, task):
        bindings = []
        s = self.projection
        for spec in task["inputs"]:
            source = spec["source"]
            if source["kind"] == "task_output":
                dep = s.tasks[source["task_id"]]
                binding = next((b for b in dep.accepted_bindings if b["output_id"] == source["output_id"]), None)
                basis = {"kind": "outputs_accepted", "event_id": dep.accepted_event_id}
            else:
                binding = next((b for b in s.manifest["external_input_bindings"] if b["input_id"] == source["input_id"]), None)
                basis = {"kind": "external_input_binding", "event_id": f"event-{s.manifest_sequence}"}
            if binding:
                descriptor = s.artifacts.get(binding["artifact"]["artifact_id"])
                if not descriptor or descriptor["content_digest"] != binding["artifact"]["content_digest"] or (
                    descriptor["artifact_type"] != spec["artifact_type"] or spec.get("content_contract_ref") and
                    descriptor["content_contract_ref"] != spec["content_contract_ref"]):
                    raise ExecutorError("inputs_unavailable", "descritor incompatível com o input")
                content = self.store.read(descriptor["locator"])
                if len(content) != descriptor["size_bytes"]:
                    raise ExecutorError("inputs_unavailable", "tamanho do input diverge do descritor")
                bindings.append({"input_id": spec["input_id"], "artifact": binding["artifact"], "basis": basis})
            elif spec.get("required", True):
                raise ExecutorError("inputs_unavailable", "input obrigatório sem binding aceito")
        return bindings

    def _estimate(self, scope, subject):
        estimates = [e for e in self.projection.manifest["budget_snapshot"]["cost_estimates"]
                     if e["scope"] == scope and e["subject_ref"] == subject]
        return max(estimates, key=lambda e: Decimal(e["amount"])) if estimates else None

    def _budget_check(self, operations):
        s = self.projection
        budget = s.manifest["budget_snapshot"]
        estimates = [self._estimate(scope, subject) for scope, subject in operations]
        if (budget["cap_mandatory"] or budget["unknown_cost_action"] == "reject") and any(e is None for e in estimates):
            return "budget_unknown"
        if budget["cap_mandatory"] and s.budget_gaps:
            return "budget_unknown"
        committed = s._budget_commitment()[0]
        if budget["max_cost"] is not None:
            with localcontext() as ctx:
                ctx.prec = 64 + sum(len(e["amount"]) for e in estimates if e)
                if committed + sum((Decimal(e["amount"]) for e in estimates if e), Decimal(0)) > Decimal(budget["max_cost"]):
                    return "budget_exceeded"
        return None

    def _reserve(self, scope, id, subject):
        estimate = self._estimate(scope, subject)
        # O número da sequência evita reutilizar reservas abandonadas por crash.
        rid = self.ids("reservation", id, len(self.journal.events) + 1)
        self.append("budget_reserved", {"reservation_id": rid, "scope_type": scope, "scope_ref": id,
                    "amount": estimate["amount"] if estimate else None, "status": "estimated" if estimate else "unavailable",
                    "currency": self.projection.manifest["budget_snapshot"]["currency"],
                    "method": estimate["method"] if estimate else "unavailable"})
        return rid

    def _admit(self, task_id):
        s = self.projection
        task = s.tasks[task_id].spec
        number = len(s.tasks[task_id].attempt_ids) + 1
        if number > s.limit("max_attempts", task_id):
            return self._reject(task_id, "attempts_exhausted")
        if not s.time_remaining(self.clock.now()):
            return self._reject(task_id, "run_deadline")
        try:
            bindings = self._inputs(task)
        except (ExecutorError, StorageError):
            return self._reject(task_id, "inputs_unavailable")
        try:
            selection = select_knowledge(self.vault_root, self.knowledge_catalog, task["knowledge_requirements"],
                query=task["objective"], run_id=s.id, workflow_ref=s.manifest["workflow_snapshot_ref"], task_id=task_id,
                candidate_attempt_number=number, project_id=s.intent["project_id"], created_at=self.clock.now(),
                max_tokens=self.policy.get("knowledge_max_tokens", 2048),
                selection_id=self.ids("selection", task_id, number, len(self.journal.events) + 1),
                policy_snapshot_refs=[s.manifest["execution_policy_snapshot_ref"]])
            self.append("knowledge_selection_recorded", selection, "task", task_id)
            if self.projection.cancel_requested:
                return
            if selection["status"] not in {"satisfied", "partial"}:
                return self._reject(task_id, "knowledge_unsatisfied")
            request = {"schema_version": "0.1", "routing_request_id": self.ids("routing", task_id, number, len(self.journal.events)+1),
                       "run_id": s.id, "workflow_ref": s.manifest["workflow_snapshot_ref"], "task_snapshot": task,
                       "candidate_attempt_number": number, "catalog_ref": s.manifest["routing_catalog_ref"],
                       "execution_policy_snapshot_ref": s.manifest["execution_policy_snapshot_ref"],
                       "routing_policy_ref": s.manifest["routing_policy_ref"],
                       "estimated_context_input_tokens": selection["budget"]["estimated_tokens"],
                       "required_output_tokens": self.policy.get("reserved_output_tokens", 256),
                       "required_classification": "internal", "required_permission_ids": [],
                       "required_control_ids": ["timeout", "cancellation"], "created_at": self.clock.now()}
            decision = route_and_finalize(request, self.catalog, self.policy["routing_policy"],
                preflight_checks=[{"control": c, "status": "passed", "observed_at": self.clock.now(), "evidence_refs": [self.script.reference]}
                                  for c in ("credentials", "quota", "limits")], decision_engine=self.decision_engine)
            self.append("routing_decision_recorded", decision, "task", task_id)
            if self.projection.cancel_requested:
                return
            if decision["effective_selection"] is None:
                return self._reject(task_id, decision["decision_status"])
            target = next(t for t in self.catalog["targets"] if t["target_id"] == decision["effective_selection"]["selected_target_id"])
            target_ref = reference(target, "target_id")
            context = materialize_knowledge_context(self.vault_root, self.knowledge_catalog, selection,
                routing_decision_ref=record_ref(decision, "routing_decision_id"), target_ref=target_ref,
                target_context_limit_tokens=target["context_limit_tokens"], reserved_output_tokens=request["required_output_tokens"],
                context_manifest_id=self.ids("context", task_id, number, len(self.journal.events)+1))
            manifest = deepcopy(context.manifest)
            if manifest["routing_decision_ref"] != record_ref(decision, "routing_decision_id"):
                return self._reject(task_id, "context_rejected", "integrity_mismatch")
            self.append("context_manifest_recorded", manifest, "task", task_id)
            if self.projection.cancel_requested:
                return
        except (VaultError, ContextError, RoutingError) as error:
            return self._reject(task_id, "context_rejected" if isinstance(error, ContextError) else "policy_rejected")
        aid = self.ids("attempt", task_id, number)
        eid = self.ids("evaluation", task_id, number, 1)
        operations = [("attempt", target_ref), ("evaluation", self.evaluator.snapshot["evaluator_ref"])]
        budget_error = self._budget_check(operations)
        if budget_error:
            return self._reject(task_id, budget_error)
        reservations = [self._reserve("attempt", aid, target_ref)]
        if self.projection.cancel_requested:
            return
        reservations.append(self._reserve("evaluation", eid, self.evaluator.snapshot["evaluator_ref"]))
        if self.projection.cancel_requested:
            return
        if bytes_digest(context.bundle) != manifest["bundle_hash"] or manifest["routing_decision_ref"] != record_ref(decision, "routing_decision_id"):
            return self._reject(task_id, "context_rejected", "integrity_mismatch")
        # Além do cutoff, o manifesto pode fixar revogações posteriores ao catálogo.
        now = self.clock.now()
        for item in manifest["items"]:
            unit = item["knowledge_provenance"]["unit_ref"]
            source_ref = {"source_id": unit["source_id"], "revision": unit["revision"], "content_digest": unit["source_digest"]}
            assertions = [a for a in (*self.knowledge_catalog["status_assertions"], *s.manifest["knowledge_status_assertions"])
                          if a["source_ref"] == source_ref and parse_utc(a["effective_at"]) <= parse_utc(now)]
            if assertions:
                latest = max(parse_utc(a["effective_at"]) for a in assertions)
                if any(a["status"] != "active" for a in assertions if parse_utc(a["effective_at"]) == latest):
                    return self._reject(task_id, "knowledge_revoked")
        if not self.projection.time_remaining(now):
            return self._reject(task_id, "run_deadline")
        base = {"artifact_id": "workspace-empty", "content_digest": bytes_digest(b"")}
        dispatch_id = self.ids("dispatch", task_id, number)
        common = {"schema_version": "0.1", "run_id": s.id, "task_id": task_id, "attempt_id": aid,
                  "attempt_number": number, "workflow_ref": s.manifest["workflow_snapshot_ref"],
                  "routing_decision_ref": record_ref(decision, "routing_decision_id"),
                  "context_manifest_ref": record_ref(manifest, "context_manifest_id"), "workspace_base_ref": base,
                  "input_bindings": bindings, "budget_reservation_ids": reservations, "dispatch_intent_id": dispatch_id,
                  "dispatch_idempotency_key": s.id + "/" + dispatch_id, "created_at": now,
                  "deadline_at": self._deadline(s.limit("attempt_timeout_ms", task_id))}
        header = seal_record({**common, "previous_attempt_id": s.tasks[task_id].attempt_ids[-1] if number > 1 else None,
                              "effective_policy_snapshot_ref": s.manifest["execution_policy_snapshot_ref"],
                              "execution_target_snapshot": {"target_ref": target_ref, **{k: target[k] for k in (
                                  "model_ref", "provider_ref", "runtime_ref", "executor_ref", "execution_config_ref")}}}, "attempt_header")
        intent = seal_record({**common, "target_ref": target_ref, "executor_ref": target["executor_ref"],
                             "bundle_hash": manifest["bundle_hash"], "attempt_timeout_ms": s.limit("attempt_timeout_ms", task_id),
                             "admission_checks": [{"control": c, "status": "passed", "observed_at": now,
                                                   "evidence_refs": [record_ref(manifest, "context_manifest_id")]}
                              for c in ("context_integrity", "knowledge_status", "attempts_remaining", "run_time_remaining", "budget", "inputs_accepted")]}, "dispatch_intent")
        # Marcar antes do callback; somente este processo pode provar que não despachou.
        self._undispatched.add(aid)
        self.append("dispatch_requested", {"attempt_header": header, "dispatch_intent": intent}, "attempt", aid,
                    key=intent["dispatch_idempotency_key"])
        self._transition("task", task_id, "running", "dispatch_requested")
        if self.projection.cancel_requested:
            return
        self._undispatched.remove(aid)
        workspace = WorkspaceHandle(self.ids("workspace", task_id, number), base, tuple(task["expected_output"]), target["model_ref"]["id"])
        try:
            ack = self.executor.dispatch(intent, context.bundle, workspace)
        except ExecutorError as error:
            if error.termination_certainty == "confirmed":
                self._error("attempt", aid, "dispatch_failed", phase="dispatch", retryability=error.retryability)
                self._transition("attempt", aid, "failed", "dispatch_failed")
            else:
                self._unknown(aid, "dispatch_uncertain")
            return
        for error in ack.errors:
            self._attach_error(error, "attempt", aid)
        if ack.errors and all(e["termination_certainty"] == "confirmed" for e in ack.errors):
            self._transition("attempt", aid, "failed", "dispatch_failed")
        elif ack.acknowledged:
            self.append("dispatch_acknowledged", {"external_call_id": ack.external_call_id}, "attempt", aid,
                        key=intent["dispatch_idempotency_key"] + "/ack", actor="executor")
            self.append("execution_started", {"observed_at": ack.observed_at}, "attempt", aid,
                        key=intent["dispatch_idempotency_key"] + "/started", actor="executor")
            self._transition("attempt", aid, "running", "execution_started")
        else:
            self._unknown(aid, "dispatch_uncertain")

    def _unknown(self, aid, reason):
        self._error("attempt", aid, "dispatch_failed" if self.projection.attempts[aid].state == "queued" else "execution_failed",
                    phase="dispatch" if self.projection.attempts[aid].state == "queued" else "observe", certainty="unknown", retryability="unknown")
        self._transition("attempt", aid, "unknown", reason)
        tid = self.projection.attempts[aid].header["task_id"]
        if self.projection.tasks[tid].state == "running":
            self._transition("task", tid, "reconciling", reason)

    def _collect(self, aid):
        a = self.projection.attempts[aid]
        result = self.executor.collect(a.intent)
        if not self.projection.history("attempt", aid, "result_collected"):
            self.append("result_collected", {"requested_model_id": result.requested_model_id,
                        "resolved_model_id": result.resolved_model_id, "provider_context_coverage": result.provider_context_coverage},
                        "attempt", aid, key=a.intent["dispatch_idempotency_key"] + "/result", actor="executor")
        for usage in result.usage:
            self.append("usage_attached", usage, "attempt", aid, key=usage["usage_id"], actor="executor")
        for error in result.errors:
            self._attach_error(error, "attempt", aid)
        task = self.projection.tasks[a.header["task_id"]].spec
        failed = False
        for file in result.produced_files:
            artifact_id = self.ids("artifact", aid, file.output_id)
            if artifact_id not in self.projection.artifacts:
                self._ingest(file.content, artifact_id, file.artifact_type, file.content_contract_ref,
                    {"kind": "attempt_output", "run_id": self.journal.run_id, "task_id": task["task_id"], "attempt_id": aid}, "attempt", aid)
            spec = next((o for o in task["expected_output"] if o["output_id"] == file.output_id), None)
            code = "integrity_mismatch" if bytes_digest(file.content) != file.declared_digest else (
                "output_contract_mismatch" if not spec or file.content_contract_ref != spec["content_contract_ref"] or
                file.artifact_type != spec["artifact_type"] else None)
            if code is None:
                validator = self.evaluator.contracts.validator(file.content_contract_ref)
                try:
                    if validator is None or not validator.is_valid(json.loads(file.content)):
                        code = "output_contract_mismatch"
                except (UnicodeError, ValueError):
                    code = "output_contract_mismatch"
            if code:
                self._error("attempt", aid, code, phase="collect")
                failed = True
            elif not any(b["output_id"] == file.output_id for b in self.projection.attempts[aid].output_bindings):
                self.append("output_bound", {"output_id": file.output_id, "artifact": self._artifact_ref(self.projection.artifacts[artifact_id])}, "attempt", aid)
        if not self.projection.outputs_complete(self.projection.attempts[aid]):
            self._error("attempt", aid, "output_missing", phase="collect")
            failed = True
        self._settle_attempt(aid)
        self._transition("attempt", aid, "failed" if failed else "completed", "output_invalid" if failed else "outputs_complete")

    def _settle_attempt(self, aid):
        s = self.projection
        for rid in s.attempts[aid].intent["budget_reservation_ids"]:
            r = self.projection.reservations[rid]
            if r.state == "open" and r.reservation["scope_type"] == "attempt":
                usages = [u["usage_id"] for u in self.projection.usages.values() if u["scope_type"] == "attempt" and u["scope_ref"] == aid]
                if usages:
                    self.append("budget_settled", {"reservation_id": rid, "usage_ids": usages})
                elif self.projection.dispatch_proven_not_executed(self.projection.attempts[aid]):
                    self.append("budget_released", {"reservation_id": rid, "reason": "not_executed"})

    def _observe(self, aid):
        a = self.projection.attempts[aid]
        try:
            observation = self.executor.observe(a.intent, a.intent["deadline_at"])
        except ExecutorError:
            return self._unknown(aid, "termination_unknown")
        if observation.status in {"finished", "failed"}:
            self.append("termination_confirmed", {"cause": "finished" if observation.status == "finished" else "failed",
                        "observed_at": observation.observed_at}, "attempt", aid, key=a.intent["dispatch_idempotency_key"] + "/termination", actor="executor")
            if observation.status == "finished":
                self._collect(aid)
            else:
                self._finish_failure(aid)
        elif observation.status == "unknown":
            self._unknown(aid, "termination_unknown")
        else:
            self.append("deadline_exceeded", {"deadline_at": a.intent["deadline_at"], "observed_at": self.clock.now()}, "attempt", aid,
                        key=a.intent["dispatch_idempotency_key"] + "/deadline", actor="clock")
            self._cancel_attempt(aid, timeout=True)

    def _finish_failure(self, aid):
        a = self.projection.attempts[aid]
        result = self.executor.collect(a.intent)
        for error in result.errors:
            self._attach_error(error, "attempt", aid)
        for usage in result.usage:
            self.append("usage_attached", usage, "attempt", aid, key=usage["usage_id"], actor="executor")
        self._settle_attempt(aid)
        self._transition("attempt", aid, "failed", "execution_failed")

    def _cancel_attempt(self, aid, *, timeout=False):
        a = self.projection.attempts[aid]
        number = len(self.projection.history("attempt", aid, "cancel_requested")) + 1
        self.append("cancel_requested", {"reason": "attempt_timeout" if timeout else "run_cancelled"}, "attempt", aid,
                    key=f"{a.intent['dispatch_idempotency_key']}/cancel/{number}")
        try:
            observation = self.executor.cancel(a.intent, "attempt_timeout" if timeout else "run_cancelled")
        except ExecutorError:
            if a.state != "unknown":
                self._unknown(aid, "termination_unknown")
            return False
        if a.state == "unknown":
            # AT12–AT15 exigem observação de reconciliação, não só confirmação de cancel.
            return observation.termination_certainty == "confirmed"
        if observation.termination_certainty == "confirmed":
            # cancel tardio pode confirmar resultado; observe distingue finished/failed/stopped.
            observed = self.executor.observe(a.intent, self.clock.now())
            if observed.status in {"finished", "failed"}:
                return self._observe(aid)
            self.append("termination_confirmed", {"cause": "timeout" if timeout else "cancel", "observed_at": observation.observed_at},
                        "attempt", aid, key=a.intent["dispatch_idempotency_key"] + "/termination", actor="executor")
            result = self.executor.collect(a.intent)
            for usage in result.usage:
                self.append("usage_attached", usage, "attempt", aid, key=usage["usage_id"], actor="executor")
            self._settle_attempt(aid)
            self._transition("attempt", aid, "timed_out" if timeout else "cancelled", "attempt_timeout" if timeout else "run_cancelled")
        elif a.state != "unknown":
            self._unknown(aid, "termination_unknown")

    def _reconcile(self, aid):
        a = self.projection.attempts[aid]
        if not a.reconciliation_deadline:
            # append usará exatamente now() para recorded_at, como exige o 6C.
            deadline = self._deadline(self.projection.limit("reconciliation_timeout_ms"))
        else:
            deadline = a.reconciliation_deadline
        self.append("reconciliation_requested", {"dispatch_idempotency_key": a.intent["dispatch_idempotency_key"],
                    "external_call_id": a.external_call_id, "deadline_at": deadline}, "attempt", aid,
                    key=a.intent["dispatch_idempotency_key"] + "/reconcile/" + str(len(self.journal.events)+1))
        if self.projection.cancel_requested and self.projection.state == "running":
            self._transition("run", self.journal.run_id, "stopping", "run_cancelled")
        try:
            observation = self.executor.reconcile(a.intent["dispatch_idempotency_key"], a.external_call_id)
        except ExecutorError:
            self._error("attempt", aid, "execution_failed", phase="reconcile", certainty="unknown", retryability="unknown")
            self.clock.advance_to(deadline)
            return self._transition("task", a.header["task_id"], "indeterminate", "reconciliation_timeout")
        outcome = observation.outcome
        if outcome == "not_found" and not (self._authoritative and observation.authoritative and observation.evidence_refs):
            outcome = "unknown"
        self.append("reconciliation_observed", {"outcome": outcome, "external_call_id": observation.external_call_id,
                    "evidence_refs": list(observation.evidence_refs)}, "attempt", aid,
                    key=a.intent["dispatch_idempotency_key"] + "/observation/" + str(len(self.journal.events)+1), actor="executor")
        self._apply_reconciliation(aid, observation.errors)

    def _apply_reconciliation(self, aid, errors=()):
        """Conclui a partir da evidência durável, inclusive após crash."""
        a = self.projection.attempts[aid]
        observed = self.projection.history("attempt", aid, "reconciliation_observed")[-1]
        outcome = observed["payload"]["outcome"]
        deadline = a.reconciliation_deadline
        if outcome == "found_finished":
            self._collect(aid)
        elif outcome in {"found_failed", "not_found"}:
            if outcome == "found_failed":
                result = self.executor.collect(a.intent)
                errors = result.errors or errors
                for usage in result.usage:
                    self.append("usage_attached", usage, "attempt", aid, key=usage["usage_id"], actor="executor")
            # Um crash pode ocorrer após o ErrorRecord e antes de AT13/AT15.
            prior = [e for e in self.projection.history("attempt", aid, "error_attached")
                     if e["global_sequence"] > observed["global_sequence"] and e["payload"]["termination_certainty"] == "confirmed"]
            if not prior:
                if errors:
                    for error in errors:
                        self._attach_error(error, "attempt", aid)
                else:
                    self._error("attempt", aid, "dispatch_not_executed" if outcome == "not_found" else "execution_failed",
                                phase="reconcile", retryability="retryable" if outcome == "not_found" else "unknown")
            self._settle_attempt(aid)
            if outcome == "not_found" and self.projection.state == "stopping":
                self._transition("attempt", aid, "cancelled", "run_cancelled")
            else:
                self._transition("attempt", aid, "failed", "dispatch_not_executed" if outcome == "not_found" else "execution_failed")
        elif outcome == "found_stopped":
            timeout = any(e["global_sequence"] < observed["global_sequence"]
                          for e in self.projection.history("attempt", aid, "deadline_exceeded"))
            cancelled = any(e["global_sequence"] < observed["global_sequence"] for e in
                            self.projection.history("attempt", aid, "cancel_requested") + self.projection.cancel_requested)
            result = self.executor.collect(a.intent)
            for usage in result.usage:
                self.append("usage_attached", usage, "attempt", aid, key=usage["usage_id"], actor="executor")
            self._settle_attempt(aid)
            if timeout:
                self._transition("attempt", aid, "timed_out", "attempt_timeout")
            elif self.projection.state == "stopping" and cancelled:
                self._transition("attempt", aid, "cancelled", "run_cancelled")
            else:
                self._error("attempt", aid, "execution_failed", phase="reconcile", retryability="unknown")
                self._transition("attempt", aid, "failed", "execution_failed")
        elif outcome == "found_running" and self.projection.state == "running" and not self.projection.cancel_requested and parse_utc(self.clock.now()) < parse_utc(a.intent["deadline_at"]):
            self._transition("attempt", aid, "running", "reconciled_running")
        else:
            if outcome == "found_running":
                timeout = bool(self.projection.history("attempt", aid, "deadline_exceeded"))
                if not timeout and parse_utc(self.clock.now()) >= parse_utc(a.intent["deadline_at"]):
                    self.append("deadline_exceeded", {"deadline_at": a.intent["deadline_at"], "observed_at": self.clock.now()},
                                "attempt", aid, key=a.intent["dispatch_idempotency_key"] + "/deadline", actor="clock")
                    timeout = True
                if self._cancel_attempt(aid, timeout=timeout):
                    return self._reconcile(aid)
            if self.projection.attempts[aid].state == "unknown":
                self.clock.advance_to(deadline)
                tid = a.header["task_id"]
                self._transition("task", tid, "indeterminate", "reconciliation_timeout")

    def _release_evaluation(self, ev):
        for rid in ev.request["budget_reservation_ids"]:
            if self.projection.reservations[rid].state == "open":
                self.append("budget_released", {"reservation_id": rid, "reason": "evaluation_finished"})

    def _evaluate(self, task_id=None, *, interrupted=False):
        s = self.projection
        a = s.latest_attempt(task_id) if task_id else None
        number = len(s.task_evaluations(task_id)) + 1 if task_id else 1
        eid = self.ids("evaluation", task_id, a.header["attempt_number"], number) if task_id else self.ids("evaluation", "workflow", 1)
        scope = {"kind": "task_attempt", "task_id": task_id, "attempt_id": a.id} if task_id else {"kind": "workflow"}
        criteria = s.tasks[task_id].spec["evaluation_criteria"] if task_id else self.workflow["evaluation_criteria"]
        bindings = a.output_bindings if a else s.output_bindings
        reservations = [rid for rid, r in s.reservations.items() if r.state == "open" and r.reservation["scope_type"] == "evaluation" and r.reservation["scope_ref"] == eid]
        if not reservations:
            if interrupted:
                raise ExecutorError("reservation_missing", "avaliação interrompida exige reserva existente")
            budget_error = self._budget_check([("evaluation", self.evaluator.snapshot["evaluator_ref"])])
            if budget_error:
                # Falha financeira definitiva, sem iniciar uma avaliação nem fabricar relatório.
                self._error("run", s.id, budget_error, phase="evaluation")
                self._transition("run", s.id, "stopping", budget_error)
                return
            reservations = [self._reserve("evaluation", eid, self.evaluator.snapshot["evaluator_ref"])]
            if self.projection.cancel_requested:
                # Cancelamento chegou durante a reserva: não inicia a avaliação e devolve a reserva.
                self.append("budget_released", {"reservation_id": reservations[0], "reason": "admission_aborted"})
                return
        self.append("evaluation_requested", {"evaluation_id": eid, "evaluation_number": number, "scope": scope,
                    "subject_bindings": bindings, "criteria_snapshot": {"criteria": criteria, "criteria_hash": canonical_digest(criteria)},
                    "evaluator_snapshot": self.evaluator.snapshot, "deadline_at": self._deadline(s.limit("evaluation_timeout_ms", task_id)),
                    "budget_reservation_ids": reservations}, "evaluation", eid)
        if task_id and self.projection.tasks[task_id].state == "waiting_evaluation":
            self._transition("task", task_id, "evaluating", "reevaluation_requested")
        if interrupted:
            return self._interrupted_evaluation(eid)
        result = self.evaluator.evaluate(run_id=s.id, evaluation_id=eid, evaluation_number=number, scope=scope,
                    subject_bindings=bindings, criteria=criteria,
                    artifacts={id: ArtifactEvidence(d) for id, d in self.projection.artifacts.items()},
                    attempt_number=a.header["attempt_number"] if a else None)
        for error in result.errors:
            self._attach_error(error, "evaluation", eid)
        if result.report is not None:
            self.append("evaluation_report_recorded", result.report, "evaluation", eid, key=eid + "/report", actor="evaluator")
            self._release_evaluation(self.projection.evaluations[eid])
        elif task_id:
            self._transition("task", task_id, "reconciling", "evaluation_unknown")
            deadline = self._deadline(s.limit("reconciliation_timeout_ms"))
            self.clock.advance_to(deadline)
            self._transition("task", task_id, "indeterminate", "reconciliation_timeout")
        else:
            self._error("evaluation", eid, "evaluator_failed", phase="evaluation", certainty="unknown", retryability="unknown")
            self.clock.advance_to(self._deadline(s.limit("reconciliation_timeout_ms")))
            self._transition("run", s.id, "stopping", "evaluation_unknown")

    def _task_step(self, tid):
        s = self.projection
        task, a = s.tasks[tid], s.latest_attempt(tid)
        if a.state == "running":
            if task.state == "reconciling":
                self._transition("task", tid, "running", "reconciled_running")
            return self._observe(a.id)
        if a.state == "unknown":
            if task.state == "running":
                self._transition("task", tid, "reconciling", "termination_unknown")
            return self._reconcile(a.id)
        if a.state in {"failed", "timed_out"}:
            self._settle_attempt(a.id)
            self._release_unused_evaluation(a)
            return self._transition("task", tid, "retry_wait" if retry_allowed(self.projection, tid, self.clock.now()) else "failed", "attempt_failed")
        if a.state == "cancelled":
            self._settle_attempt(a.id)
            self._release_unused_evaluation(a)
            return self._transition("task", tid, "cancelled", "run_cancelled")
        if a.state == "completed":
            if task.state in {"running", "reconciling"}:
                return self._transition("task", tid, "evaluating", "attempt_completed")
            evaluations = s.task_evaluations(tid)
            if not evaluations or task.state == "waiting_evaluation":
                return self._evaluate(tid)
            ev = evaluations[-1]
            if ev.report is None:
                return
            self._release_evaluation(ev)
            report = ev.report
            if report["verdict"] == "pass":
                if not task.accepted_event_id:
                    self.append("outputs_accepted", {"task_id": tid, "evaluation_id": ev.id, "output_bindings": a.output_bindings}, "task", tid)
                return self._transition("task", tid, "succeeded", "evaluation_pass")
            if report["verdict"] == "fail":
                return self._transition("task", tid, "retry_wait" if retry_allowed(self.projection, tid, self.clock.now()) else "failed", "evaluation_fail")
            capacity = self.projection.evaluation_capacity(tid)
            return self._transition("task", tid, "waiting_evaluation" if capacity else "failed", "evaluation_inconclusive")

    def _release_unused_evaluation(self, a):
        for rid in a.intent["budget_reservation_ids"]:
            r = self.projection.reservations[rid]
            if r.state == "open" and r.reservation["scope_type"] == "evaluation" and r.reservation["scope_ref"] not in self.projection.evaluations:
                self.append("budget_released", {"reservation_id": rid, "reason": "admission_aborted"})

    def _interrupted_evaluation(self, eid):
        ev = self.projection.evaluations[eid]
        request = ev.request
        if request["scope"]["kind"] == "task_attempt":
            tid = request["scope"]["task_id"]
            if self.projection.tasks[tid].state == "waiting_evaluation":
                self._transition("task", tid, "evaluating", "reevaluation_requested")
        report = {"schema_version": "0.1", "run_id": self.journal.run_id,
                  **{k: request[k] for k in ("evaluation_id", "evaluation_number", "scope", "subject_bindings", "criteria_snapshot", "evaluator_snapshot")},
                  "patch_application_refs": [], "context_manifest_ref": None, "started_at": self.clock.now(),
                  "completed_at": self.clock.now(), "execution_status": "failed", "verdict": "not_run",
                  "criterion_results": [{"criterion_id": c["criterion_id"], "required": c["required"],
                      "requirement_ids": c.get("requirement_ids", c.get("workflow_requirement_ids")), "method": c["method"],
                      "status": "not_run", "evidence_refs": [], "observations": [], "error_record_ids": []}
                      for c in request["criteria_snapshot"]["criteria"]],
                  "usage_record_ids": [], "error_record_ids": [], "evidence_refs": [],
                  "limitations": ["Avaliação interrompida por crash ou parada do run."], "unknowns": []}
        self.append("evaluation_report_recorded", seal_record(report, "evaluation_report"), "evaluation", eid,
                    key=eid + "/report", actor="evaluator")
        self._release_evaluation(self.projection.evaluations[eid])

    def _recover(self):
        # Cada ação muda o prefixo: consultar novamente antes da próxima etapa.
        # reconcile_dispatch normaliza o estado de crash; o laço normal faz I/O.
        handled = set()
        while True:
            action = next((a for a in recovery_actions(self.projection)
                           if (a.kind, a.aggregate_id) not in handled), None)
            if action is None:
                return
            handled.add((action.kind, action.aggregate_id))
            if action.kind == "release_reservation":
                self.append("budget_released", {"reservation_id": action.aggregate_id, "reason": "admission_aborted"})
            elif action.kind == "release_evaluation":
                self._release_evaluation(self.projection.evaluations[action.aggregate_id])
            elif action.kind == "settle_attempt":
                self._settle_attempt(action.aggregate_id)
            elif action.kind == "finish_preparation":
                status = self.projection.manifest["manifest_status"]
                self._transition("run", self.journal.run_id, "failed" if status == "planning_failed" else "cancelled", status)
            elif action.kind == "preserve_uncertain_evaluation":
                requested = self.projection.history("evaluation", action.aggregate_id, "evaluation_requested")[0]
                deadline = min(parse_utc(requested["recorded_at"]) + timedelta(milliseconds=self.projection.limit("reconciliation_timeout_ms")),
                               parse_utc(self._end()))
                self.clock.advance_to(format_utc(deadline))
            elif action.kind == "record_interrupted_evaluation":
                self._interrupted_evaluation(action.aggregate_id)
            elif action.kind == "reconcile_dispatch":
                aid, details = action.aggregate_id, action.details
                a = self.projection.attempts[aid]
                if details["task_transition"]:
                    self._transition("task", a.header["task_id"], "running", "dispatch_requested")
                if details["transition_to"]:
                    if details.get("required_error"):
                        self._error("attempt", aid, "dispatch_failed", phase="dispatch", certainty="unknown", retryability="unknown")
                    self._transition("attempt", aid, "unknown", details["reason"])
                tid = a.header["task_id"]
                if self.projection.tasks[tid].state == "running":
                    self._transition("task", tid, "reconciling", "crash")
            elif action.kind == "resume_attempt_projection":
                a = self.projection.attempts[action.aggregate_id]
                tid = a.header["task_id"]
                if self.projection.tasks[tid].state == "ready":
                    self._transition("task", tid, "running", "dispatch_requested")
                term = self.projection.history("attempt", a.id, "termination_confirmed")
                obs = self.projection.history("attempt", a.id, "reconciliation_observed")
                if a.state == "queued":
                    self._transition("attempt", a.id, "failed", "dispatch_failed")
                elif a.state == "unknown" and obs:
                    self._apply_reconciliation(a.id)
                elif term and term[-1]["payload"]["cause"] == "finished":
                    self._collect(a.id)
                else:
                    # Retomar a confirmação/conclusão sem repetir dispatch.
                    if a.state == "running" and term:
                        cause = term[-1]["payload"]["cause"]
                        if cause == "failed":
                            self._finish_failure(a.id)
                        else:
                            result = self.executor.collect(a.intent)
                            for usage in result.usage:
                                self.append("usage_attached", usage, "attempt", a.id, key=usage["usage_id"], actor="executor")
                            self._settle_attempt(a.id)
                            self._transition("attempt", a.id, {"timeout": "timed_out", "cancel": "cancelled"}[cause], cause)
            elif action.kind == "stop_run" and self.projection.manifest:
                self._transition("run", self.journal.run_id, "stopping", "recovery_stop")
            else:
                return  # admissão, avaliação e parada seguem no laço normal

    def parar(self):
        s = self.projection
        if s.state == "running":
            self._transition("run", s.id, "stopping", "run_cancelled" if s.cancel_requested else "workflow_failed")
        for aid in list(self.projection.attempts):
            a = self.projection.attempts[aid]
            tid = a.header["task_id"]
            task = self.projection.tasks[tid]
            if task.state in TASK_TERMINAL:
                continue
            if a.state == "queued" and aid in self._undispatched:
                self._transition("attempt", aid, "cancelled", "run_cancelled")
            elif a.state in {"queued", "running"}:
                if a.state == "queued":
                    if self.projection.history("attempt", aid, "dispatch_acknowledged"):
                        self._unknown(aid, "dispatch_uncertain")
                    else:
                        self._transition("attempt", aid, "unknown", "crash")
                        if task.state == "running":
                            self._transition("task", tid, "reconciling", "crash")
                else:
                    self._cancel_attempt(aid)
            a = self.projection.attempts[aid]
            if a.state == "unknown":
                if self.projection.tasks[tid].state == "running":
                    self._transition("task", tid, "reconciling", "termination_unknown")
                self._reconcile(aid)
            a = self.projection.attempts[aid]
            task = self.projection.tasks[tid]
            if task.state in TASK_TERMINAL:
                continue
            if a.state in {"failed", "timed_out", "cancelled"}:
                self._task_step(tid)
            elif a.state == "completed" and task.state in {"running", "reconciling"}:
                self._transition("task", tid, "evaluating", "attempt_completed")
        for tid in list(self.projection.tasks):
            task = self.projection.tasks[tid]
            if task.state not in TASK_TERMINAL:
                if task.state in {"evaluating", "waiting_evaluation"}:
                    evs = self.projection.task_evaluations(tid)
                    if not evs:
                        self._evaluate(tid, interrupted=True)
                    elif evs[-1].report is None:
                        self._interrupted_evaluation(evs[-1].id)
                    else:
                        self._release_evaluation(evs[-1])
                self._transition("task", tid, "blocked" if task.state == "pending" and self.projection.failed_dependency(tid) else "cancelled",
                                 "run_cancelled" if self.projection.stopping_cause == "cancelled" else "workflow_failed")
        for rid, r in list(self.projection.unassigned_reservations.items()):
            if r.state == "open":
                self.append("budget_released", {"reservation_id": rid, "reason": "admission_aborted"})
        final = "indeterminate" if (any(t.state == "indeterminate" for t in self.projection.tasks.values()) or
                                   self.projection.global_evaluation_uncertain(self.clock.now())) else self.projection.stopping_cause
        self._transition("run", self.journal.run_id, final, "stopping_finished")

    def _close_run(self):
        s = self.projection
        summary = {"state": s.state, "tasks": [{"task_id": t.id, "state": t.state} for t in s.tasks.values()],
                   "output_bindings": s.output_bindings, "usage_record_ids": list(s.usages), "error_record_ids": list(s.errors),
                   "cost_summary": s.cost_summary, "limitations": [], "unknowns": []}
        if not summary["cost_summary"]["total_cost_exact"]:
            summary["unknowns"] = ["Custo total não exato; consultar usos e reservas."]
        self.append("run_closed", {"as_of_global_sequence": s.as_of_global_sequence,
                    "event_hash": self.journal.final_digest, "run_summary": summary, "summary_hash": canonical_digest(summary)})

    def run(self) -> RunProjection:
        if self.resumed:
            self._recover()
            self.resumed = False
        if self.projection.manifest is None:
            self._prepare()
        while self.projection.state not in RUN_TERMINAL:
            s = self.projection
            if s.state == "stopping":
                self.parar()
                continue
            if s.cancel_requested or s.run_deadlines or any(t.state in {"failed", "indeterminate", "blocked"} for t in s.tasks.values()):
                self.parar()
                continue
            if not s.time_remaining(self.clock.now()):
                self.append("deadline_exceeded", {"deadline_at": self._end(), "observed_at": self.clock.now()}, actor="clock",
                            key=s.id + "/deadline")
                continue
            active = next((t for t in s.tasks.values() if t.state in {"running", "evaluating", "waiting_evaluation", "reconciling"}), None)
            if active:
                self._task_step(active.id)
                continue
            for t in list(s.tasks.values()):
                if t.state == "pending" and all(self.projection.tasks[d].accepted_event_id for d in t.spec["dependencies"]):
                    self._transition("task", t.id, "ready", "dependencies_accepted")
            candidate = next((t for t in self.projection.tasks.values() if t.state in {"ready", "retry_wait"}), None)
            if candidate:
                if candidate.state == "retry_wait":
                    self._transition("task", candidate.id, "ready", "retry_admitted")
                self._admit(candidate.id)
                continue
            if all(t.state == "succeeded" for t in self.projection.tasks.values()):
                if self.projection.global_report is None:
                    self._evaluate()
                if self.projection.state == "stopping" or self.projection.cancel_requested:
                    continue
                report = self.projection.global_report
                if report is None:
                    raise ExecutorError("termination_unknown", "avaliação global sem término confirmado")
                if self.projection.cancel_requested:
                    continue
                self._transition("run", s.id, "succeeded" if report["verdict"] == "pass" else "failed", "global_evaluation")
                continue
            raise ExecutorError("invalid_state_transition", "DAG sem progresso")
        if self.projection.published_summary is None:
            self._close_run()
        return self.projection
