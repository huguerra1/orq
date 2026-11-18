"""Harness executável do Experimento 7F: Camada A (simulada), Camada B (CLIs reais) e análise/auditoria (Q28–Q30).

Este módulo nunca modifica nem importa arquivos de `experiments/7f/tools/` para preservar
intacto o `package_digest` verificado por `experiments/7f/tools/f7_build.py --check`.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Callable, Mapping, Sequence

from mcp import Client

from .adapters.quota import QuotaLimitDetector, VERSIONS
from .canonical import canonical_digest, canonicalize
from .evaluator import ContentContractRegistry
from .executor import (
    CancelObservation,
    DispatchAck,
    DispatchIntent,
    ExecutorDescription,
    ExecutorError,
    ExecutionObservation,
    ExecutorResult,
    ProducedFile,
    ReconciliationObservation,
    WorkspaceHandle,
    bytes_digest,
    seal_record,
    stable_id,
)
from .mcp_server import create_family_knowledge_server
from .memory_store.config import (
    FAMILY_PARTITIONED_REGIME,
    MEMORY_REGIMES,
    OFFLINE_BACKEND,
    SHARED_GLOBAL_REGIME,
    MemoryConfig,
    resolve_memory_config,
)
from .projections import project_journal
from .runner import SequentialRunner, reference
from .runner_family import FamilyRuntime
from .simulated_executor import SimulationScript, format_utc
from .subscription_hub import SubscriptionHub
from .validator import validate_instance, validate_workflow


CODEX_REAL_QUOTA_FIXTURE = (
    "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/plus) or try again at 10:05 PM."
)
PROJECT_ID_7F = "orq-7f"
CHECK_KINDS = frozenset({"equals", "set_equals", "contains_all", "matches", "no_forbidden_text"})
DEFAULT_CLOCK_START = "2026-11-14T23:15:00Z"
_MISSING = object()
_EMBEDDING_HINTS = ("embedding", "vector", "pgvector", "openai_embed", "cohere_embed")


class Experiment7FError(ValueError):
    """Erro estruturado da execução, verificação ou auditoria do 7F."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class Loaded7FPackage:
    package_dir: Path
    manifest: dict[str, Any]
    catalog: dict[str, Any]
    tasks_by_id: dict[str, dict[str, Any]]
    vault_root: Path


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_package(package_dir: Path) -> dict[str, Any]:
    """Verifica digests brutos, digests JCS, snapshot do catálogo e `package_digest` do pacote 7F."""

    root = Path(package_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise Experiment7FError("manifest_missing", f"manifest.json ausente em {root}")
    manifest = _read_json(manifest_path)
    expected_pkg_digest = canonical_digest({k: v for k, v in manifest.items() if k != "package_digest"})
    if expected_pkg_digest != manifest.get("package_digest"):
        raise Experiment7FError("package_digest_mismatch", "package_digest diverge do manifesto")

    verified_files = 0
    for entry in manifest.get("files", []):
        target = root / entry["path"]
        if not target.is_file():
            raise Experiment7FError("package_file_missing", f"arquivo ausente no pacote: {entry['path']}")
        raw = target.read_bytes()
        if _sha256_bytes(raw) != entry["raw_sha256"]:
            raise Experiment7FError("package_file_digest_mismatch", f"raw_sha256 diverge em {entry['path']}")
        if "jcs_digest" in entry:
            doc = json.loads(raw.decode("utf-8"))
            if canonical_digest(doc)["value"] != entry["jcs_digest"]:
                raise Experiment7FError("package_jcs_digest_mismatch", f"jcs_digest diverge em {entry['path']}")
        verified_files += 1

    catalog_path = root / manifest["catalog"]["snapshot_path"]
    catalog = _read_json(catalog_path)
    if catalog.get("catalog_hash") != manifest["catalog"]["catalog_hash"]:
        raise Experiment7FError("catalog_hash_mismatch", "catalog_hash diverge do manifesto")
    if len(catalog.get("source_entries", [])) != manifest["catalog"]["source_count"]:
        raise Experiment7FError("catalog_count_mismatch", "contagem de fontes diverge do manifesto")

    tasks = manifest.get("tasks", [])
    if len(tasks) != manifest["design"]["tasks"]:
        raise Experiment7FError("task_count_mismatch", "quantidade de tarefas diverge do design")

    batches = manifest["batch_plan"]["batches"]
    total_runs = sum(batch["run_count"] for batch in batches)
    if total_runs != manifest["design"]["total_runs"]:
        raise Experiment7FError("batch_plan_mismatch", "total de runs nos lotes diverge do design")

    return {
        "valid": True,
        "experiment_id": manifest["experiment_id"],
        "catalog_id": manifest["catalog"]["catalog_id"],
        "catalog_hash": manifest["catalog"]["catalog_hash"],
        "package_digest": manifest["package_digest"],
        "verified_files": verified_files,
        "tasks": len(tasks),
        "batches": len(batches),
        "total_runs": total_runs,
    }


def load_7f_package(package_dir: Path) -> Loaded7FPackage:
    """Carrega e valida integralmente o pacote 7F antes de qualquer execução."""

    verify_package(package_dir)
    root = Path(package_dir)
    manifest = _read_json(root / "manifest.json")
    catalog = _read_json(root / manifest["catalog"]["snapshot_path"])
    tasks_by_id: dict[str, dict[str, Any]] = {}
    for entry in manifest["tasks"]:
        wrapper = _read_json(root / entry["path"])
        tasks_by_id[wrapper["task_id"]] = wrapper
    return Loaded7FPackage(
        package_dir=root,
        manifest=manifest,
        catalog=catalog,
        tasks_by_id=tasks_by_id,
        vault_root=root / "vault",
    )


def _resolve_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise Experiment7FError("invalid_pointer", f"JSON Pointer inválido: {pointer}")
    current = document
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _strict_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_strict_equal(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _collect_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for child in value.values() for text in _collect_strings(child)]
    if isinstance(value, list):
        return [text for child in value for text in _collect_strings(child)]
    return []


def run_7f_check(answer: Any, check: Mapping[str, Any]) -> tuple[bool, str]:
    """Avalia uma checagem determinística declarada em `acceptance.deterministic_checks`."""

    kind = check["kind"]
    if kind not in CHECK_KINDS:
        raise Experiment7FError("unknown_check_kind", f"tipo de checagem desconhecido: {kind}")
    if kind == "no_forbidden_text":
        texts = _collect_strings(answer)
        hits = sorted({needle for needle in check["values"] if any(needle in text for text in texts)})
        return (not hits, "ocorrências proibidas: " + ", ".join(hits) if hits else "ok")
    actual = _resolve_pointer(answer, check["pointer"])
    if actual is _MISSING:
        return False, "campo ausente"
    if kind == "equals":
        ok = _strict_equal(actual, check["value"])
        return ok, "ok" if ok else "valor diferente"
    if kind == "matches":
        ok = isinstance(actual, str) and re.fullmatch(check["pattern"], actual) is not None
        return ok, "ok" if ok else "texto não casa"
    if not isinstance(actual, list):
        return False, "esperava lista"
    if kind == "set_equals":
        wanted = check["values"]
        ok = len(actual) == len(set(map(repr, actual))) and sorted(map(repr, actual)) == sorted(map(repr, wanted))
        return ok, "ok" if ok else "conjunto diferente"
    ok = all(any(_strict_equal(item, wanted) for item in actual) for wanted in check["values"])
    return ok, "ok" if ok else "faltam elementos"


def evaluate_7f_checks(answer: Any, checks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Executa todas as checagens determinísticas sobre o artefato de saída JSON."""

    results: list[dict[str, Any]] = []
    for check in checks:
        passed, detail = run_7f_check(answer, check)
        results.append({"check_id": check["check_id"], "passed": passed, "detail": detail})
    return results


def all_checks_passed(answer: Any, checks: Sequence[Mapping[str, Any]]) -> bool:
    return all(item["passed"] for item in evaluate_7f_checks(answer, checks))


def _profiles_for_7f() -> dict[str, dict[str, Any]]:
    specs = (
        ("sub-claude", "family_claude", "pool-claude", "claude", 1.0),
        ("sub-codex", "family_codex", "pool-codex", "codex", 1.0),
        ("sub-gemini", "family_gemini", "pool-gemini", "agy", 1.0),
        ("sub-kiro", "family_kiro", "pool-kiro", "kiro-cli", 0.25),
    )
    profiles: dict[str, dict[str, Any]] = {}
    for sub_id, family_id, pool_id, cli_exec, _mult in specs:
        profile = {
            "schema_version": "0.1",
            "subscription_id": sub_id,
            "family_id": family_id,
            "cli_executable": cli_exec,
            "quota_pool_id": pool_id,
            "billing_mode": "personal_subscription",
            "default_cooldown_ms": 3600000,
            "local_timezone": "America/Sao_Paulo",
            "detector_version": VERSIONS[cli_exec],
        }
        profiles[sub_id] = seal_record(profile, "subscription_profile")
    return profiles


def _executor_ref(target_id: str) -> dict[str, Any]:
    return {
        "id": f"executor-{target_id}",
        "version": "0.1",
        "content_digest": canonical_digest(["executor-7f", target_id, "0.1"]),
    }


def _routing_catalog_for(
    profiles: Mapping[str, Mapping[str, Any]],
    role_refs: Sequence[Mapping[str, Any]],
    output_contract_refs: Sequence[Mapping[str, Any]] = (),
    *,
    priorities: Mapping[str, int] | None = None,
    families: Sequence[str] | None = None,
) -> dict[str, Any]:
    default_order = ("family_claude", "family_codex", "family_gemini", "family_kiro")
    enabled = set(families) if families is not None else set(default_order)
    priority_map = dict(priorities or {})
    control_ev = [
        {
            "id": "controls-7f",
            "version": "0.1",
            "content_digest": canonical_digest("controls-7f"),
        }
    ]
    targets = []
    for idx, profile in enumerate(profiles.values(), 1):
        family_id = profile["family_id"]
        if family_id not in enabled:
            continue
        short = family_id.removeprefix("family_")
        target_id = f"target-{short}"
        targets.append(
            {
                "target_id": target_id,
                "version": "1",
                "family_id": family_id,
                "subscription_id": profile["subscription_id"],
                "quota_pool_id": profile["quota_pool_id"],
                "model_ref": {
                    "id": f"model-{short}-7f",
                    "version": "1",
                    "content_digest": canonical_digest(["model-7f", family_id, "1"]),
                },
                "provider_ref": {
                    "id": f"provider-{short}",
                    "version": "1",
                    "content_digest": canonical_digest(["provider-7f", family_id, "1"]),
                },
                "runtime_ref": {
                    "id": f"runtime-{profile['cli_executable']}",
                    "version": "1",
                    "content_digest": canonical_digest(["runtime-7f", profile["cli_executable"], "1"]),
                },
                "executor_ref": _executor_ref(target_id),
                "execution_config_ref": {
                    "id": f"config-{short}-7f",
                    "version": "1",
                    "content_digest": canonical_digest(["config-7f", short, "1"]),
                },
                "priority": priority_map.get(target_id, idx),
                "status": "active",
                "supported_task_types": [
                    "structured_answer",
                    "architecture",
                    "specification",
                    "implementation",
                    "testing",
                    "review",
                    "documentation",
                    "analysis",
                    "generation",
                ],
                "supported_role_refs": list(role_refs),
                "capabilities": [],
                "permission_ids": [],
                "controls": [
                    {"id": "non_interactive", "support": "unknown", "evidence_refs": []},
                    {"id": "sandbox", "support": "unknown", "evidence_refs": []},
                    {"id": "timeout", "support": "supported", "evidence_refs": control_ev},
                    {"id": "cancellation", "support": "supported", "evidence_refs": control_ev},
                    {"id": "structured_output", "support": "unknown", "evidence_refs": []},
                    {"id": "usage_reporting", "support": "unknown", "evidence_refs": []},
                    {"id": "model_identification", "support": "unknown", "evidence_refs": []},
                    {"id": "call_identification", "support": "unknown", "evidence_refs": []},
                    {"id": "reconcile_not_found_authoritative", "support": "unknown", "evidence_refs": []},
                ],
                "output_contract_refs": list(output_contract_refs),
                "accepted_classifications": ["internal"],
                "context_limit_tokens": 16384,
                "max_output_tokens": 2048,
                "routing_description": f"Destino de assinatura {family_id} para experimento 7F",
                "specialization_tags": [family_id, "subscription", "exp7f"],
            }
        )
    catalog = {
        "schema_version": "0.1",
        "catalog_id": "catalog-7f-subscriptions",
        "version": "1",
        "created_at": DEFAULT_CLOCK_START,
        "targets": targets,
    }
    catalog["catalog_hash"] = canonical_digest({k: v for k, v in catalog.items() if k != "catalog_hash"})
    return catalog


def _routing_policy_fixed(target_id: str) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "routing_policy_id": f"policy-fixed-{target_id}",
        "version": "1",
        "mode": "fixed",
        "fixed_target_id": target_id,
        "ranking_rule": "priority_then_id",
        "decision_engine_ref": None,
        "input_template_ref": {
            "id": "routing-input-7f",
            "version": "1",
            "content_digest": canonical_digest("routing-input-7f"),
        },
        "confidence_threshold": None,
        "low_confidence_action": "no_dispatch",
        "invalid_response_action": "no_dispatch",
        "provider_error_action": "no_dispatch",
        "fallback_target_id": None,
    }


def _routing_policy_quota_rules() -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "routing_policy_id": "policy-quota-fallback-7f",
        "version": "1",
        "mode": "rules",
        "fixed_target_id": None,
        "ranking_rule": "priority_then_id",
        "decision_engine_ref": None,
        "input_template_ref": {
            "id": "routing-input-7f",
            "version": "1",
            "content_digest": canonical_digest("routing-input-7f"),
        },
        "confidence_threshold": None,
        "low_confidence_action": "rules_fallback",
        "invalid_response_action": "rules_fallback",
        "provider_error_action": "rules_fallback",
        "fallback_target_id": None,
    }


def _single_task_workflow(wrapper: Mapping[str, Any]) -> dict[str, Any]:
    task = deepcopy(wrapper["task"])
    return {
        "schema_version": "0.1",
        "workflow_id": f"workflow-{task['task_id']}",
        "version": "1",
        "project_id": PROJECT_ID_7F,
        "objective": task["objective"],
        "requirements": [{"requirement_id": "WREQ1", "description": "Responder segundo contrato", "required": True}],
        "external_inputs": [],
        "tasks": [task],
        "requirement_coverage": [
            {"workflow_requirement_id": "WREQ1", "task_id": task["task_id"], "task_requirement_id": "REQ1"}
        ],
        "outputs": [
            {
                "output_id": "answer",
                "artifact_type": "structured_answer",
                "source_task_id": task["task_id"],
                "source_output_id": "answer",
            }
        ],
        "evaluation_criteria": [
            {
                "criterion_id": f"WCRIT{index}",
                "method": method,
                "pass_rule": {"output_ids": ["answer"]},
                "required": True,
                "verifier_ref": None,
                "workflow_requirement_ids": ["WREQ1"],
                "output_ids": ["answer"],
            }
            for index, method in enumerate(
                ("output_binding_present", "digest_matches", "content_contract_valid"), start=1
            )
        ],
        "execution_policy_ref": {
            "id": "policy-7f",
            "version": "1",
            "content_digest": canonical_digest({"policy": "7f"}),
        },
        "constraints": {
            "max_concurrency": 1,
            "max_run_duration_ms": 600000,
            "max_attempts": 1,
            "attempt_timeout_ms": 300000,
            "max_evaluations_per_attempt": 1,
            "evaluation_timeout_ms": 20000,
            "reconciliation_timeout_ms": 15000,
        },
    }


def _runner_policy(
    *,
    routing_policy: dict[str, Any],
    experiment_metadata: dict[str, Any],
    max_attempts: int = 1,
) -> dict[str, Any]:
    return {
        "routing_policy": routing_policy,
        "knowledge_max_tokens": 4000,
        "reserved_output_tokens": 256,
        "experiment_metadata": experiment_metadata,
        "resolved_limits": {
            "max_concurrency": 1,
            "max_attempts": max_attempts,
            "attempt_timeout_ms": 300000,
            "max_run_duration_ms": 600000,
            "max_evaluations_per_attempt": 1,
            "evaluation_timeout_ms": 20000,
            "reconciliation_timeout_ms": 15000,
            "retry_policy": {
                "on_evaluation_fail": False,
                "on_retryable_error": max_attempts > 1,
                "on_confirmed_timeout": False,
            },
            "sources": [
                {
                    "field": "max_attempts",
                    "origin_ref": {
                        "id": "policy-7f",
                        "version": "1",
                        "content_digest": canonical_digest({"policy": "7f"}),
                    },
                }
            ],
        },
        "budget_snapshot": {
            "currency": "USD",
            "max_cost": None,
            "cap_mandatory": False,
            "unknown_cost_action": "admit_with_estimate",
            "cost_estimates": [],
        },
    }


def _empty_script(clock_start: str = DEFAULT_CLOCK_START) -> SimulationScript:
    doc = {
        "schema_version": "0.1",
        "script_id": "sim-script-7f-empty",
        "version": "1",
        "clock": {"start": clock_start, "step_ms": 100},
        "reconcile_not_found_authoritative": True,
        "default": {
            "dispatch": "ack",
            "execution": "finish",
            "duration_ms": 100,
            "on_cancel": "confirmed",
            "reconcile": "found_finished",
            "resolved_model_id": "sim-7f-1",
            "usage": {"cost": {"status": "unavailable", "amount": None, "currency": "USD"}},
            "outputs": "from_output_spec",
            "verifier": {},
        },
        "attempts": {},
        "workflow_verifier": {},
        "hooks": [],
    }
    return SimulationScript(doc, expected_digest=canonical_digest(doc))


def _query_from_requirement(requirement: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    topics = requirement.get("applicability_filters", {}).get("topics", [])
    raw_words = re.findall(r"[A-Za-zÀ-ÿ0-9_.-]{3,}", f"{requirement['subject']} {' '.join(topics)} {task['objective']}")
    stop = {"para", "como", "deve", "com", "que", "uma", "dos", "das", "pela", "pelo", "voce", "atua"}
    dedup: list[str] = []
    seen: set[str] = set()
    for word in raw_words:
        low = word.casefold()
        if low in stop or low in seen:
            continue
        seen.add(low)
        dedup.append(word)
        if len(dedup) >= 12:
            break
    query = " ".join(dedup) or " ".join(topics) or requirement["subject"]
    return query[:512]


class _SevenFAgentAdapter:
    """Executor determinístico para a Camada A: consulta o MCP real da tentativa e gera JSON do contrato."""

    def __init__(
        self,
        runner: SequentialRunner,
        target: Mapping[str, Any],
        wrappers_by_task: Mapping[str, Mapping[str, Any]],
        *,
        selection_mode: str = "agent_mcp_choice",
        quota_fail_on: set[tuple[str, int, str]] | None = None,
        quota_message: str = CODEX_REAL_QUOTA_FIXTURE,
    ) -> None:
        self.runner = runner
        self.target = dict(target)
        self.wrappers = wrappers_by_task
        self.selection_mode = selection_mode
        self.quota_fail_on = quota_fail_on or set()
        self.quota_message = quota_message
        self.sessions: dict[str, Any] = {}
        self.records: dict[str, dict[str, Any]] = {}

    def describe(self) -> ExecutorDescription:
        return ExecutorDescription(
            executor_ref=self.target["executor_ref"],
            adapter_version="0.1",
            runtime_ref=self.target["runtime_ref"],
            runtime_version="7f-sim-1",
            controls=tuple(
                {"id": c["id"], "support": c["support"], "evidence_refs": list(c["evidence_refs"])}
                for c in self.target["controls"]
            ),
            expected_telemetry=("cost", "tokens", "resolved_model_id", "provider_context_coverage"),
        )

    def bind_knowledge_session(self, intent: DispatchIntent, session: Any) -> None:
        self.sessions[intent["attempt_id"]] = session

    def dispatch(self, intent: DispatchIntent, bundle: bytes, workspace: WorkspaceHandle) -> DispatchAck:
        del bundle, workspace
        aid = intent["attempt_id"]
        task_id = intent["task_id"]
        number = intent["attempt_number"]
        key = (task_id, number, self.target["family_id"])
        session = self.sessions[aid]
        wrapper = self.wrappers[task_id]
        task = wrapper["task"]

        async def _interact() -> dict[str, Any]:
            async with Client(create_family_knowledge_server(session)) as client:
                selections: dict[str, list[dict[str, str]]] = {}
                max_budget = session.config["choice"]["max_tokens"]
                used_tokens = 0
                for kr in task["knowledge_requirements"]:
                    rid = kr["knowledge_requirement_id"]
                    res = await client.call_tool(
                        "search_family_knowledge",
                        {"requirement_id": rid, "query": _query_from_requirement(kr, task), "limit": 20},
                    )
                    payload = json.loads(res.content[0].text)
                    candidates = payload.get("candidates", [])
                    chosen: list[dict[str, str]] = []
                    kr_limit = min(kr.get("max_context_tokens") or max_budget, max_budget - used_tokens)
                    max_items = 2 if self.selection_mode == "static_top_k" else 1
                    kr_tokens = 0
                    for cand in candidates:
                        if len(chosen) >= max_items and chosen:
                            break
                        if kr_tokens + cand["estimated_tokens"] > kr_limit and chosen:
                            continue
                        chosen.append({k: cand[k] for k in ("source_id", "revision", "unit_id")})
                        kr_tokens += cand["estimated_tokens"]
                        used_tokens += cand["estimated_tokens"]
                    if not chosen and candidates:
                        c0 = candidates[0]
                        chosen.append({k: c0[k] for k in ("source_id", "revision", "unit_id")})
                    selections[rid] = chosen
                selected_res = await client.call_tool(
                    "select_family_knowledge",
                    {"selections": selections, "rationale": f"7f {self.selection_mode}"},
                )
                return json.loads(selected_res.content[0].text)

        mcp_out = asyncio.run(_interact())
        selected_ids = {
            unit["unit_ref"]["source_id"]
            for unit in (mcp_out.get("selection") or {}).get("selected_units", [])
        }
        distractor_ids = {d["source_id"] for d in wrapper["knowledge"]["distractors"]}
        contaminated = bool(selected_ids & distractor_ids)
        fail_quota = key in self.quota_fail_on
        answer = wrapper["reference_answers"]["contaminated" if contaminated else "pass"]
        self.records[aid] = {
            "observed_at": self.runner.clock.now(),
            "fail_quota": fail_quota,
            "answer": answer,
            "contaminated": contaminated,
        }
        return DispatchAck(acknowledged=True, external_call_id=f"call-{aid}", observed_at=self.runner.clock.now())

    def observe(self, intent: DispatchIntent, until: str) -> ExecutionObservation:
        del until
        rec = self.records[intent["attempt_id"]]
        status = "failed" if rec["fail_quota"] else "finished"
        return ExecutionObservation(status=status, termination_certainty="confirmed", observed_at=self.runner.clock.now())

    def cancel(self, intent: DispatchIntent, reason: str) -> CancelObservation:
        del intent, reason
        return CancelObservation(termination_certainty="confirmed", observed_at=self.runner.clock.now())

    def reconcile(self, dispatch_idempotency_key: str, external_call_id: str | None) -> ReconciliationObservation:
        del dispatch_idempotency_key
        return ReconciliationObservation(
            outcome="found_finished",
            external_call_id=external_call_id,
            termination_certainty="confirmed",
            observed_at=self.runner.clock.now(),
            authoritative=True,
        )

    def _build_usage(self, intent: DispatchIntent, observed_at: str) -> dict[str, Any]:
        aid = intent["attempt_id"]
        return seal_record(
            {
                "schema_version": "0.1",
                "usage_id": stable_id("usage", aid),
                "run_id": intent["run_id"],
                "scope_type": "attempt",
                "scope_ref": aid,
                "subscription_id": self.target["subscription_id"],
                "provider_ref": self.target["provider_ref"],
                "observed_at": observed_at,
                "recorded_at": observed_at,
                "measurements": [],
                "cost_lines": [
                    {
                        "status": "unavailable",
                        "amount": None,
                        "currency": None,
                        "pricing_ref": None,
                        "method": "personal_subscription",
                    }
                ],
                "coverage": {"reported": [], "estimated": [], "unavailable": ["cost"]},
                "supersedes_usage_id": None,
            },
            "usage_record",
        )

    def collect(self, intent: DispatchIntent) -> ExecutorResult:
        aid = intent["attempt_id"]
        rec = self.records[aid]
        observed_at = rec["observed_at"]
        usage = self._build_usage(intent, observed_at)
        wrapper = self.wrappers[intent["task_id"]]
        profile = self.runner.family_runtime.profiles[self.target["subscription_id"]]

        if rec["fail_quota"]:
            detector = QuotaLimitDetector(
                profile["cli_executable"],
                local_timezone=profile["local_timezone"],
                default_cooldown_ms=profile["default_cooldown_ms"],
            )
            detection = detector.detect(
                returncode=1,
                final_response=False,
                diagnostics=(self.quota_message,),
                observed_at=observed_at,
            )
            quota_state = detector.state_record(
                detection,
                profile=profile,
                intent=intent,
                observed_at=observed_at,
            )
            error = seal_record(
                {
                    "schema_version": "0.1",
                    "error_id": stable_id("err-quota", aid),
                    "run_id": intent["run_id"],
                    "scope_type": "attempt",
                    "scope_ref": aid,
                    "phase": "collect",
                    "category": "provider",
                    "code": "quota_limit_reached",
                    "message": "quota_limit_reached",
                    "retryability": "retryable",
                    "termination_certainty": "confirmed",
                    "occurred_at": observed_at,
                    "recorded_at": observed_at,
                    "external_call_ref": None,
                    "cause_error_refs": [],
                    "diagnostic_artifact_refs": [],
                    "quota_state": quota_state,
                },
                "error_record",
            )
            return ExecutorResult(
                produced_files=(),
                usage=(usage,),
                errors=(error,),
                requested_model_id=self.target["model_ref"]["id"],
                resolved_model_id=self.target["model_ref"]["id"],
                provider_context_coverage="platform_only",
            )

        out_spec = wrapper["task"]["expected_output"][0]
        raw = canonicalize(rec["answer"])
        file_obj = ProducedFile(
            output_id=out_spec["output_id"],
            content=raw,
            artifact_type=out_spec["artifact_type"],
            content_contract_ref=out_spec["content_contract_ref"],
            declared_digest=bytes_digest(raw),
            storage_locator=f"outputs/{aid}/answer.json",
        )
        return ExecutorResult(
            produced_files=(file_obj,),
            usage=(usage,),
            errors=(),
            requested_model_id=self.target["model_ref"]["id"],
            resolved_model_id=self.target["model_ref"]["id"],
            provider_context_coverage="platform_only",
        )

    def quota_consumption(self, intent: DispatchIntent) -> dict[str, Any]:
        aid = intent["attempt_id"]
        rec = self.records[aid]
        observed_at = rec["observed_at"]
        usage = self._build_usage(intent, observed_at)
        return seal_record(
            {
                "schema_version": "0.1",
                "quota_consumption_id": stable_id("consumption", aid),
                "run_id": intent["run_id"],
                "task_id": intent["task_id"],
                "attempt_id": aid,
                "family_id": self.target["family_id"],
                "subscription_id": self.target["subscription_id"],
                "quota_pool_id": self.target["quota_pool_id"],
                "usage_record_ref": {
                    "id": usage["usage_id"],
                    "version": "0.1",
                    "content_digest": canonical_digest(usage),
                },
                "messages_consumed": 1,
                "input_tokens": None,
                "output_tokens": None,
                "credit_multiplier": None,
                "quota_outcome": "quota_exhausted_on_call" if rec["fail_quota"] else "accepted",
                "recorded_at": observed_at,
            },
            "quota_consumption_record",
        )


def _contract_registry_for(wrappers: Sequence[Mapping[str, Any]]) -> ContentContractRegistry:
    registry = ContentContractRegistry()
    for wrapper in wrappers:
        ref = wrapper["task"]["expected_output"][0]["content_contract_ref"]
        registry.register(ref, wrapper["output_contract"])
    return registry


def _compute_task_metrics(
    run_id: str,
    task_id: str,
    proj: Any,
    events: Sequence[Mapping[str, Any]],
    read_artifact: Callable[[str], bytes],
    knowledge_catalog: Mapping[str, Any],
    wrapper: Mapping[str, Any],
    *,
    condition: str,
    pair_id: str,
    run_seed: int,
) -> dict[str, Any]:
    sealed = [e for e in events if e["event_type"] == "attempt_context_sealed"]
    manifest = sealed[-1]["payload"]["manifest"] if sealed else None
    selected_sources = [
        item["knowledge_provenance"]["unit_ref"]["source_id"]
        for item in (manifest or {}).get("items", [])
    ]
    tokens = int((manifest or {}).get("budget", {}).get("materialized_input_tokens", 0))
    gold_ids = {item["source_id"] for item in wrapper["knowledge"]["gold"]}
    alt_ids = {item["source_id"] for item in wrapper["knowledge"]["acceptable_alternatives"]}
    distractor_ids = {item["source_id"] for item in wrapper["knowledge"]["distractors"]}
    selected_set = set(selected_sources)
    denom = len(selected_set - alt_ids)
    precision = (len(selected_set & gold_ids) / denom) if denom else 0.0
    recall = (len(selected_set & gold_ids) / len(gold_ids)) if gold_ids else 1.0
    contamination = (len(selected_set & distractor_ids) / len(selected_set)) if selected_set else 0.0

    entry_by_source = {
        e["source_metadata"]["source_id"]: e["source_metadata"]
        for e in knowledge_catalog["source_entries"]
    }
    target_family = wrapper["target"]["family_id"]
    cross_hits = sum(
        1
        for sid in selected_set
        if entry_by_source.get(sid, {}).get("audience", {}).get("area_scope") == "family"
        and entry_by_source[sid]["audience"].get("target_families")
        and target_family not in entry_by_source[sid]["audience"]["target_families"]
    )
    cross_rate = (cross_hits / len(selected_set)) if selected_set else 0.0

    eval_pass = False
    checks_pass = False
    task_state = proj.tasks.get(task_id)
    if task_state and task_state.accepted_bindings:
        binding = next((b for b in task_state.accepted_bindings if b["output_id"] == "answer"), None)
        if binding:
            descriptor = proj.artifacts[binding["artifact"]["artifact_id"]]
            answer = json.loads(read_artifact(descriptor["locator"]).decode("utf-8"))
            checks_pass = all_checks_passed(answer, wrapper["acceptance"]["deterministic_checks"])
            eval_pass = any(ev.report and ev.report["verdict"] == "pass" for ev in proj.evaluations.values())

    return {
        "run_id": run_id,
        "task_id": task_id,
        "family_id": target_family,
        "role_id": wrapper["target"]["role_id"],
        "condition": condition,
        "pair_id": pair_id,
        "run_seed": run_seed,
        "run_state": proj.state,
        "pass_at_1": 1 if (eval_pass and checks_pass and proj.state == "succeeded") else 0,
        "run_success_rate": 1 if proj.state == "succeeded" else 0,
        "materialized_input_tokens": tokens,
        "selection_precision": round(precision, 6),
        "selection_recall": round(recall, 6),
        "distractor_contamination_rate": round(contamination, 6),
        "cross_family_exposure_rate": round(cross_rate, 6),
        "selected_sources": selected_sources,
        "bundle_hash": manifest["bundle_hash"]["value"] if manifest else None,
        "backend": (sealed[-1]["payload"]["selection"] or {}).get("backend") if sealed else None,
        "unavailable_cost_integrity": not proj.cost_summary["total_cost_exact"],
    }


def _extract_run_metrics(
    runner: SequentialRunner,
    wrapper: Mapping[str, Any],
    *,
    condition: str,
    pair_id: str,
    run_seed: int,
) -> dict[str, Any]:
    return _compute_task_metrics(
        runner.journal.run_id,
        wrapper["task_id"],
        runner.projection,
        runner.journal.events,
        runner.store.read,
        runner.knowledge_catalog,
        wrapper,
        condition=condition,
        pair_id=pair_id,
        run_seed=run_seed,
    )


def run_simulated_experiment(
    store_root: Path,
    *,
    package_dir: Path,
    task_ids: Sequence[str] | None = None,
    repetitions: int = 1,
    selection_mode: str = "agent_mcp_choice",
    memory_config: MemoryConfig | None = None,
) -> dict[str, Any]:
    """Executa Camada A pareada (`shared_global` vs `family_partitioned_rls`) para Q28."""

    if selection_mode not in {"agent_mcp_choice", "static_top_k"}:
        raise Experiment7FError("invalid_selection_mode", f"modo inválido: {selection_mode}")
    if repetitions < 1:
        raise Experiment7FError("invalid_repetitions", "repetitions deve ser >= 1")

    pkg = load_7f_package(package_dir)
    mem_cfg = memory_config or MemoryConfig(OFFLINE_BACKEND)
    profiles = _profiles_for_7f()
    selected_task_ids = list(task_ids) if task_ids is not None else [t["task_id"] for t in pkg.manifest["tasks"]]
    wrappers = [pkg.tasks_by_id[tid] for tid in selected_task_ids]
    role_refs = list({canonicalize(w["task"]["agent_role"]): w["task"]["agent_role"] for w in wrappers}.values())
    contract_refs = [w["task"]["expected_output"][0]["content_contract_ref"] for w in wrappers]
    catalog = _routing_catalog_for(profiles, role_refs, contract_refs)
    wrappers_by_task = {w["task_id"]: w for w in wrappers}
    contracts = _contract_registry_for(wrappers)

    runs: list[dict[str, Any]] = []
    store_root.mkdir(parents=True, exist_ok=True)
    pair_lookup: dict[str, str] = {}
    seed_lookup: dict[str, int] = {}
    for batch in pkg.manifest["batch_plan"]["batches"]:
        for entry in batch["runs"]:
            pair_lookup[entry["pair_id"]] = entry["pair_first_condition"]
            seed_lookup[entry["run_key"]] = entry["run_seed"]

    for task_index, wrapper in enumerate(wrappers):
        tid = wrapper["task_id"]
        target_id = f"target-{wrapper['target']['family_id'].removeprefix('family_')}"
        workflow = _single_task_workflow(wrapper)
        for rep in range(1, repetitions + 1):
            pair_id = f"{tid}.r{rep}"
            first = pair_lookup.get(
                pair_id,
                SHARED_GLOBAL_REGIME if (task_index + rep) % 2 == 0 else FAMILY_PARTITIONED_REGIME,
            )
            order = (
                (SHARED_GLOBAL_REGIME, FAMILY_PARTITIONED_REGIME)
                if first == SHARED_GLOBAL_REGIME
                else (FAMILY_PARTITIONED_REGIME, SHARED_GLOBAL_REGIME)
            )
            for condition in order:
                run_key = f"{pair_id}.{condition}"
                run_seed = seed_lookup.get(run_key, int(hashlib.sha256(run_key.encode()).hexdigest()[:12], 16))
                short_cond = "sg" if condition == SHARED_GLOBAL_REGIME else "rls"
                run_id = f"run-7f-{tid}-r{rep}-{short_cond}"
                script = _empty_script()
                request = {
                    "schema_version": "0.1",
                    "planning_request_id": f"plan-req-{run_id}",
                    "run_id": run_id,
                    "project_id": PROJECT_ID_7F,
                    "objective": workflow["objective"],
                    "workflow_template_ref": reference(workflow, "workflow_id"),
                    "planner_policy_ref": {
                        "id": "planner-fixed",
                        "version": "0.1",
                        "content_digest": canonical_digest("fixed_template"),
                    },
                    "created_at": DEFAULT_CLOCK_START,
                }
                policy = _runner_policy(
                    routing_policy=_routing_policy_fixed(target_id),
                    experiment_metadata={
                        "group": condition,
                        "seed": run_seed,
                        "tags": [
                            "experiment:7f",
                            "layer:A",
                            f"memory_regime:{condition}",
                            f"selection_mode:{selection_mode}",
                            f"task_id:{tid}",
                            f"pair_id:{pair_id}",
                        ],
                    },
                )

                def _factory(runner: SequentialRunner) -> SubscriptionHub:
                    return SubscriptionHub(
                        runner,
                        {
                            t["target_id"]: _SevenFAgentAdapter(
                                runner, t, wrappers_by_task, selection_mode=selection_mode
                            )
                            for t in runner.catalog["targets"]
                        },
                    )

                rt = FamilyRuntime(
                    profiles=profiles,
                    executor_factory=_factory,
                    memory_config=mem_cfg,
                    memory_regime=condition,
                )
                with SequentialRunner.create(
                    store_root,
                    workflow=workflow,
                    request=request,
                    catalog=catalog,
                    policy=policy,
                    script=script,
                    knowledge_catalog=pkg.catalog,
                    vault_root=pkg.vault_root,
                    clock=script.make_clock(),
                    family_runtime=rt,
                    contracts=contracts,
                ) as runner:
                    runner.run()
                    runs.append(_extract_run_metrics(runner, wrapper, condition=condition, pair_id=pair_id, run_seed=run_seed))

    return {"experiment_id": "7f", "layer": "A", "selection_mode": selection_mode, "runs": runs}


def _q29_stress_workflow(pkg: Loaded7FPackage) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    t_first = pkg.tasks_by_id["exp7f-t02-architect-codex-readonly"]
    t_second = pkg.tasks_by_id["exp7f-t08-tester-kiro-quota"]
    wrappers_by_task = {t_first["task_id"]: t_first, t_second["task_id"]: t_second}
    task1 = deepcopy(t_first["task"])
    task2 = deepcopy(t_second["task"])
    task2["dependencies"] = [task1["task_id"]]
    task2["constraints"]["max_attempts"] = 2

    workflow = {
        "schema_version": "0.1",
        "workflow_id": "workflow-7f-q29-stress",
        "version": "1",
        "project_id": PROJECT_ID_7F,
        "objective": "Fluxo sequencial 7F para estresse de cota e fallback entre famílias",
        "requirements": [
            {"requirement_id": "WREQ1", "description": "Primeira tarefa", "required": True},
            {"requirement_id": "WREQ2", "description": "Segunda tarefa", "required": True},
        ],
        "external_inputs": [],
        "tasks": [task1, task2],
        "requirement_coverage": [
            {"workflow_requirement_id": "WREQ1", "task_id": task1["task_id"], "task_requirement_id": "REQ1"},
            {"workflow_requirement_id": "WREQ2", "task_id": task2["task_id"], "task_requirement_id": "REQ1"},
        ],
        "outputs": [
            {
                "output_id": "answer",
                "artifact_type": "structured_answer",
                "source_task_id": task2["task_id"],
                "source_output_id": "answer",
            }
        ],
        "evaluation_criteria": [
            {
                "criterion_id": f"WCRIT{index}",
                "method": method,
                "pass_rule": {"output_ids": ["answer"]},
                "required": True,
                "verifier_ref": None,
                "workflow_requirement_ids": ["WREQ1", "WREQ2"],
                "output_ids": ["answer"],
            }
            for index, method in enumerate(
                ("output_binding_present", "digest_matches", "content_contract_valid"), start=1
            )
        ],
        "execution_policy_ref": {
            "id": "policy-7f",
            "version": "1",
            "content_digest": canonical_digest({"policy": "7f"}),
        },
        "constraints": {
            "max_concurrency": 1,
            "max_run_duration_ms": 600000,
            "max_attempts": 2,
            "attempt_timeout_ms": 300000,
            "max_evaluations_per_attempt": 1,
            "evaluation_timeout_ms": 20000,
            "reconciliation_timeout_ms": 15000,
        },
    }
    return workflow, wrappers_by_task


def run_quota_stress_comparison(
    store_root: Path,
    *,
    package_dir: Path,
    memory_config: MemoryConfig | None = None,
) -> dict[str, Any]:
    """Executa comparação pareada de estresse de cota para Q29 (`single_family_no_fallback` vs `quota_aware_multi_family`)."""

    pkg = load_7f_package(package_dir)
    mem_cfg = memory_config or MemoryConfig(OFFLINE_BACKEND)
    profiles = _profiles_for_7f()
    workflow, wrappers_by_task = _q29_stress_workflow(pkg)
    task1, task2 = workflow["tasks"]
    contracts = _contract_registry_for(list(wrappers_by_task.values()))

    role_refs = list({canonicalize(t["agent_role"]): t["agent_role"] for t in (task1, task2)}.values())
    contract_refs = [t["expected_output"][0]["content_contract_ref"] for t in (task1, task2)]
    results: dict[str, Any] = {}
    store_root.mkdir(parents=True, exist_ok=True)

    arms = (
        ("single_family_no_fallback", ("family_codex",), {"target-codex": 1}),
        ("quota_aware_multi_family", ("family_codex", "family_claude"), {"target-codex": 1, "target-claude": 2}),
    )
    for arm_name, families, priorities in arms:
        catalog = _routing_catalog_for(profiles, role_refs, contract_refs, priorities=priorities, families=families)
        run_id = f"run-7f-q29-{arm_name}"
        script = _empty_script()
        request = {
            "schema_version": "0.1",
            "planning_request_id": f"plan-req-{run_id}",
            "run_id": run_id,
            "project_id": PROJECT_ID_7F,
            "objective": workflow["objective"],
            "workflow_template_ref": reference(workflow, "workflow_id"),
            "planner_policy_ref": {
                "id": "planner-fixed",
                "version": "0.1",
                "content_digest": canonical_digest("fixed_template"),
            },
            "created_at": DEFAULT_CLOCK_START,
        }
        policy = _runner_policy(
            routing_policy=_routing_policy_quota_rules(),
            max_attempts=2,
            experiment_metadata={
                "group": arm_name,
                "seed": 20261114,
                "tags": ["experiment:7f", "q29:quota_stress", f"arm:{arm_name}"],
            },
        )
        fail_trigger = {(task2["task_id"], 1, "family_codex")}

        def _factory(runner: SequentialRunner, trigger: set[tuple[str, int, str]] = fail_trigger) -> SubscriptionHub:
            return SubscriptionHub(
                runner,
                {
                    t["target_id"]: _SevenFAgentAdapter(
                        runner, t, wrappers_by_task, selection_mode="static_top_k", quota_fail_on=trigger
                    )
                    for t in runner.catalog["targets"]
                },
            )

        rt = FamilyRuntime(
            profiles=profiles,
            executor_factory=_factory,
            memory_config=mem_cfg,
            memory_regime=FAMILY_PARTITIONED_REGIME,
        )
        with SequentialRunner.create(
            store_root,
            workflow=workflow,
            request=request,
            catalog=catalog,
            policy=policy,
            script=script,
            knowledge_catalog=pkg.catalog,
            vault_root=pkg.vault_root,
            clock=script.make_clock(),
            family_runtime=rt,
            contracts=contracts,
        ) as runner:
            proj = runner.run()
            ev_list = runner.journal.events
            blocked_pools: set[str] = set()
            wasted = 0
            for ev in ev_list:
                if ev["event_type"] == "quota_exhausted_recorded":
                    blocked_pools.add(ev["payload"]["quota_pool_id"])
                elif ev["event_type"] == "quota_reset_observed":
                    blocked_pools.discard(ev["payload"]["quota_pool_id"])
                elif ev["event_type"] == "dispatch_requested":
                    tid = ev["payload"]["dispatch_intent"]["target_ref"]["id"]
                    tdef = next((t for t in catalog["targets"] if t["target_id"] == tid), None)
                    if tdef and tdef["quota_pool_id"] in blocked_pools:
                        wasted += 1
            sealed = [e["payload"] for e in ev_list if e["event_type"] == "attempt_context_sealed"]
            decisions = [e["payload"] for e in ev_list if e["event_type"] == "routing_decision_recorded"]
            results[arm_name] = {
                "run_id": run_id,
                "run_state": proj.state,
                "wasted_dispatches_on_exhausted_quota": wasted,
                "quota_exhausted_events": sum(1 for e in ev_list if e["event_type"] == "quota_exhausted_recorded"),
                "used_fallback": any(
                    (d.get("effective_selection") or {}).get("used_fallback") is True for d in decisions
                ),
                "fallback_sources": [
                    item["knowledge_provenance"]["unit_ref"]["source_id"]
                    for item in (sealed[-1]["manifest"]["items"] if sealed and sealed[-1]["manifest"] else [])
                ],
                "context_manifests_sealed": len(sealed),
                "bundle_hashes": [
                    entry["manifest"]["bundle_hash"]["value"] for entry in sealed if entry["manifest"]
                ],
                "unavailable_cost_integrity": not proj.cost_summary["total_cost_exact"],
            }
    return {"experiment_id": "7f", "scenario": "Q29", "arms": results}


def run_live_batch(
    store_root: Path,
    *,
    package_dir: Path,
    batch_number: int,
    profiles: Mapping[str, Mapping[str, Any]],
    adapter_factory: Callable[[SequentialRunner], Any],
    memory_config: MemoryConfig | None = None,
) -> dict[str, Any]:
    """Camada B: executa um lote `1..6` de `manifest.batch_plan` com CLIs reais fornecidas pelo chamador."""

    pkg = load_7f_package(package_dir)
    batch = next((b for b in pkg.manifest["batch_plan"]["batches"] if b["batch"] == batch_number), None)
    if batch is None:
        raise Experiment7FError("unknown_batch", f"lote {batch_number} inexistente no manifesto 7F")
    mem_cfg = memory_config or resolve_memory_config()
    wrappers = [pkg.tasks_by_id[w["task_id"]] for w in pkg.manifest["tasks"]]
    role_refs = list({canonicalize(w["task"]["agent_role"]): w["task"]["agent_role"] for w in wrappers}.values())
    contract_refs = [w["task"]["expected_output"][0]["content_contract_ref"] for w in wrappers]
    catalog = _routing_catalog_for(profiles, role_refs, contract_refs)
    contracts = _contract_registry_for(wrappers)
    store_root.mkdir(parents=True, exist_ok=True)

    outputs: list[dict[str, Any]] = []
    for item in batch["runs"]:
        wrapper = pkg.tasks_by_id[item["task_id"]]
        workflow = _single_task_workflow(wrapper)
        target_id = f"target-{wrapper['target']['family_id'].removeprefix('family_')}"
        run_id = f"run-7f-live-{item['run_key']}"
        script = _empty_script(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        request = {
            "schema_version": "0.1",
            "planning_request_id": f"plan-req-{run_id}",
            "run_id": run_id,
            "project_id": PROJECT_ID_7F,
            "objective": workflow["objective"],
            "workflow_template_ref": reference(workflow, "workflow_id"),
            "planner_policy_ref": {
                "id": "planner-fixed",
                "version": "0.1",
                "content_digest": canonical_digest("fixed_template"),
            },
            "created_at": script.make_clock().now(),
        }
        policy = _runner_policy(
            routing_policy=_routing_policy_fixed(target_id),
            experiment_metadata={
                "group": item["condition"],
                "seed": item["run_seed"],
                "tags": [
                    "experiment:7f",
                    "layer:B",
                    f"batch:{batch_number}",
                    f"memory_regime:{item['condition']}",
                    f"task_id:{item['task_id']}",
                    f"pair_id:{item['pair_id']}",
                ],
            },
        )
        rt = FamilyRuntime(
            profiles=dict(profiles),
            executor_factory=adapter_factory,
            memory_config=mem_cfg,
            memory_regime=item["condition"],
        )
        with SequentialRunner.create(
            store_root,
            workflow=workflow,
            request=request,
            catalog=catalog,
            policy=policy,
            script=script,
            knowledge_catalog=pkg.catalog,
            vault_root=pkg.vault_root,
            clock=script.make_clock(),
            family_runtime=rt,
            contracts=contracts,
        ) as runner:
            runner.run()
            outputs.append(
                _extract_run_metrics(
                    runner,
                    wrapper,
                    condition=item["condition"],
                    pair_id=item["pair_id"],
                    run_seed=item["run_seed"],
                )
            )
    return {"experiment_id": "7f", "layer": "B", "batch": batch_number, "runs": outputs}


def _quantile_linear(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return float(sorted_vals[low])
    frac = pos - low
    return float(sorted_vals[low]) * (1.0 - frac) + float(sorted_vals[high]) * frac


def _median_iqr(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(float(v) for v in values)
    med = statistics.median(ordered) if ordered else 0.0
    q25 = _quantile_linear(ordered, 0.25)
    q75 = _quantile_linear(ordered, 0.75)
    return {"median": round(med, 6), "q25": round(q25, 6), "q75": round(q75, 6), "iqr": round(q75 - q25, 6)}


def wilcoxon_signed_rank_exact(xs: Sequence[float], ys: Sequence[float]) -> dict[str, Any]:
    """Teste exato (quando n <= 25) de postos sinalizados de Wilcoxon bilateral sobre pares."""

    diffs = [float(x) - float(y) for x, y in zip(xs, ys, strict=True) if float(x) != float(y)]
    n = len(diffs)
    if n == 0:
        return {"n": 0, "w_plus": 0.0, "p_value": 1.0}
    abs_ranked = sorted((abs(d), idx, d) for idx, d in enumerate(diffs))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i + 1
        while j < n and abs_ranked[j][0] == abs_ranked[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[abs_ranked[k][1]] = avg_rank
        i = j
    w_plus = sum(rank for rank, d in zip(ranks, diffs, strict=True) if d > 0)
    total_rank = n * (n + 1) / 2.0
    w_stat = min(w_plus, total_rank - w_plus)
    has_ties = len({abs(d) for d in diffs}) != n
    if not has_ties and n <= 25:
        dp: dict[int, int] = {0: 1}
        for rank_int in range(1, n + 1):
            nxt: dict[int, int] = {}
            for s, cnt in dp.items():
                nxt[s] = nxt.get(s, 0) + cnt
                nxt[s + rank_int] = nxt.get(s + rank_int, 0) + cnt
            dp = nxt
        tail = sum(cnt for s, cnt in dp.items() if s <= int(round(w_stat)))
        p_val = min(1.0, 2.0 * tail / (2**n))
        return {"n": n, "w_plus": w_plus, "w_stat": w_stat, "p_value": round(p_val, 8), "method": "exact"}
    mean = total_rank / 2.0
    sd = math.sqrt(n * (n + 1) * (2 * n + 1) / 24.0)
    z = (abs(w_plus - mean) - 0.5) / sd if sd > 0 else 0.0
    p_val = min(1.0, math.erfc(max(z, 0.0) / math.sqrt(2.0)))
    return {"n": n, "w_plus": w_plus, "w_stat": w_stat, "p_value": round(p_val, 8), "method": "normal_approx"}


def mcnemar_exact(a_outcomes: Sequence[int], b_outcomes: Sequence[int]) -> dict[str, Any]:
    """Teste exato bilateral de McNemar para proporções pareadas binárias."""

    discordant_a1_b0 = sum(1 for a, b in zip(a_outcomes, b_outcomes, strict=True) if a == 1 and b == 0)
    discordant_a0_b1 = sum(1 for a, b in zip(a_outcomes, b_outcomes, strict=True) if a == 0 and b == 1)
    n_disc = discordant_a1_b0 + discordant_a0_b1
    if n_disc == 0:
        return {"b": 0, "c": 0, "odds_ratio": 1.0, "p_value": 1.0}
    k = min(discordant_a1_b0, discordant_a0_b1)
    p_val = min(1.0, 2.0 * sum(math.comb(n_disc, i) * (0.5**n_disc) for i in range(k + 1)))
    if discordant_a0_b1 == 0:
        odds_ratio: float | None = None
    else:
        odds_ratio = round(discordant_a1_b0 / discordant_a0_b1, 6)
    return {
        "b": discordant_a1_b0,
        "c": discordant_a0_b1,
        "odds_ratio": odds_ratio,
        "p_value": round(p_val, 8),
    }


def cliffs_delta(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Tamanho de efeito não paramétrico delta de Cliff em [-1, 1]."""

    if not xs or not ys:
        return 0.0
    greater = sum(1 for x in xs for y in ys if x > y)
    less = sum(1 for x in xs for y in ys if x < y)
    return round((greater - less) / (len(xs) * len(ys)), 6)


def _resolve_workflow_for_events(events: Sequence[Mapping[str, Any]], pkg: Loaded7FPackage) -> dict[str, Any] | None:
    manifest_ev = next((e for e in events if e["event_type"] == "run_manifest_closed"), None)
    if manifest_ev:
        wf_ref = manifest_ev["payload"].get("workflow_snapshot_ref") or {}
        wf_id = wf_ref.get("id", "")
        if wf_id == "workflow-7f-q29-stress":
            return _q29_stress_workflow(pkg)[0]
        task_id = wf_id.removeprefix("workflow-")
        if task_id in pkg.tasks_by_id:
            return _single_task_workflow(pkg.tasks_by_id[task_id])
    return None


def _audit_single_run(run_dir: Path, pkg: Loaded7FPackage) -> dict[str, Any]:
    journal_path = run_dir / "journal.jsonl"
    if not journal_path.is_file():
        return {"run_dir": str(run_dir), "valid": False, "violations": ["journal_missing"]}
    lines = [line for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    violations: list[str] = []
    previous = None
    for index, event in enumerate(events, 1):
        if event.get("global_sequence") != index:
            violations.append(f"sequence_gap@{index}")
        if event.get("previous_event_hash") != previous:
            violations.append(f"previous_hash_mismatch@{index}")
        expected_hash = canonical_digest({k: v for k, v in event.items() if k != "event_hash"})
        if event.get("event_hash") != expected_hash:
            violations.append(f"event_hash_mismatch@{index}")
        previous = event.get("event_hash")

    for event in events:
        etype = event["event_type"]
        payload = event["payload"]
        if etype == "usage_attached" and payload.get("subscription_id"):
            for line in payload.get("cost_lines", []):
                if line.get("status") != "unavailable" or line.get("amount") is not None:
                    violations.append("subscription_cost_not_unavailable")
        if etype == "run_manifest_closed":
            es = payload.get("evaluator_snapshot") or {}
            if "model_rubric" in es.get("supported_methods", []):
                violations.append("experiment_7f_forbids_model_rubric")
        if etype in {"attempt_context_sealed", "knowledge_selection_recorded"}:
            sel = payload.get("selection") if etype == "attempt_context_sealed" else payload
            if sel and "backend" in sel:
                backend = str(sel.get("backend", ""))
                if any(hint in backend.casefold() for hint in _EMBEDDING_HINTS):
                    violations.append("embedding_backend_forbidden")
                if backend not in {"postgres_fts_trgm", "offline_markdown_bm25"}:
                    violations.append(f"unexpected_knowledge_backend:{backend}")
        if etype == "evaluation_report_recorded":
            report = payload.get("report", payload)
            for crit in report.get("criteria_snapshot", {}).get("criteria", []):
                if crit.get("method") == "model_rubric":
                    violations.append("experiment_7f_forbids_model_rubric")

    run_id = events[0]["aggregate_id"] if events else run_dir.name
    state = "invalid"
    workflow = _resolve_workflow_for_events(events, pkg)
    try:
        proj = project_journal(events, workflow=workflow, knowledge_catalog=pkg.catalog)
        run_id = proj.id
        state = proj.state
        if proj.cost_summary.get("total_cost_exact"):
            violations.append("subscription_total_cost_marked_exact")
    except Exception as error:
        violations.append(f"projection_invalid:{getattr(error, 'code', type(error).__name__)}")

    return {
        "run_id": run_id,
        "state": state,
        "events": len(events),
        "valid": not violations,
        "violations": sorted(set(violations)),
    }


def audit_and_analyze(
    store_root: Path,
    *,
    package_dir: Path,
    runs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Executa auditoria Q30 de reprodutibilidade e calcula estatísticas pareadas sobre os runs do 7F."""

    pkg = load_7f_package(package_dir)
    runs_dir = Path(store_root) / "runs"
    if not runs_dir.is_dir():
        raise Experiment7FError("runs_directory_missing", f"diretório sem runs: {runs_dir}")

    audited = [_audit_single_run(p, pkg) for p in sorted(runs_dir.iterdir()) if p.is_dir()]
    if not audited:
        raise Experiment7FError("no_runs_found", f"nenhum run encontrado em {runs_dir}")
    all_violations = {r.get("run_id") or r.get("run_dir"): r["violations"] for r in audited if not r["valid"]}

    run_records = list(runs or [])
    if not run_records and not all_violations:
        for run_path in sorted(runs_dir.iterdir()):
            if not run_path.is_dir():
                continue
            events = [
                json.loads(line)
                for line in (run_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            workflow = _resolve_workflow_for_events(events, pkg)
            if workflow is None or len(workflow["tasks"]) != 1:
                continue
            proj = project_journal(events, workflow=workflow, knowledge_catalog=pkg.catalog)
            meta = (proj.intent or {}).get("experiment_metadata", {})
            tags = {t.split(":", 1)[0]: t.split(":", 1)[1] for t in meta.get("tags", []) if ":" in t}
            task_id = tags.get("task_id")
            if task_id and task_id in pkg.tasks_by_id:
                run_records.append(
                    _extract_run_metrics_from_projection(
                        run_path,
                        events,
                        proj,
                        pkg.catalog,
                        pkg.tasks_by_id[task_id],
                        condition=meta.get("group", "unknown"),
                        pair_id=tags.get("pair_id", run_path.name),
                        run_seed=int(meta.get("seed", 0)),
                    )
                )

    by_condition: dict[str, list[Mapping[str, Any]]] = {}
    for row in run_records:
        by_condition.setdefault(row["condition"], []).append(row)

    summary_by_condition: dict[str, Any] = {}
    for cond, items in sorted(by_condition.items()):
        summary_by_condition[cond] = {
            "count": len(items),
            "pass_at_1_rate": round(sum(r["pass_at_1"] for r in items) / len(items), 6),
            "run_success_rate": round(sum(r["run_success_rate"] for r in items) / len(items), 6),
            "tokens": _median_iqr([r["materialized_input_tokens"] for r in items]),
            "selection_precision": _median_iqr([r["selection_precision"] for r in items]),
            "selection_recall": _median_iqr([r["selection_recall"] for r in items]),
            "distractor_contamination": _median_iqr([r["distractor_contamination_rate"] for r in items]),
            "cross_family_exposure": _median_iqr([r["cross_family_exposure_rate"] for r in items]),
        }

    pairs: dict[str, dict[str, Mapping[str, Any]]] = {}
    for row in run_records:
        pairs.setdefault(row["pair_id"], {})[row["condition"]] = row
    complete_pairs = [
        (p[FAMILY_PARTITIONED_REGIME], p[SHARED_GLOBAL_REGIME])
        for p in pairs.values()
        if FAMILY_PARTITIONED_REGIME in p and SHARED_GLOBAL_REGIME in p
    ]
    paired_stats: dict[str, Any] = {"pair_count": len(complete_pairs)}
    if complete_pairs:
        rls_list = [p[0] for p in complete_pairs]
        sg_list = [p[1] for p in complete_pairs]
        paired_stats["tokens_wilcoxon"] = wilcoxon_signed_rank_exact(
            [r["materialized_input_tokens"] for r in rls_list],
            [r["materialized_input_tokens"] for r in sg_list],
        )
        paired_stats["tokens_cliffs_delta"] = cliffs_delta(
            [r["materialized_input_tokens"] for r in rls_list],
            [r["materialized_input_tokens"] for r in sg_list],
        )
        paired_stats["precision_wilcoxon"] = wilcoxon_signed_rank_exact(
            [r["selection_precision"] for r in rls_list],
            [r["selection_precision"] for r in sg_list],
        )
        paired_stats["pass_at_1_mcnemar"] = mcnemar_exact(
            [r["pass_at_1"] for r in rls_list],
            [r["pass_at_1"] for r in sg_list],
        )

    return {
        "audit": {
            "valid": not all_violations,
            "runs_audited": len(audited),
            "violations": all_violations,
            "details": audited,
        },
        "conditions": summary_by_condition,
        "paired_comparison": paired_stats,
    }


def _extract_run_metrics_from_projection(
    run_path: Path,
    events: Sequence[Mapping[str, Any]],
    proj: Any,
    knowledge_catalog: Mapping[str, Any],
    wrapper: Mapping[str, Any],
    *,
    condition: str,
    pair_id: str,
    run_seed: int,
) -> dict[str, Any]:
    return _compute_task_metrics(
        proj.id,
        wrapper["task_id"],
        proj,
        events,
        lambda locator: (run_path / locator).read_bytes(),
        knowledge_catalog,
        wrapper,
        condition=condition,
        pair_id=pair_id,
        run_seed=run_seed,
    )
