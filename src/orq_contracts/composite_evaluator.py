"""Avaliador composto em dois estágios (Estágio 1 determinístico + Estágio 2 model_rubric) — Fase 8 / 8D."""

from __future__ import annotations

import copy
from datetime import timedelta
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ._storage import confined_path
from .artifact_store import ArtifactStore
from .canonical import canonical_digest, canonicalize
from .evaluator import (
    ArtifactEvidence,
    ContentContractRegistry,
    DeterministicEvaluator,
    EvaluationError,
    EvaluationResult,
    SUPPORTED_METHODS,
)
from .executor import ExecutorError, Record, bytes_digest, error_record, seal_record
from .knowledge_backend import EligibilityPolicy, create_knowledge_backend
from .knowledge_choice import KnowledgeChoiceSession, materialize_chosen_context
from .memory_store.config import FAMILY_PARTITIONED_REGIME, MemoryScope, resolve_memory_config
from .quota import QuotaStateSnapshot
from .rubric_evaluator import JudgeEvaluationOutcome, RubricModelEvaluator
from .rubric_prep import (
    IsolatedEvalWorkspace,
    default_prompt_template_ref,
    model_vendor_for_target,
    render_evaluation_prompt,
    select_evaluator_targets,
)
from .simulated_executor import DeterministicClock, SimulationScript, format_utc, parse_utc
from .validator import validate_instance


COMPOSITE_SUPPORTED_METHODS = (*SUPPORTED_METHODS, "model_rubric")


def default_evaluator_routing_policy() -> Record:
    """Política padrão aceita na ADR 0009 (family_gemini -> family_kiro, fallbacks claude/codex)."""
    return seal_record({
        "schema_version": "0.1",
        "evaluator_routing_policy_id": "eval-routing-default-v1",
        "version": "0.1",
        "cross_family_policy": "require_different",
        "enforce_distinct_model_vendor": True,
        "primary_evaluator_families": ["family_gemini", "family_kiro"],
        "fallback_evaluator_families": ["family_claude", "family_codex"],
        "panel_size": 1,
    }, "evaluator_routing_policy")


def _rubric_map(rubric_specs: Mapping[Any, Record] | Sequence[Record] | None) -> dict[tuple[str, str], Record]:
    mapping: dict[tuple[str, str], Record] = {}
    if rubric_specs is None:
        return mapping
    items = list(rubric_specs.values()) if isinstance(rubric_specs, Mapping) else list(rubric_specs)
    for spec in items:
        issues = validate_instance(spec, "rubric_spec")
        if issues:
            raise EvaluationError("invalid_rubric_spec", f"{issues[0].path}: {issues[0].code}")
        mapping[(spec["rubric_id"], spec["version"])] = copy.deepcopy(spec)
    return mapping


def _compute_verdict(criterion_results: Sequence[Record], execution_status: str) -> str:
    if execution_status != "completed":
        return "not_run"
    required = [res["status"] for res in criterion_results if res["required"]]
    if "fail" in required:
        return "fail"
    if all(status == "pass" for status in required):
        return "pass"
    return "inconclusive"


class CompositeEvaluator:
    """Executa obrigatoriamente o portão determinístico antes de qualquer despacho model_rubric."""

    def __init__(
        self,
        store: ArtifactStore,
        script: SimulationScript,
        *,
        clock: DeterministicClock,
        contracts: ContentContractRegistry | None = None,
        routing_policy: Record | None = None,
        rubric_specs: Mapping[Any, Record] | Sequence[Record] | None = None,
        prompt_template_ref: Record | None = None,
        eval_temp_root: Path | None = None,
        profiles: Mapping[str, Record] | None = None,
        judge_adapter: Any | None = None,
    ) -> None:
        if not isinstance(clock, DeterministicClock):
            raise EvaluationError("invalid_clock", "DeterministicClock injetado é obrigatório")
        if store.journal.clock is not clock:
            raise EvaluationError("clock_mismatch", "avaliador composto e journal devem compartilhar o relógio")
        policy = copy.deepcopy(routing_policy) if routing_policy is not None else default_evaluator_routing_policy()
        issues = validate_instance(policy, "evaluator_routing_policy")
        if issues:
            raise EvaluationError("invalid_evaluator_routing_policy", f"{issues[0].path}: {issues[0].code}")
        self.store = store
        self.script = script
        self.clock = clock
        self.contracts = contracts if contracts is not None else ContentContractRegistry()
        self._deterministic = DeterministicEvaluator(store, script, clock=clock, contracts=self.contracts)
        self.routing_policy = policy
        self._rubrics = _rubric_map(rubric_specs)
        self.prompt_template_ref = copy.deepcopy(prompt_template_ref or default_prompt_template_ref())
        self.eval_temp_root = Path(eval_temp_root) if eval_temp_root is not None else None
        self.profiles = copy.deepcopy(dict(profiles)) if profiles else {}
        self.judge_adapter = judge_adapter
        self.rubric_evaluator = RubricModelEvaluator(clock=clock)

    @property
    def snapshot(self) -> Record:
        identity = {
            "kind": "composite_rubric",
            "version": "0.1",
            "supported_methods": list(COMPOSITE_SUPPORTED_METHODS),
            "cross_family_policy": self.routing_policy["cross_family_policy"],
            "routing_policy_ref": {
                "id": self.routing_policy["evaluator_routing_policy_id"],
                "version": self.routing_policy["version"],
                "content_digest": copy.deepcopy(self.routing_policy["record_hash"]),
            },
        }
        return {
            **identity,
            "evaluator_ref": {
                "id": "evaluator-composite-v1",
                "version": "0.1",
                "content_digest": canonical_digest(identity),
            },
        }

    def criteria_snapshot(self, criteria: Sequence[Record]) -> Record:
        snap: Record = {
            "criteria": copy.deepcopy(list(criteria)),
            "criteria_hash": canonical_digest(criteria),
        }
        rubric_refs: list[Record] = []
        for item in criteria:
            if item["method"] == "model_rubric":
                ref = copy.deepcopy(item["pass_rule"]["rubric_ref"])
                if ref not in rubric_refs:
                    rubric_refs.append(ref)
        if rubric_refs:
            snap["rubric_refs"] = rubric_refs
        return snap

    def resolve_rubric(self, rubric_ref: Record) -> Record:
        spec = self._rubrics.get((rubric_ref["id"], rubric_ref["version"]))
        if spec is None:
            raise EvaluationError("rubric_not_found", f"rubrica não registrada: {rubric_ref['id']}@{rubric_ref['version']}")
        if spec["record_hash"] != rubric_ref["content_digest"]:
            raise EvaluationError("rubric_digest_mismatch", f"digest da rubrica {rubric_ref['id']} diverge")
        return copy.deepcopy(spec)

    def _resolve_profile(self, target: Record, runner: Any) -> Record:
        sub_id = target.get("subscription_id")
        if sub_id and sub_id in self.profiles:
            return copy.deepcopy(self.profiles[sub_id])
        if sub_id and runner.family_runtime and sub_id in runner.family_runtime.profiles:
            return copy.deepcopy(runner.family_runtime.profiles[sub_id])
        fam = target.get("family_id")
        if isinstance(fam, str):
            cli = {
                "family_claude": "claude",
                "family_codex": "codex",
                "family_gemini": "agy",
                "family_kiro": "kiro-cli",
            }.get(fam, "codex")
            from .adapters.quota import VERSIONS
            return seal_record({
                "schema_version": "0.1",
                "subscription_id": sub_id or f"sub-{fam.removeprefix('family_')}",
                "family_id": fam,
                "cli_executable": cli,
                "quota_pool_id": target.get("quota_pool_id") or f"pool-{fam.removeprefix('family_')}",
                "billing_mode": "personal_subscription",
                "default_cooldown_ms": 3600000,
                "local_timezone": "UTC",
                "detector_version": VERSIONS[cli],
            }, "subscription_profile")
        raise EvaluationError("missing_evaluator_profile", f"destino {target.get('target_id')} sem perfil de assinatura")

    def evaluate(
        self,
        *,
        run_id: str,
        evaluation_id: str,
        evaluation_number: int,
        scope: Record,
        subject_bindings: Sequence[Record],
        criteria: Sequence[Record],
        artifacts: Mapping[str, ArtifactEvidence],
        attempt_number: int | None = None,
    ) -> EvaluationResult:
        """Execução puramente determinística (usada em workflow global ou sem model_rubric)."""
        if scope["kind"] == "workflow" and any(c["method"] == "model_rubric" for c in criteria):
            raise EvaluationError("workflow_scope_model_rubric_not_supported", "model_rubric é restrito a task_attempt")
        det_result = self._deterministic.evaluate(
            run_id=run_id,
            evaluation_id=evaluation_id,
            evaluation_number=evaluation_number,
            scope=scope,
            subject_bindings=subject_bindings,
            criteria=criteria,
            artifacts=artifacts,
            attempt_number=attempt_number,
        )
        if det_result.report is None:
            return det_result
        report = copy.deepcopy(det_result.report)
        report["evaluator_snapshot"] = self.snapshot
        report["criteria_snapshot"] = self.criteria_snapshot(criteria)
        seal_record(report, "evaluation_report")
        return EvaluationResult(report, det_result.errors, det_result.termination_certainty, det_result.observed_at)

    def _run_stage1(
        self,
        *,
        run_id: str,
        evaluation_id: str,
        evaluation_number: int,
        scope: Record,
        subject_bindings: Sequence[Record],
        criteria: Sequence[Record],
        artifacts: Mapping[str, ArtifactEvidence],
        attempt_number: int,
    ) -> tuple[str, dict[str, bytes], dict[str, str], list[Record], list[Record], bool]:
        """Relê SHA-256 de subject_bindings (Estágio 0) e executa critérios determinísticos (Estágio 1)."""
        evidences: dict[str, ArtifactEvidence] = {}
        for binding in subject_bindings:
            evidence = artifacts.get(binding["artifact"]["artifact_id"])
            if not isinstance(evidence, ArtifactEvidence) or evidence.artifact_id != binding["artifact"]["artifact_id"]:
                raise EvaluationError("artifact_descriptor_missing", "binding sem descritor ingerido")
            evidences[binding["output_id"]] = evidence

        scripted: dict[str, str] = {}
        for criterion in criteria:
            if criterion["method"] == "simulated_verdict":
                scripted[criterion["criterion_id"]] = self.script.verdict(
                    criterion["pass_rule"]["script_key"],
                    evaluation_number,
                    task_id=scope.get("task_id"),
                    attempt_number=attempt_number,
                )

        contents, broken = self._deterministic._verify_bindings(
            subject_bindings, evidences, run_id=run_id, scope=scope
        )
        started = self.clock()
        integrity_errors = [
            error_record(
                run_id=run_id,
                scope_type="evaluation",
                scope_ref=evaluation_id,
                phase="evaluation",
                category="integrity",
                code="integrity_mismatch",
                message=message,
                timestamp=started,
                retryability="non_retryable",
                termination_certainty="confirmed",
                discriminator=[output_id],
            )
            for output_id, message in broken.items()
        ]
        integrity_ids = [err["error_id"] for err in integrity_errors]
        bindings_map = {binding["output_id"]: binding for binding in subject_bindings}
        det_results: dict[str, Record] = {}
        det_gate_failed = bool(broken)

        for criterion in criteria:
            method = criterion["method"]
            if method == "model_rubric":
                continue
            status, observations, ev_refs, err_ids = self._deterministic._criterion(
                criterion, bindings_map, evidences, contents, broken, integrity_ids, scripted
            )
            det_results[criterion["criterion_id"]] = {
                "criterion_id": criterion["criterion_id"],
                "required": criterion["required"],
                "requirement_ids": list(criterion.get("requirement_ids", [])),
                "method": method,
                "status": status,
                "evidence_refs": ev_refs,
                "observations": observations,
                "error_record_ids": err_ids,
            }
            if criterion["required"] and status != "pass":
                det_gate_failed = True

        return started, contents, broken, integrity_errors, list(det_results.values()), det_gate_failed

    def _effective_policy_for_criterion(self, criterion: Record, *, panel_size_override: int | None = None) -> Record:
        rule = criterion["pass_rule"]
        policy = copy.deepcopy(self.routing_policy)
        changed = False
        if "cross_family_policy" in rule and rule["cross_family_policy"] != policy["cross_family_policy"]:
            policy["cross_family_policy"] = rule["cross_family_policy"]
            changed = True
        desired_panel = panel_size_override if panel_size_override is not None else rule.get("panel_size")
        if desired_panel is not None and desired_panel != policy["panel_size"]:
            policy["panel_size"] = desired_panel
            changed = True
        if changed:
            return seal_record(policy, "evaluator_routing_policy")
        return policy

    def _prior_eval_context(self, runner: Any, task_id: str, attempt_id: str) -> tuple[bool, set[str], set[str]]:
        """Extrai contexto das avaliações anteriores da mesma tentativa (desempate / falhas técnicas)."""
        evals = [
            ev for ev in runner.projection.evaluations.values()
            if ev.request["scope"].get("attempt_id") == attempt_id and ev.report is not None
        ]
        if not evals:
            return False, set(), set()
        last_ev = evals[-1]
        disagreement = any(
            runner.projection.errors[eid]["code"] == "evaluator_panel_disagreement"
            for eid in last_ev.error_record_ids
        )
        prior_panel_families: set[str] = set()
        if disagreement:
            for jc in last_ev.judge_calls.values():
                fam = jc.intent_payload.get("evaluator_family_id")
                if fam:
                    prior_panel_families.add(fam)

        tech_failed_families: set[str] = set()
        tech_codes = {
            "eval_workspace_mutated",
            "eval_sandbox_escape_attempt",
            "evaluator_invalid_output",
            "timeout",
            "execution_failed",
        }
        for prev in evals:
            has_tech = any(runner.projection.errors[eid]["code"] in tech_codes for eid in prev.error_record_ids)
            if has_tech:
                for jc in prev.judge_calls.values():
                    fam = jc.intent_payload.get("evaluator_family_id")
                    if fam:
                        tech_failed_families.add(fam)
        return disagreement, prior_panel_families, tech_failed_families

    def _select_for_criterion(
        self,
        runner: Any,
        *,
        task_id: str,
        attempt: Any,
        criterion: Record,
        quota_snapshot: QuotaStateSnapshot | None = None,
    ) -> tuple[Any, str | None, str]:
        snapshot = quota_snapshot or QuotaStateSnapshot.from_events(runner.journal.events)
        target_id = attempt.header["execution_target_snapshot"]["target_ref"]["id"]
        author_target = next(t for t in runner.catalog["targets"] if t["target_id"] == target_id)
        author_family_id = author_target.get("family_id")
        author_families = [author_family_id] if author_family_id else []
        author_vendor = model_vendor_for_target(author_target) or "unknown"

        disagreement, prior_panel, tech_failed = self._prior_eval_context(runner, task_id, attempt.id)
        eff_policy = self._effective_policy_for_criterion(
            criterion, panel_size_override=1 if disagreement else None
        )
        calibration_mode = bool((runner.projection.intent or {}).get("experiment_metadata", {}).get("calibration_mode"))
        excluded = prior_panel | tech_failed
        selection = select_evaluator_targets(
            runner.catalog,
            eff_policy,
            author_family_ids=author_families,
            author_model_vendor=author_vendor,
            quota_snapshot=snapshot,
            calibration_mode=calibration_mode,
            excluded_families=excluded,
        )
        # Se exclusão apenas por erro técnico anterior esgotou candidatos, tenta mantendo somente prior_panel excluído.
        if not selection.complete and tech_failed - prior_panel:
            selection = select_evaluator_targets(
                runner.catalog,
                eff_policy,
                author_family_ids=author_families,
                author_model_vendor=author_vendor,
                quota_snapshot=snapshot,
                calibration_mode=calibration_mode,
                excluded_families=prior_panel,
            )
        return selection, author_family_id, author_vendor

    def quota_wait_target(self, runner: Any, *, task_id: str) -> str | None:
        """Retorna min_blocked_until quando todos os avaliadores elegíveis estão bloqueados por cota (TK19->TK22)."""
        attempt = runner.projection.latest_attempt(task_id)
        task = runner.projection.tasks[task_id].spec
        rubric_criteria = [c for c in task["evaluation_criteria"] if c["method"] == "model_rubric"]
        if not attempt or not rubric_criteria:
            return None
        selection, _, _ = self._select_for_criterion(
            runner, task_id=task_id, attempt=attempt, criterion=rubric_criteria[0]
        )
        if selection.complete:
            return None
        return selection.min_blocked_until

    def _materialize_evaluator_context(
        self,
        runner: Any,
        *,
        task: Record,
        evaluation_id: str,
        evaluation_number: int,
        criterion: Record,
        evaluator_target: Record,
    ) -> tuple[Record | None, Record | None]:
        req_ids = criterion["pass_rule"].get("knowledge_requirement_ids", [])
        if not req_ids:
            return None, None
        evaluator_role_id = criterion["pass_rule"].get("evaluator_role_id", "code_reviewer")
        evaluator_family_id = evaluator_target["family_id"]
        now = runner.clock.now()
        run_id = runner.journal.run_id
        workflow_ref = runner.projection.manifest["workflow_snapshot_ref"]
        task_reqs = {req["knowledge_requirement_id"]: req for req in task.get("knowledge_requirements", [])}
        requirements = [
            copy.deepcopy(task_reqs[rid])
            if rid in task_reqs
            else {
                "knowledge_requirement_id": rid,
                "subject": task["objective"],
                "required": True,
                "allowed_kinds": ["operational", "project"],
                "required_authority_scopes": [],
                "source_refs": [],
                "applicability_filters": {
                    "topics": [],
                    "paths": [],
                    "domain": None,
                    "roles": [evaluator_role_id],
                    "task_types": [task["task_type"]],
                },
                "freshness": None,
                "coverage_rule": "at_least_one",
                "max_context_tokens": None,
            }
            for rid in req_ids
        ]

        memory_config = (
            runner.family_runtime.memory_config
            if runner.family_runtime is not None
            else resolve_memory_config({})
        )
        memory_regime = (
            runner.family_runtime.memory_regime
            if runner.family_runtime is not None
            else FAMILY_PARTITIONED_REGIME
        )
        scope = MemoryScope(
            runner.knowledge_catalog["catalog_id"],
            runner.projection.intent["project_id"],
            evaluator_family_id,
            evaluator_role_id,
            memory_regime=memory_regime,
        )
        elig_policy = EligibilityPolicy(
            project_id=runner.projection.intent["project_id"],
            as_of=now,
        )
        backend = create_knowledge_backend(
            memory_config,
            catalog=runner.knowledge_catalog,
            policy=elig_policy,
            scope=scope,
            vault_root=runner.vault_root,
        )
        target_ref = {
            "id": evaluator_target["target_id"],
            "version": evaluator_target.get("version", evaluator_target.get("schema_version", "0.1")),
            "content_digest": canonical_digest(evaluator_target),
        }
        max_tokens = min(
            runner.policy.get("knowledge_max_tokens", 2048),
            (evaluator_target.get("context_limit_tokens") or 8192) - runner.policy.get("reserved_output_tokens", 256),
        )
        attempt_rec = runner.projection.attempts[runner.projection.evaluations[evaluation_id].request["scope"]["attempt_id"]]
        normative_items: list[dict[str, Any]] = []
        try:
            choice = KnowledgeChoiceSession(
                backend,
                requirements,
                run_id=run_id,
                workflow_ref=copy.deepcopy(workflow_ref),
                task_id=task["task_id"],
                candidate_attempt_number=attempt_rec.header["attempt_number"],
                created_at=now,
                max_tokens=max_tokens,
                policy_snapshot_refs=[runner.projection.manifest["execution_policy_snapshot_ref"]],
                selection_id=runner.ids("eval-selection", evaluation_id, evaluation_number),
            )
            selections: dict[str, list[dict[str, str]]] = {}
            seen_keys: set[tuple[str, str, str]] = set()
            all_cands = []
            for req in requirements:
                rid = req["knowledge_requirement_id"]
                cands = choice.search(rid, req["subject"], 10)
                if not cands:
                    cands = choice.search(rid, "regras operacao global", 10)
                selections[rid] = [
                    {"source_id": cand.source_id, "revision": cand.revision, "unit_id": cand.unit_id}
                    for cand in cands
                ]
                for cand in cands:
                    if cand.key not in seen_keys:
                        seen_keys.add(cand.key)
                        all_cands.append(cand)
            contents = backend.load_unit_contents([c.key for c in all_cands]) if all_cands else {}
            for cand in all_cands:
                normative_items.append({
                    "source_id": cand.source_id,
                    "revision": cand.revision,
                    "unit_id": cand.unit_id,
                    "content": contents[cand.key],
                })
            selection_rec = choice.choose(selections)
            mat_ctx = materialize_chosen_context(
                backend,
                selection_rec,
                routing_decision_ref=attempt_rec.header["routing_decision_ref"],
                target_ref=target_ref,
                target_context_limit_tokens=evaluator_target.get("context_limit_tokens") or 8192,
                reserved_output_tokens=runner.policy.get("reserved_output_tokens", 256),
                context_manifest_id=runner.ids("eval-context", evaluation_id, evaluation_number),
            )
        finally:
            reader = getattr(backend, "reader", None)
            if reader is not None and hasattr(reader, "close"):
                reader.close()

        manifest = mat_ctx.manifest
        manifest_id = manifest["context_manifest_id"]
        desc = runner._ingest(
            canonicalize(manifest),
            runner.ids("artifact", evaluation_id, "context-manifest"),
            "context-manifest",
            {"id": "context-manifest", "version": "0.1", "content_digest": canonical_digest("context-manifest:0.1")},
            {"kind": "evaluation_output", "evaluation_id": evaluation_id, "purpose": "context-manifest"},
            "evaluation",
            evaluation_id,
        )
        manifest_ref = {"id": manifest_id, "version": "0.1", "content_digest": desc["content_digest"]}
        normative_payload = {
            "evaluator_family_id": evaluator_family_id,
            "evaluator_role_id": evaluator_role_id,
            "context_manifest_ref": manifest_ref,
            "items": normative_items,
        }
        return manifest_ref, normative_payload

    def _save_recovery_state(self, runner: Any, judge_call_id: str, data: Record) -> None:
        path = confined_path(runner._state_directory, f"judge-meta-{judge_call_id}.json")
        path.write_bytes(canonicalize(data))

    def _load_recovery_state(self, runner: Any, judge_call_id: str) -> Record:
        path = confined_path(runner._state_directory, f"judge-meta-{judge_call_id}.json")
        return json.loads(path.read_bytes())

    def _delete_recovery_state(self, runner: Any, judge_call_id: str) -> None:
        path = confined_path(runner._state_directory, f"judge-meta-{judge_call_id}.json")
        path.unlink(missing_ok=True)

    def _workspace_from_recovery(self, runner: Any, meta: Record, binding_map: Mapping[str, Record]) -> IsolatedEvalWorkspace:
        ws_path = Path(meta["eval_workspace_dir"])
        ws_digest = meta["eval_workspace_digest"]
        snapshot = meta["eval_workspace_snapshot"]
        artifact_files = meta["artifact_files"]
        return IsolatedEvalWorkspace(
            ws_path,
            eval_workspace_digest=ws_digest,
            snapshot=snapshot,
            artifact_files=artifact_files,
        )

    def _record_judge_outcome_events(
        self,
        runner: Any,
        *,
        evaluation_id: str,
        outcome: JudgeEvaluationOutcome,
    ) -> None:
        usage = outcome.usage_record
        runner.append("usage_attached", usage, "evaluation", evaluation_id, key=usage["usage_id"], actor="evaluator")
        cons = outcome.quota_consumption
        runner.append("quota_consumption_recorded", cons, "evaluation", evaluation_id,
                      key=cons["quota_consumption_id"], actor="evaluator")
        for err in outcome.errors:
            runner._attach_error(err, "evaluation", evaluation_id)
        if outcome.quota_state is not None:
            runner.append("quota_exhausted_recorded", outcome.quota_state, key=outcome.quota_state["quota_event_id"])
        if outcome.evidence is not None:
            desc = runner._ingest(
                canonicalize(outcome.evidence),
                outcome.evidence["evidence_id"],
                "rubric-evaluation-evidence",
                {"id": "rubric-evaluation-evidence", "version": "0.1", "content_digest": canonical_digest("rubric-evaluation-evidence:0.1")},
                {"kind": "evaluation_output", "evaluation_id": evaluation_id, "purpose": "rubric-evaluation-evidence"},
                "evaluation",
                evaluation_id,
            )

    def _finish_evaluation_report(
        self,
        runner: Any,
        *,
        evaluation_id: str,
        evaluation_number: int,
        scope: Record,
        subject_bindings: Sequence[Record],
        criteria: Sequence[Record],
        started_at: str,
        completed_at: str,
        execution_status: str,
        criterion_results: Sequence[Record],
        context_manifest_ref: Record | None,
        limitations: Sequence[str] = (),
        unknowns: Sequence[str] = (),
    ) -> None:
        ev = runner.projection.evaluations[evaluation_id]
        verdict = _compute_verdict(criterion_results, execution_status)
        ev_refs: list[Record] = []
        for res in criterion_results:
            for ref in res["evidence_refs"]:
                if ref not in ev_refs:
                    ev_refs.append(copy.deepcopy(ref))
        report = seal_record({
            "schema_version": "0.1",
            "evaluation_id": evaluation_id,
            "evaluation_number": evaluation_number,
            "run_id": runner.journal.run_id,
            "scope": copy.deepcopy(scope),
            "subject_bindings": copy.deepcopy(list(subject_bindings)),
            "patch_application_refs": [],
            "criteria_snapshot": self.criteria_snapshot(criteria),
            "evaluator_snapshot": self.snapshot,
            "context_manifest_ref": copy.deepcopy(context_manifest_ref),
            "started_at": started_at,
            "completed_at": completed_at,
            "execution_status": execution_status,
            "criterion_results": copy.deepcopy(list(criterion_results)),
            "verdict": verdict,
            "evidence_refs": ev_refs,
            "usage_record_ids": list(ev.usage_record_ids),
            "error_record_ids": list(ev.error_record_ids),
            "limitations": list(limitations),
            "unknowns": list(unknowns),
        }, "evaluation_report")
        runner.append("evaluation_report_recorded", report, "evaluation", evaluation_id,
                      key=evaluation_id + "/report", actor="evaluator")

    def execute_task_evaluation(
        self,
        runner: Any,
        *,
        evaluation_id: str,
        evaluation_number: int,
        scope: Record,
        task: Record,
        attempt: Any,
        subject_bindings: Sequence[Record],
        criteria: Sequence[Record],
        artifacts: Mapping[str, ArtifactEvidence],
        deadline_at: str,
    ) -> bool:
        """Executa avaliação em dois estágios; retorna False se entrar em TK21 reconciling."""
        started_at, contents, broken, integrity_errors, det_results, det_gate_failed = self._run_stage1(
            run_id=runner.journal.run_id,
            evaluation_id=evaluation_id,
            evaluation_number=evaluation_number,
            scope=scope,
            subject_bindings=subject_bindings,
            criteria=criteria,
            artifacts=artifacts,
            attempt_number=attempt.header["attempt_number"],
        )
        for err in integrity_errors:
            runner._attach_error(err, "evaluation", evaluation_id)

        det_by_id = {res["criterion_id"]: res for res in det_results}
        rubric_criteria = [c for c in criteria if c["method"] == "model_rubric"]
        if det_gate_failed or not rubric_criteria:
            reason = "skipped_due_to_integrity_mismatch" if broken else "skipped_due_to_deterministic_gate"
            all_results: list[Record] = []
            limitations: list[str] = []
            for item in criteria:
                if item["method"] != "model_rubric":
                    all_results.append(det_by_id[item["criterion_id"]])
                    if det_by_id[item["criterion_id"]]["status"] == "not_run":
                        limitations.append("Critério não executado: " + item["criterion_id"])
                else:
                    all_results.append({
                        "criterion_id": item["criterion_id"],
                        "required": item["required"],
                        "requirement_ids": list(item.get("requirement_ids", [])),
                        "method": "model_rubric",
                        "status": "not_run",
                        "evidence_refs": [],
                        "observations": [reason],
                        "error_record_ids": [],
                    })
                    limitations.append(reason + ": " + item["criterion_id"])
            self._finish_evaluation_report(
                runner,
                evaluation_id=evaluation_id,
                evaluation_number=evaluation_number,
                scope=scope,
                subject_bindings=subject_bindings,
                criteria=criteria,
                started_at=started_at,
                completed_at=self.clock(),
                execution_status="completed",
                criterion_results=all_results,
                context_manifest_ref=None,
                limitations=limitations,
            )
            return True

        adapter = self.judge_adapter if self.judge_adapter is not None else runner.executor
        subject_artifacts = [(binding, contents[binding["output_id"]]) for binding in subject_bindings]
        run_deadline_dt = parse_utc(runner.projection.intent["created_at"]) + timedelta(
            milliseconds=runner.projection.limit("max_run_duration_ms")
        )
        context_manifest_ref: Record | None = None
        eval_temp = self.eval_temp_root or (Path(runner.journal.directory).parent.parent.parent / f"_orq_eval_{runner.journal.run_id}")
        store_root = runner.journal.directory.parent.parent
        adapter_desc = adapter.describe()

        final_results: dict[str, Record] = dict(det_by_id)
        limitations_out: list[str] = []
        eval_exec_status = "completed"
        eval_timeout_ms = runner.projection.limit("evaluation_timeout_ms", task["task_id"])

        for crit_idx, criterion in enumerate(rubric_criteria, 1):
            rubric_spec = self.resolve_rubric(criterion["pass_rule"]["rubric_ref"])
            selection, author_family_id, author_vendor = self._select_for_criterion(
                runner, task_id=task["task_id"], attempt=attempt, criterion=criterion
            )
            if not selection.complete:
                err = error_record(
                    run_id=runner.journal.run_id,
                    scope_type="evaluation",
                    scope_ref=evaluation_id,
                    phase="evaluation",
                    category="policy",
                    code="no_eligible_cross_family_evaluator",
                    message="Nenhuma família avaliadora elegível disponível.",
                    timestamp=self.clock(),
                    retryability="retryable",
                    termination_certainty="confirmed",
                )
                runner._attach_error(err, "evaluation", evaluation_id)
                obs_notes = ["no_eligible_cross_family_evaluator"]
                if selection.min_blocked_until is None or parse_utc(selection.min_blocked_until) > run_deadline_dt:
                    obs_notes.append("no_eligible_evaluator_before_deadline")
                    limitations_out.append("no_eligible_evaluator_before_deadline")
                final_results[criterion["criterion_id"]] = {
                    "criterion_id": criterion["criterion_id"],
                    "required": criterion["required"],
                    "requirement_ids": list(criterion.get("requirement_ids", [])),
                    "method": "model_rubric",
                    "status": "not_run",
                    "evidence_refs": [],
                    "observations": obs_notes,
                    "error_record_ids": [err["error_id"]],
                }
                eval_exec_status = "failed"
                continue

            if criterion["pass_rule"].get("knowledge_requirement_ids"):
                context_manifest_ref, normative_payload = self._materialize_evaluator_context(
                    runner,
                    task=task,
                    evaluation_id=evaluation_id,
                    evaluation_number=evaluation_number,
                    criterion=criterion,
                    evaluator_target=selection.selected_targets[0],
                )
            else:
                normative_payload = None

            judge_outcomes: list[JudgeEvaluationOutcome] = []
            for j_offset, target in enumerate(selection.selected_targets, 1):
                global_judge_idx = j_offset if len(rubric_criteria) == 1 else (crit_idx - 1) * 2 + j_offset
                judge_call_id = f"judge-{evaluation_id}-{global_judge_idx}"
                profile = self._resolve_profile(target, runner)
                ws = IsolatedEvalWorkspace.create(
                    workspace_id=f"eval-{judge_call_id}",
                    rubric_spec=rubric_spec,
                    subject_artifacts=subject_artifacts,
                    eval_temp_root=eval_temp,
                    store_root=store_root,
                    normative_context=normative_payload,
                    wsl_compatible=bool(getattr(adapter, "_wsl", False)),
                )
                rendered = render_evaluation_prompt(
                    run_id=runner.journal.run_id,
                    evaluation_id=evaluation_id,
                    criterion_id=criterion["criterion_id"],
                    judge_call_id=judge_call_id,
                    rubric_spec=rubric_spec,
                    subject_artifacts=subject_artifacts,
                    evaluator_role_id=criterion["pass_rule"].get("evaluator_role_id", "code_reviewer"),
                    prompt_template_ref=self.prompt_template_ref,
                    normative_context=normative_payload,
                )
                target_ref = {
                    "id": target["target_id"],
                    "version": target["version"],
                    "content_digest": canonical_digest(target),
                }
                vendor = model_vendor_for_target(target) or "unknown"
                dispatch_key = f"{runner.journal.run_id}/{judge_call_id}"
                dispatch_payload = {
                    "judge_call_id": judge_call_id,
                    "judge_index": j_offset,
                    "dispatch_idempotency_key": dispatch_key,
                    "evaluator_family_id": profile["family_id"],
                    "evaluator_model_vendor": vendor,
                    "evaluator_target_ref": target_ref,
                    "subscription_id": profile["subscription_id"],
                    "quota_pool_id": profile["quota_pool_id"],
                    "runtime_ref": copy.deepcopy(target.get("runtime_ref", adapter_desc.runtime_ref)),
                    "runtime_version": str(adapter_desc.runtime_version),
                    "requested_model_id": target["model_ref"]["id"],
                    "prompt_template_ref": copy.deepcopy(rendered.prompt_template_ref),
                    "rendered_prompt_digest": copy.deepcopy(rendered.rendered_prompt_digest),
                    "eval_workspace_digest": copy.deepcopy(ws.eval_workspace_digest),
                    "deadline_at": deadline_at,
                }
                dispatch_intent = {
                    **dispatch_payload,
                    "run_id": runner.journal.run_id,
                    "task_id": task["task_id"],
                    "attempt_id": attempt.id,
                    "attempt_number": attempt.header["attempt_number"],
                    "evaluation_id": evaluation_id,
                    "evaluation_number": evaluation_number,
                    "criterion_id": criterion["criterion_id"],
                    "target_ref": target_ref,
                    "evaluation_timeout_ms": eval_timeout_ms,
                }
                self._save_recovery_state(runner, judge_call_id, {
                    "criterion": copy.deepcopy(criterion),
                    "dispatch_intent": dispatch_intent,
                    "evaluator_target": copy.deepcopy(target),
                    "profile": copy.deepcopy(profile),
                    "author_family_ids": [author_family_id] if author_family_id else [],
                    "author_model_vendor": author_vendor,
                    "eval_workspace_dir": str(ws.path),
                    "eval_workspace_digest": ws.eval_workspace_digest,
                    "eval_workspace_snapshot": ws._snapshot,
                    "artifact_files": ws.artifact_files,
                    "context_manifest_ref": context_manifest_ref,
                    "started_at": started_at,
                })
                runner.append("evaluator_dispatch_requested", dispatch_payload, "evaluation", evaluation_id,
                              key=dispatch_key, actor="orchestrator")

                try:
                    ack = adapter.dispatch_judge(dispatch_intent, rendered.prompt_text.encode("utf-8"), ws)
                except ExecutorError as err:
                    if err.termination_certainty == "confirmed":
                        ws.cleanup()
                        self._delete_recovery_state(runner, judge_call_id)
                        runner._attach_error(
                            error_record(
                                run_id=runner.journal.run_id,
                                scope_type="evaluation",
                                scope_ref=evaluation_id,
                                phase="dispatch",
                                category="transport",
                                code="dispatch_failed",
                                message=err.message,
                                timestamp=self.clock(),
                                retryability=err.retryability,
                                termination_certainty="confirmed",
                            ),
                            "evaluation",
                            evaluation_id,
                        )
                        runner.append("evaluator_termination_confirmed", {
                            "judge_call_id": judge_call_id,
                            "cause": "failed",
                            "observed_at": self.clock(),
                            "resolved_model_id": None,
                            "provider_context_coverage": "unknown",
                        }, "evaluation", evaluation_id, key=dispatch_key + "/term", actor="evaluator")
                        eval_exec_status = "failed"
                        break
                    runner._attach_error(
                        error_record(
                            run_id=runner.journal.run_id,
                            scope_type="evaluation",
                            scope_ref=evaluation_id,
                            phase="dispatch",
                            category="transport",
                            code="dispatch_failed",
                            message=err.message,
                            timestamp=self.clock(),
                            retryability="unknown",
                            termination_certainty="unknown",
                        ),
                        "evaluation",
                        evaluation_id,
                    )
                    runner._transition("task", task["task_id"], "reconciling", "evaluation_unknown")
                    return False

                for ack_err in ack.errors:
                    runner._attach_error(ack_err, "evaluation", evaluation_id)
                if ack.errors and all(e["termination_certainty"] == "confirmed" for e in ack.errors):
                    ws.cleanup()
                    self._delete_recovery_state(runner, judge_call_id)
                    runner.append("evaluator_termination_confirmed", {
                        "judge_call_id": judge_call_id,
                        "cause": "failed",
                        "observed_at": ack.observed_at,
                        "resolved_model_id": None,
                        "provider_context_coverage": "unknown",
                    }, "evaluation", evaluation_id, key=dispatch_key + "/term", actor="evaluator")
                    eval_exec_status = "failed"
                    break
                if ack.acknowledged:
                    runner.append("evaluator_dispatch_acknowledged", {
                        "judge_call_id": judge_call_id,
                        "external_call_id": ack.external_call_id,
                    }, "evaluation", evaluation_id, key=dispatch_key + "/ack", actor="evaluator")
                else:
                    # ack_lost -> entra em reconciliação TK21
                    runner._attach_error(
                        error_record(
                            run_id=runner.journal.run_id,
                            scope_type="evaluation",
                            scope_ref=evaluation_id,
                            phase="dispatch",
                            category="transport",
                            code="dispatch_failed",
                            message="dispatch_uncertain",
                            timestamp=self.clock(),
                            retryability="unknown",
                            termination_certainty="unknown",
                        ),
                        "evaluation",
                        evaluation_id,
                    )
                    runner._transition("task", task["task_id"], "reconciling", "evaluation_unknown")
                    return False

                try:
                    obs = adapter.observe_judge(dispatch_intent, deadline_at)
                except ExecutorError:
                    obs = None
                if obs is None or obs.status == "unknown":
                    runner._attach_error(
                        error_record(
                            run_id=runner.journal.run_id,
                            scope_type="evaluation",
                            scope_ref=evaluation_id,
                            phase="observe",
                            category="evaluation",
                            code="evaluator_failed",
                            message="termination_unknown",
                            timestamp=self.clock(),
                            retryability="unknown",
                            termination_certainty="unknown",
                        ),
                        "evaluation",
                        evaluation_id,
                    )
                    runner._transition("task", task["task_id"], "reconciling", "evaluation_unknown")
                    return False

                term_cause = obs.status
                if obs.status == "running":
                    cancel_obs = adapter.cancel_judge(dispatch_intent, "evaluation_timeout")
                    if cancel_obs.termination_certainty != "confirmed":
                        runner._attach_error(
                            error_record(
                                run_id=runner.journal.run_id,
                                scope_type="evaluation",
                                scope_ref=evaluation_id,
                                phase="observe",
                                category="timeout",
                                code="timeout",
                                message="timeout com término incerto do juiz",
                                timestamp=self.clock(),
                                retryability="retryable",
                                termination_certainty="unknown",
                            ),
                            "evaluation",
                            evaluation_id,
                        )
                        runner._transition("task", task["task_id"], "reconciling", "evaluation_unknown")
                        return False
                    reobs = adapter.observe_judge(dispatch_intent, self.clock.now())
                    term_cause = reobs.status if reobs.status in {"finished", "failed"} else "timeout"

                raw_outcome = adapter.collect_judge(
                    dispatch_intent,
                    rubric_spec=rubric_spec,
                    subject_artifacts=subject_artifacts,
                    eval_workspace=ws,
                )
                if term_cause == "timeout":
                    from dataclasses import replace as _dc_replace
                    raw_outcome = _dc_replace(raw_outcome, execution_status="stopped", stop_reason="timeout")

                runner.append("evaluator_termination_confirmed", {
                    "judge_call_id": judge_call_id,
                    "cause": term_cause,
                    "observed_at": raw_outcome.observed_at,
                    "resolved_model_id": raw_outcome.resolved_model_id,
                    "provider_context_coverage": raw_outcome.provider_context_coverage,
                }, "evaluation", evaluation_id, key=dispatch_key + "/term", actor="evaluator")

                try:
                    outcome = self.rubric_evaluator.collect_and_verify(
                        dispatch_intent=dispatch_intent,
                        raw_outcome=raw_outcome,
                        eval_workspace=ws,
                        rubric_spec=rubric_spec,
                        prompt_template_ref=rendered.prompt_template_ref,
                        rendered_prompt_digest=rendered.rendered_prompt_digest,
                        subject_artifacts=subject_artifacts,
                        author_family_ids=[author_family_id] if author_family_id else [],
                        author_model_vendor=author_vendor,
                        evaluator_target=target,
                        profile=profile,
                        runtime_ref=dispatch_payload["runtime_ref"],
                        runtime_version=dispatch_payload["runtime_version"],
                        store_root=store_root,
                    )
                finally:
                    ws.cleanup()
                    self._delete_recovery_state(runner, judge_call_id)

                self._record_judge_outcome_events(runner, evaluation_id=evaluation_id, outcome=outcome)
                judge_outcomes.append(outcome)

            if judge_outcomes:
                crit_res, crit_exec = self._consolidate_criterion_outcomes(
                    runner,
                    evaluation_id=evaluation_id,
                    criterion=criterion,
                    outcomes=judge_outcomes,
                    selection=selection,
                    run_deadline_dt=run_deadline_dt,
                    limitations_out=limitations_out,
                )
                final_results[criterion["criterion_id"]] = crit_res
                if crit_exec != "completed":
                    eval_exec_status = crit_exec
            elif criterion["criterion_id"] not in final_results:
                final_results[criterion["criterion_id"]] = {
                    "criterion_id": criterion["criterion_id"],
                    "required": criterion["required"],
                    "requirement_ids": list(criterion.get("requirement_ids", [])),
                    "method": "model_rubric",
                    "status": "not_run",
                    "evidence_refs": [],
                    "observations": ["judge_dispatch_aborted"],
                    "error_record_ids": [],
                }
                eval_exec_status = "failed"

        ordered_results = [final_results[c["criterion_id"]] for c in criteria]
        self._finish_evaluation_report(
            runner,
            evaluation_id=evaluation_id,
            evaluation_number=evaluation_number,
            scope=scope,
            subject_bindings=subject_bindings,
            criteria=criteria,
            started_at=started_at,
            completed_at=self.clock(),
            execution_status=eval_exec_status,
            criterion_results=ordered_results,
            context_manifest_ref=context_manifest_ref,
            limitations=limitations_out,
        )
        return True

    def _consolidate_criterion_outcomes(
        self,
        runner: Any,
        *,
        evaluation_id: str,
        criterion: Record,
        outcomes: Sequence[JudgeEvaluationOutcome],
        selection: Any,
        run_deadline_dt: Any,
        limitations_out: list[str],
    ) -> tuple[Record, str]:
        ev_refs: list[Record] = []
        error_ids: list[str] = []
        observations: list[str] = []
        for out in outcomes:
            error_ids.extend(err["error_id"] for err in out.errors)
            observations.extend(out.observations)
            if out.evidence is not None:
                desc = runner.projection.artifacts[out.evidence["evidence_id"]]
                ev_refs.append({
                    "artifact_id": desc["artifact_id"],
                    "content_digest": copy.deepcopy(desc["content_digest"]),
                })

        non_completed = next((o.execution_status for o in outcomes if o.execution_status != "completed"), None)
        if non_completed is not None:
            return {
                "criterion_id": criterion["criterion_id"],
                "required": criterion["required"],
                "requirement_ids": list(criterion.get("requirement_ids", [])),
                "method": "model_rubric",
                "status": "not_run",
                "evidence_refs": ev_refs,
                "observations": observations,
                "error_record_ids": error_ids,
            }, non_completed

        statuses = [out.criterion_status for out in outcomes]
        if len(outcomes) == 1:
            crit_status = statuses[0]
        elif all(s == "pass" for s in statuses):
            crit_status = "pass"
        elif all(s == "fail" for s in statuses):
            crit_status = "fail"
        else:
            crit_status = "inconclusive"
            disagree_err = error_record(
                run_id=runner.journal.run_id,
                scope_type="evaluation",
                scope_ref=evaluation_id,
                phase="evaluation",
                category="evaluation",
                code="evaluator_panel_disagreement",
                message=f"Painel duplo divergiu: {statuses}",
                timestamp=self.clock(),
                retryability="retryable",
                termination_certainty="confirmed",
            )
            runner._attach_error(disagree_err, "evaluation", evaluation_id)
            error_ids.append(disagree_err["error_id"])
            quota_snap = QuotaStateSnapshot.from_events(runner.journal.events)
            third_ok = any(
                (rec := quota_snap.blocking_record(t)) is None
                or (rec.get("blocked_until") and parse_utc(rec["blocked_until"]) <= run_deadline_dt)
                for t in selection.third_family_targets
            )
            if not third_ok:
                observations.append("no_third_family_tiebreaker_available")
                limitations_out.append("no_third_family_tiebreaker_available")

        return {
            "criterion_id": criterion["criterion_id"],
            "required": criterion["required"],
            "requirement_ids": list(criterion.get("requirement_ids", [])),
            "method": "model_rubric",
            "status": crit_status,
            "evidence_refs": ev_refs,
            "observations": observations,
            "error_record_ids": error_ids,
        }, "completed"

    def reconcile_evaluation(self, runner: Any, evaluation_id: str, *, force_cancel: bool = False) -> bool:
        """Reconcilia juízes com término unknown (TK21); retorna True se fechou report, False se expirou/indeterminate."""
        ev = runner.projection.evaluations[evaluation_id]
        scope = ev.request["scope"]
        task_id = scope.get("task_id")
        if task_id and runner.projection.reconciliation_expired(task_id, self.clock.now()):
            runner._transition("task", task_id, "indeterminate", "reconciliation_timeout")
            return False

        adapter = self.judge_adapter if self.judge_adapter is not None else runner.executor
        attempt = runner.projection.attempts[scope["attempt_id"]]
        bindings = ev.request["subject_bindings"]
        criteria = ev.request["criteria_snapshot"]["criteria"]
        subject_artifacts = []
        for binding in bindings:
            desc = runner.projection.artifacts[binding["artifact"]["artifact_id"]]
            subject_artifacts.append((binding, runner.store.read(desc["locator"])))

        all_confirmed = True
        collected_outcomes: list[JudgeEvaluationOutcome] = []
        aborted_exec_status: str | None = None
        recovered_criterion: Record | None = None
        recovered_ctx_ref: Record | None = None
        recovered_started_at = ev.request.get("started_at") or self.clock.now()
        recovered_author_fam: list[str] = []
        recovered_author_vendor = "unknown"

        for jid, jc in list(ev.judge_calls.items()):
            meta = self._load_recovery_state(runner, jid)
            recovered_criterion = meta["criterion"]
            recovered_ctx_ref = meta.get("context_manifest_ref")
            recovered_started_at = meta.get("started_at", recovered_started_at)
            recovered_author_fam = meta.get("author_family_ids", [])
            recovered_author_vendor = meta.get("author_model_vendor", "unknown")
            dispatch_intent = meta["dispatch_intent"]
            dispatch_key = jc.intent_payload["dispatch_idempotency_key"]

            if jc.termination_certainty != "confirmed":
                deadline = jc.reconciliation_deadline or format_utc(
                    min(
                        parse_utc(self.clock.now()) + timedelta(milliseconds=runner.projection.limit("reconciliation_timeout_ms")),
                        parse_utc(runner.projection.intent["created_at"]) + timedelta(milliseconds=runner.projection.limit("max_run_duration_ms")),
                    )
                )
                runner.append("evaluator_reconciliation_requested", {
                    "judge_call_id": jid,
                    "dispatch_idempotency_key": dispatch_key,
                    "external_call_id": jc.external_call_id,
                    "deadline_at": deadline,
                }, "evaluation", evaluation_id, key=f"{dispatch_key}/reconcile/{len(runner.journal.events)+1}", actor="orchestrator")

                try:
                    obs = adapter.reconcile_judge(dispatch_key, jc.external_call_id)
                except ExecutorError:
                    all_confirmed = False
                    self.clock.advance_to(deadline)
                    runner._transition("task", task_id, "indeterminate", "reconciliation_timeout")
                    return False

                outcome_name = obs.outcome
                runner.append("evaluator_reconciliation_observed", {
                    "judge_call_id": jid,
                    "outcome": outcome_name,
                    "external_call_id": obs.external_call_id,
                    "evidence_refs": list(obs.evidence_refs),
                }, "evaluation", evaluation_id, key=f"{dispatch_key}/obs/{len(runner.journal.events)+1}", actor="evaluator")

                if outcome_name == "found_running" and force_cancel:
                    cobs = adapter.cancel_judge(dispatch_intent, "run_cancelled")
                    if cobs.termination_certainty == "confirmed":
                        re_obs = adapter.reconcile_judge(dispatch_key, jc.external_call_id)
                        outcome_name = re_obs.outcome

                if outcome_name not in {"found_finished", "found_failed", "found_stopped", "not_found"} or obs.termination_certainty != "confirmed":
                    all_confirmed = False
                    self.clock.advance_to(deadline)
                    runner._transition("task", task_id, "indeterminate", "reconciliation_timeout")
                    return False

                cause_map = {
                    "found_finished": "finished",
                    "found_failed": "failed",
                    "found_stopped": "cancel" if force_cancel or runner.projection.cancel_requested else "timeout",
                    "not_found": "failed",
                }
                term_cause = cause_map[outcome_name]
                not_found_flag = outcome_name == "not_found"
            else:
                term_cause = jc.terminal_cause or "finished"
                not_found_flag = jc.reconciled_outcome == "not_found"

            if not_found_flag:
                ws = self._workspace_from_recovery(runner, meta, {})
                ws.cleanup()
                self._delete_recovery_state(runner, jid)
                if not any(runner.projection.errors[eid]["code"] == "dispatch_not_executed" for eid in ev.error_record_ids):
                    runner._attach_error(
                        error_record(
                            run_id=runner.journal.run_id,
                            scope_type="evaluation",
                            scope_ref=evaluation_id,
                            phase="reconcile",
                            category="transport",
                            code="dispatch_not_executed",
                            message="despacho de juiz comprovadamente não executado",
                            timestamp=self.clock(),
                            retryability="retryable",
                            termination_certainty="confirmed",
                        ),
                        "evaluation",
                        evaluation_id,
                    )
                if jc.termination_certainty != "confirmed":
                    runner.append("evaluator_termination_confirmed", {
                        "judge_call_id": jid,
                        "cause": "failed",
                        "observed_at": self.clock(),
                        "resolved_model_id": None,
                        "provider_context_coverage": "unknown",
                    }, "evaluation", evaluation_id, key=dispatch_key + "/term", actor="evaluator")
                aborted_exec_status = "cancelled" if force_cancel else "failed"
                continue

            rubric_spec = self.resolve_rubric(recovered_criterion["pass_rule"]["rubric_ref"])
            ws = self._workspace_from_recovery(runner, meta, {})
            raw_outcome = adapter.collect_judge(
                dispatch_intent,
                rubric_spec=rubric_spec,
                subject_artifacts=subject_artifacts,
                eval_workspace=ws,
            )
            if term_cause == "timeout":
                from dataclasses import replace as _dc_replace
                raw_outcome = _dc_replace(raw_outcome, execution_status="stopped", stop_reason="timeout")

            if jc.termination_certainty != "confirmed":
                runner.append("evaluator_termination_confirmed", {
                    "judge_call_id": jid,
                    "cause": term_cause,
                    "observed_at": raw_outcome.observed_at,
                    "resolved_model_id": raw_outcome.resolved_model_id,
                    "provider_context_coverage": raw_outcome.provider_context_coverage,
                }, "evaluation", evaluation_id, key=dispatch_key + "/term", actor="evaluator")

            try:
                outcome = self.rubric_evaluator.collect_and_verify(
                    dispatch_intent=dispatch_intent,
                    raw_outcome=raw_outcome,
                    eval_workspace=ws,
                    rubric_spec=rubric_spec,
                    prompt_template_ref=jc.intent_payload["prompt_template_ref"],
                    rendered_prompt_digest=jc.intent_payload["rendered_prompt_digest"],
                    subject_artifacts=subject_artifacts,
                    author_family_ids=recovered_author_fam,
                    author_model_vendor=recovered_author_vendor,
                    evaluator_target=meta["evaluator_target"],
                    profile=meta["profile"],
                    runtime_ref=jc.intent_payload["runtime_ref"],
                    runtime_version=jc.intent_payload["runtime_version"],
                    store_root=runner.journal.directory.parent.parent,
                )
            finally:
                ws.cleanup()
                self._delete_recovery_state(runner, jid)
            if outcome.usage_record["usage_id"] not in ev.usage_record_ids:
                self._record_judge_outcome_events(runner, evaluation_id=evaluation_id, outcome=outcome)
            collected_outcomes.append(outcome)

        if not all_confirmed or recovered_criterion is None:
            return False

        # Reconstrói resultados finais determinísticos + critério reconciliado
        det_results = [
            {
                "criterion_id": c["criterion_id"],
                "required": c["required"],
                "requirement_ids": list(c.get("requirement_ids", [])),
                "method": c["method"],
                "status": "pass",
                "evidence_refs": [copy.deepcopy(b["artifact"]) for b in bindings],
                "observations": [],
                "error_record_ids": [],
            }
            for c in criteria if c["method"] != "model_rubric"
        ]
        if aborted_exec_status is not None or not collected_outcomes:
            exec_status = aborted_exec_status or "failed"
            crit_results = [
                *det_results,
                {
                    "criterion_id": recovered_criterion["criterion_id"],
                    "required": recovered_criterion["required"],
                    "requirement_ids": list(recovered_criterion.get("requirement_ids", [])),
                    "method": "model_rubric",
                    "status": "not_run",
                    "evidence_refs": [],
                    "observations": ["reconciled_without_output"],
                    "error_record_ids": list(runner.projection.evaluations[evaluation_id].error_record_ids),
                },
            ]
            limitations: list[str] = []
        else:
            run_deadline_dt = parse_utc(runner.projection.intent["created_at"]) + timedelta(
                milliseconds=runner.projection.limit("max_run_duration_ms")
            )
            selection, _, _ = self._select_for_criterion(
                runner, task_id=task_id, attempt=attempt, criterion=recovered_criterion
            )
            limitations = []
            crit_res, exec_status = self._consolidate_criterion_outcomes(
                runner,
                evaluation_id=evaluation_id,
                criterion=recovered_criterion,
                outcomes=collected_outcomes,
                selection=selection,
                run_deadline_dt=run_deadline_dt,
                limitations_out=limitations,
            )
            crit_results = [*det_results, crit_res]

        by_id = {r["criterion_id"]: r for r in crit_results}
        ordered = [by_id[c["criterion_id"]] for c in criteria]
        self._finish_evaluation_report(
            runner,
            evaluation_id=evaluation_id,
            evaluation_number=ev.request["evaluation_number"],
            scope=scope,
            subject_bindings=bindings,
            criteria=criteria,
            started_at=recovered_started_at,
            completed_at=self.clock(),
            execution_status=exec_status,
            criterion_results=ordered,
            context_manifest_ref=recovered_ctx_ref,
            limitations=limitations,
        )
        runner._transition("task", task_id, "evaluating", "evaluation_reconciled")
        return True
