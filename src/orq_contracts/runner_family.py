"""Integração opt-in da Fase 7; o caminho sequencial das Fases 1–6 fica intacto."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import json

from ._storage import confined_path
from .canonical import canonical_digest, canonicalize
from .executor import ExecutorError, seal_record
from .family_mcp import AttemptKnowledgeSession
from .knowledge_backend import EligibilityPolicy, create_knowledge_backend
from .memory_store.config import FAMILY_PARTITIONED_REGIME, MEMORY_REGIMES, MemoryScope, resolve_memory_config
from .projections import record_ref
from .quota import QuotaStateSnapshot, subscription_category
from .simulated_executor import format_utc, parse_utc
from .validator import validate_instance


@dataclass
class FamilyRuntime:
    """Portas do host: executor, backend escopado e sondagem leve não interativa.

    executor_factory(runner) retorna ExecutorAdapter (pode ser um hub). Seu método
    bind_knowledge_session(intent, session) conecta o servidor à execução do agente.
    Nenhuma escolha de unidades é feita aqui. probe(target, request) só roda após
    quota_probe_requested durável; resultado inconclusivo nunca reativa o pool.
    """
    profiles: dict
    executor_factory: object
    backend_factory: object = None
    probe: object = None
    memory_config: object = None
    memory_regime: str = FAMILY_PARTITIONED_REGIME

    def attach(self, runner):
        self.runner = runner
        if self.memory_regime not in MEMORY_REGIMES:
            raise ExecutorError("invalid_memory_scope", "memory_regime inválido")
        self.memory_config = self.memory_config or resolve_memory_config()
        self.sessions = {}
        for profile in self.profiles.values():
            if validate_instance(profile, "subscription_profile"):
                raise ExecutorError("subscription_policy_violation", "perfil inválido")
        for target in runner.catalog["targets"]:
            profile = self.profiles.get(target.get("subscription_id"))
            if not profile or any(target.get(key) != profile[key] for key in ("subscription_id", "family_id", "quota_pool_id")):
                raise ExecutorError("subscription_policy_violation", "destino sem assinatura correspondente")
        runner.executor = self.executor_factory(runner)
        if not callable(getattr(runner.executor, "bind_knowledge_session", None)):
            raise ExecutorError("subscription_policy_violation", "executor sem conexão MCP de tentativa")

    @property
    def snapshot(self):
        snap = {"profiles": self.profiles, "memory_backend": self.memory_config.backend,
                "executor": self.runner.executor.describe().executor_ref}
        if self.memory_regime != FAMILY_PARTITIONED_REGIME:
            snap["memory_regime"] = self.memory_regime
        return snap

    def target(self, intent):
        return next(t for t in self.runner.catalog["targets"] if t["target_id"] == intent["target_ref"]["id"])

    def _backend(self, scope, policy):
        r = self.runner
        if self.backend_factory:
            return self.backend_factory(scope, policy)
        return create_knowledge_backend(self.memory_config, catalog=r.knowledge_catalog,
                                        policy=policy, scope=scope, vault_root=r.vault_root)

    def session(self, task, number, decision, target, *, config=None):
        r = self.runner
        key = r.ids("knowledge-session", task["task_id"], number, decision["routing_decision_id"])
        if key in self.sessions:
            return self.sessions[key]
        directory = confined_path(r.journal.directory, "knowledge/" + key)
        if config is None and (directory / "config.json").exists():
            config = json.loads((directory / "config.json").read_bytes())
        if config is None:
            scope = MemoryScope(r.knowledge_catalog["catalog_id"], r.request["project_id"],
                                target["family_id"], task["agent_role"]["id"],
                                memory_regime=self.memory_regime)
            scope_dict = dict(zip(
                ("catalog_id", "project_id", "family_id", "role_id"),
                (scope.catalog_id, scope.project_id, scope.family_id, scope.role_id)))
            if scope.memory_regime != FAMILY_PARTITIONED_REGIME:
                scope_dict["memory_regime"] = scope.memory_regime
            config = {"session_id": key, "task": deepcopy(task),
                "knowledge_status_assertions": deepcopy(r.projection.manifest["knowledge_status_assertions"]), "scope": scope_dict,
                "eligibility": {"project_id": scope.project_id, "as_of": r.clock.now()},
                "requirements": deepcopy(task["knowledge_requirements"]),
                "choice": {"run_id": r.journal.run_id, "workflow_ref": r.projection.manifest["workflow_snapshot_ref"],
                    "task_id": task["task_id"], "candidate_attempt_number": number, "created_at": r.clock.now(),
                    "max_tokens": min(r.policy.get("knowledge_max_tokens", 2048),
                                      target["context_limit_tokens"] - r.policy.get("reserved_output_tokens", 256)),
                    "policy_snapshot_refs": [r.projection.manifest["execution_policy_snapshot_ref"]],
                    "selection_id": r.ids("effective-selection", key)},
                "materialize": {"routing_decision_ref": record_ref(decision, "routing_decision_id"),
                    "target_ref": {"id": target["target_id"], "version": target["version"], "content_digest": canonical_digest(target)},
                    "target_context_limit_tokens": target["context_limit_tokens"],
                    "reserved_output_tokens": r.policy.get("reserved_output_tokens", 256),
                    "context_manifest_id": r.ids("effective-context", key)}}
        backend = self._backend(MemoryScope(**config["scope"]), EligibilityPolicy(**config["eligibility"]))
        if "backend_id" in config and config["backend_id"] != backend.backend_id:
            raise ExecutorError("snapshot_mismatch", "backend de memória divergente")
        config["backend_id"] = backend.backend_id
        session = AttemptKnowledgeSession(directory, config, backend, clock=r.clock.now)
        self.sessions[key] = session
        return session

    def seal_context(self, aid, *, required=False):
        r = self.runner
        a = r.projection.attempts[aid]
        old = r.projection.history("attempt", aid, "attempt_context_sealed")
        if old:
            if required and old[0]["payload"]["manifest"] is None:
                # A selagem e o erro são eventos separados. Se o processo caiu
                # entre eles, reponha o erro exigido por AT07/AT13, sem duplicá-lo.
                boundary = max((e["global_sequence"] for e in (
                    r.projection.history("attempt", aid, "termination_confirmed") +
                    r.projection.history("attempt", aid, "reconciliation_observed"))), default=0)
                if not any(e["global_sequence"] > boundary and e["payload"]["code"] == "context_rejected" and
                           e["payload"]["termination_certainty"] == "confirmed" for e in
                           r.projection.history("attempt", aid, "error_attached")):
                    r._error("attempt", aid, "context_rejected", phase="collect")
            return old[0]["payload"]["manifest"] is not None or not required
        task = r.projection.tasks[a.header["task_id"]].spec
        decision = r.projection.records[a.intent["routing_decision_ref"]["id"]]
        try:
            session = self.session(task, a.header["attempt_number"], decision, self.target(a.intent))
            result, audit, closed = session.seal()
            payload = {"session_config": session.config, "audit": audit, "audit_hash": closed["audit_hash"],
                       "selection": result["selection"] if result else None, "manifest": result["manifest"] if result else None}
            r.append("attempt_context_sealed", payload, "attempt", aid, key=aid + "/context-sealed")
        except (ValueError, KeyError, OSError):
            r._error("attempt", aid, "integrity_mismatch", phase="collect")
            return False
        if required and result is None:
            r._error("attempt", aid, "context_rejected", phase="collect")
            return False
        return True

    def record_result(self, aid, result):
        r = self.runner
        a = r.projection.attempts[aid]
        target = self.target(a.intent)
        for error in result.errors:
            if error["code"] == "quota_limit_reached":
                state = error["quota_state"]
                if any(state[k] != target[k] for k in ("family_id", "subscription_id", "quota_pool_id")):
                    raise ExecutorError("quota_scope_mismatch", "bloqueio de outro destino")
                r.append("quota_exhausted_recorded", state, key=state["quota_event_id"])
        # O adaptador é dono da medição; o runner não inventa mensagens/tokens.
        method = getattr(r.executor, "quota_consumption", None)
        if method is None:
            raise ExecutorError("quota_consumption_unavailable", "executor de assinatura sem consumo")
        consumption = method(a.intent)
        if any(consumption[key] != target[key] for key in ("family_id", "subscription_id", "quota_pool_id")):
            raise ExecutorError("quota_scope_mismatch", "consumo de outro destino")
        r.append("quota_consumption_recorded", consumption, "attempt", aid,
                 key=consumption["quota_consumption_id"], actor="executor")

    def implementation_families(self, task):
        r = self.runner
        families = set()
        pending = list(task["dependencies"])
        seen = set()
        while pending:
            tid = pending.pop()
            if tid in seen:
                continue
            seen.add(tid)
            dependency = r.projection.tasks[tid]
            pending.extend(dependency.spec["dependencies"])
            if subscription_category(dependency.spec) == "implementation" and dependency.accepted_event_id:
                families.add(self.target(r.projection.latest_attempt(tid).intent)["family_id"])
        return frozenset(families)

    def refresh_quotas(self):
        r = self.runner
        for pool, old in list(r.projection.quota_states.items()):
            now = r.clock.now()
            if old["status"] == "available" or parse_utc(now) < parse_utc(old["blocked_until"]):
                continue
            evidence = []
            if old["status"] == "cooldown":
                probe_id = r.ids("probe", old["quota_event_id"])
                requests = [e for e in r.journal.events if e["event_type"] == "quota_probe_requested" and e["payload"]["probe_id"] == probe_id]
                results = [e for e in r.journal.events if e["event_type"] == "quota_probe_observed" and e["payload"]["probe_id"] == probe_id]
                if requests and not results:
                    continue  # efeito possivelmente ocorrido: não repetir sondagem
                if not results:
                    if self.probe is None:
                        continue
                    request = {"probe_id": probe_id, "quota_pool_id": pool,
                               "blocked_record_ref": record_ref(old, "quota_event_id"), "requested_at": now}
                    r.append("quota_probe_requested", request, key=probe_id)
                    target = next(t for t in r.catalog["targets"] if t["quota_pool_id"] == pool)
                    result = self.probe(deepcopy(target), deepcopy(request))
                    available = result.get("available")
                    if available is not None and type(available) is not bool:
                        raise ExecutorError("invalid_probe_result", "available deve ser bool ou null")
                    descriptor = r._ingest(canonicalize(result), r.ids("probe-evidence", probe_id), "quota-probe",
                        {"id": "quota-probe-result", "version": "0.1", "content_digest": canonical_digest("available: boolean|null")},
                        {"kind": "orchestrator_materialization", "operation": "quota-probe"}, "run", r.journal.run_id)
                    observed = {"probe_id": probe_id, "quota_pool_id": pool, "available": available,
                                "evidence_refs": [r._artifact_ref(descriptor)], "observed_at": r.clock.now()}
                    results = [r.append("quota_probe_observed", observed, key=probe_id + "/result")]
                observed = results[-1]["payload"]
                if observed["available"] is not True:
                    if observed["available"] is False:
                        renewed = {k: deepcopy(v) for k, v in old.items() if k != "record_hash"}
                        renewed.update(quota_event_id=r.ids("quota-renewed", probe_id), observed_at=r.clock.now(),
                            blocked_until=format_utc(parse_utc(r.clock.now()) + timedelta(milliseconds=old.get("default_cooldown_ms", 3600000))))
                        r.append("quota_exhausted_recorded", seal_record(renewed, "quota_state_record"), key=renewed["quota_event_id"])
                    continue
                evidence = observed["evidence_refs"]
            record = {"schema_version": "0.1", "quota_event_id": r.ids("quota-reset", old["quota_event_id"]),
                **{k: old[k] for k in ("run_id", "quota_pool_id", "subscription_id", "family_id")},
                "source_attempt_id": None, "status": "available", "previous_status": old["status"],
                "probe_evidence_refs": evidence, "observed_at": r.clock.now(), "reset_at": None,
                "blocked_until": None, "reset_source": "clock_expired", "raw_message_excerpt": None}
            r.append("quota_reset_observed", seal_record(record, "quota_state_record"), key=record["quota_event_id"])

    @property
    def quota_snapshot(self):
        return QuotaStateSnapshot.from_events(self.runner.journal.events)

    def close(self):
        for session in self.sessions.values():
            reader = getattr(session.backend, "reader", None)
            if reader and hasattr(reader, "close"):
                reader.close()
