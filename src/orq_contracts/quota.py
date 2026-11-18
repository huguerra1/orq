"""Snapshot de cota derivado do journal; expiração só vale após evento de reset."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime

from .canonical import canonical_digest


@dataclass(frozen=True)
class QuotaStateSnapshot:
    pools: dict = field(default_factory=dict)
    unavailable_targets: dict = field(default_factory=dict)

    @classmethod
    def from_events(cls, events):
        pools, unavailable = {}, {}
        for event in events:
            payload = event["payload"]
            if event["event_type"] in {"quota_exhausted_recorded", "quota_reset_observed"}:
                pools[payload["quota_pool_id"]] = deepcopy(payload)
            elif event["event_type"] == "error_attached" and payload["code"] == "execution_failed" and payload["message"] in {
                    "network_unavailable", "authentication_unavailable"}:
                aid = payload["scope_ref"]
                dispatch = next((e["payload"]["dispatch_intent"] for e in events
                                 if e["event_type"] == "dispatch_requested"
                                 and e["aggregate_id"] == aid), None)
                if dispatch:
                    unavailable[dispatch["target_ref"]["id"]] = deepcopy(payload)
        return cls(pools, unavailable)

    def blocking_record(self, target):
        """Registro de cota que bloqueia o destino (quota_exhausted/cooldown) ou None."""
        record = self.pools.get(target.get("quota_pool_id"))
        return record if record and record["status"] in {"quota_exhausted", "cooldown"} else None

    def next_blocked_until(self, targets):
        """Menor blocked_until entre os pools bloqueados dos destinos; None se nenhum."""
        values = [r["blocked_until"] for r in map(self.blocking_record, targets) if r and r["blocked_until"]]
        return min(values, key=lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")), default=None)

    def reasons(self, target):
        record = self.blocking_record(target)
        if record:
            return [{"code": "quota_exhausted", "subject": record["quota_pool_id"],
                     "evidence_refs": [{"id": record["quota_event_id"], "version": record["schema_version"],
                                        "content_digest": record["record_hash"]}]}]
        error = self.unavailable_targets.get(target["target_id"])
        if error:
            return [{"code": error["message"], "subject": target["target_id"], "evidence_refs": [
                {"id": error["error_id"], "version": error["schema_version"],
                 "content_digest": error.get("record_hash", canonical_digest(error))}]}]
        return []


def subscription_category(task):
    """Tipos explícitos primeiro; papéis resolvem os tipos genéricos do baseline."""
    kind = task["task_type"]
    if kind in {"architecture", "specification", "implementation", "testing", "review", "documentation"}:
        return kind
    role = task["agent_role"]["id"]
    if role == "software_architect":
        return "architecture"
    if role in {"code_reviewer", "security_reviewer"}:
        return "review"
    if role == "tester" or kind == "test":
        return "testing"
    if kind == "generation":
        return "implementation"
    return None
