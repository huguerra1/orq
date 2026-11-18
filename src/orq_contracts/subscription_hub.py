"""Multiplexador de ExecutorAdapter, com reconciliação pela intenção já gravada."""
from __future__ import annotations

from .canonical import canonical_digest
from .executor import ExecutorDescription, ExecutorError


class SubscriptionHub:
    def __init__(self, runner, executors: dict, *, knowledge_connector=None):
        self.runner, self.executors = runner, dict(executors)
        self.knowledge_connector = knowledge_connector
        if set(executors) != {t["target_id"] for t in runner.catalog["targets"]}:
            raise ExecutorError("subscription_policy_violation", "mapa de executores diverge do catálogo")
        for target in runner.catalog["targets"]:
            adapter = executors[target["target_id"]]
            if adapter.describe().executor_ref != target["executor_ref"]:
                raise ExecutorError("executor_mismatch", "adaptador diverge do snapshot do destino")
        self._reference = {"id": "executor-subscription-hub", "version": "0.1",
                           "content_digest": canonical_digest({k: v.describe().executor_ref for k, v in executors.items()})}

    def describe(self):
        # Capacidades reais são as do destino no catálogo; o hub não as promove.
        descriptions = [adapter.describe() for adapter in self.executors.values()]
        controls = []
        for declaration in descriptions[0].controls:
            declarations = [next((c for c in d.controls if c["id"] == declaration["id"]), None) for d in descriptions]
            supported = all(c and c["support"] == "supported" for c in declarations)
            controls.append({"id": declaration["id"], "support": "supported" if supported else "unknown",
                             "evidence_refs": declaration["evidence_refs"] if supported else []})
        return ExecutorDescription(self._reference, "0.1", self._reference, "0.1", tuple(controls),
                                   ("cost", "tokens", "resolved_model_id", "provider_context_coverage"))

    def _adapter(self, intent):
        return self.executors[intent["target_ref"]["id"]]

    def bind_knowledge_session(self, intent, session):
        adapter = self._adapter(intent)
        if self.knowledge_connector:
            self.knowledge_connector(adapter, intent, session)
        elif callable(getattr(adapter, "bind_knowledge_session", None)):
            adapter.bind_knowledge_session(intent, session)
        else:
            raise ExecutorError("mcp_binding_unverified", "destino sem conexão MCP verificada",
                                retryability="non_retryable", termination_certainty="confirmed")

    def dispatch(self, intent, bundle, workspace):
        return self._adapter(intent).dispatch(intent, bundle, workspace)

    def observe(self, intent, until):
        return self._adapter(intent).observe(intent, until)

    def cancel(self, intent, reason):
        return self._adapter(intent).cancel(intent, reason)

    def collect(self, intent):
        return self._adapter(intent).collect(intent)

    def quota_consumption(self, intent):
        return self._adapter(intent).quota_consumption(intent)

    def reconcile(self, key, external_call_id):
        intent = next((a.intent for a in self.runner.projection.attempts.values()
                       if a.intent["dispatch_idempotency_key"] == key), None)
        if intent is None:
            raise ExecutorError("dispatch_not_known", "chave sem intenção no journal")
        return self._adapter(intent).reconcile(key, external_call_id)

    def dispatch_judge(self, intent, prompt_bytes, eval_workspace):
        return self._adapter(intent).dispatch_judge(intent, prompt_bytes, eval_workspace)

    def observe_judge(self, intent, until):
        return self._adapter(intent).observe_judge(intent, until)

    def cancel_judge(self, intent, reason):
        return self._adapter(intent).cancel_judge(intent, reason)

    def collect_judge(self, intent, **kwargs):
        return self._adapter(intent).collect_judge(intent, **kwargs)

    def reconcile_judge(self, key, external_call_id):
        for ev in self.runner.projection.evaluations.values():
            for jc in getattr(ev, "judge_calls", {}).values():
                if jc.intent.get("dispatch_idempotency_key") == key:
                    return self._adapter(jc.intent).reconcile_judge(key, external_call_id)
        for event in self.runner.journal.events:
            if event["event_type"] == "evaluator_dispatch_requested" and event["payload"]["dispatch_idempotency_key"] == key:
                target_id = event["payload"]["evaluator_target_ref"]["id"]
                return self.executors[target_id].reconcile_judge(key, external_call_id)
        raise ExecutorError("dispatch_not_known", "chave de juiz sem intenção no journal")
