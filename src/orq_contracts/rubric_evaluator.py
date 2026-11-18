"""Avaliador por modelo/rubrica read-only, auditoria pós-coleta e evidências (Fase 8 / 8C)."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Callable, Sequence

from .adapters.quota import QuotaLimitDetector
from .canonical import canonical_digest
from .executor import ExecutorError, Record, error_record, seal_record, stable_id
from .rubric_prep import (
    IsolatedEvalWorkspace,
    JudgeRawOutcome,
    compute_rubric_outcome,
    model_vendor_for_target,
    render_evaluation_prompt,
)
from .validator import validate_instance, validate_quota_consumption_record


_UNTRUSTED_ORQ_FINAL_BLOCK = re.compile(r"<untrusted_artifact\b.*?</untrusted_artifact>", re.DOTALL)
_DOTDOT_SEGMENT = re.compile(r"(?:^|[\\/\s\"'=])\.\.(?:[\\/]|$|\s|[\"'])")
_ABS_POSIX_TOKEN = re.compile(r"(?:^|[\s\"'=:(])(/[^\s\"')]+)")
_ABS_WIN_TOKEN = re.compile(r"(?:^|[\s\"'=:(])([A-Za-z]:\\[^\s\"')]+)")
_HOME_TOKEN = re.compile(r"(?:^|[\s\"'=:(])(~/[^\s\"')]*)")
_FORBIDDEN_ESCAPES = (
    ".claude/",
    ".claude\\",
    ".codex/",
    ".codex\\",
    ".gemini/",
    ".gemini\\",
    ".aws/",
    ".aws\\",
    "journal.jsonl",
    "/etc/passwd",
    "credentials.json",
)
_NO_JSON = object()


@dataclass(frozen=True, slots=True)
class CitationAuditResult:
    citation_verification_status: str
    normalized_assessments: tuple[Record, ...]
    raw_scores: dict[str, int]
    failure_detail: str | None = None


@dataclass(frozen=True, slots=True)
class JudgeEvaluationOutcome:
    judge_call_id: str
    raw_outcome: JudgeRawOutcome
    evidence: Record | None
    usage_record: Record
    quota_consumption: Record
    quota_state: Record | None
    errors: tuple[Record, ...]
    criterion_status: str
    execution_status: str
    observations: tuple[str, ...] = ()


def _reject_constant(value: str) -> None:
    raise ValueError(f"constante JSON inválida: {value}")


def _resolve_json_pointer(document: Any, pointer: str) -> bool:
    if pointer == "":
        return True
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False
    current = document
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False
            current = current[token]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                return False
            index = int(token)
            if not 0 <= index < len(current):
                return False
            current = current[index]
        else:
            return False
    return True


def _extract_final_rubric_evidence(raw: JudgeRawOutcome) -> tuple[Record | None, str | None]:
    """Desambigua e extrai ORQ_FINAL:{outputs:{rubric_evidence:...}} apenas do canal final."""
    text = raw.final_text
    if not isinstance(text, str) or not text.strip():
        return None, "saída final da CLI sem conteúdo"
    cleaned = _UNTRUSTED_ORQ_FINAL_BLOCK.sub("", text).strip()
    if cleaned.startswith("ORQ_FINAL:"):
        payload_text = cleaned[len("ORQ_FINAL:"):].strip()
    else:
        markers = [line.strip()[len("ORQ_FINAL:"):].strip() for line in cleaned.splitlines()
                   if line.strip().startswith("ORQ_FINAL:")]
        if not markers:
            return None, "marcador ORQ_FINAL: ausente na resposta final da CLI"
        payload_text = markers[-1]
    try:
        envelope = json.loads(payload_text, parse_constant=_reject_constant)
    except ValueError as error:
        return None, f"JSON inválido após ORQ_FINAL: {error}"
    if not isinstance(envelope, dict) or not isinstance(envelope.get("outputs"), dict):
        return None, "envelope ORQ_FINAL sem objeto outputs"
    evidence = envelope["outputs"].get("rubric_evidence")
    if not isinstance(evidence, dict):
        return None, "outputs sem objeto rubric_evidence"
    if not isinstance(evidence.get("dimension_assessments"), list) or not evidence["dimension_assessments"]:
        return None, "rubric_evidence sem dimension_assessments válido"
    summary = evidence.get("overall_summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2048:
        return None, "overall_summary inválido em rubric_evidence"
    return evidence, None


def verify_rubric_citations(
    rubric_spec: Record,
    raw_assessments: Sequence[Record],
    subject_artifacts: Sequence[tuple[Record, bytes]],
) -> CitationAuditResult:
    """Confere estrutura das dimensões e verifica citações literais contra os bytes reais."""
    expected_dims = rubric_spec["dimensions"]
    if len(raw_assessments) != len(expected_dims):
        raise ExecutorError("evaluator_invalid_output", "quantidade de dimensões diverge da RubricSpec")

    min_score = rubric_spec["scale"]["min_score"]
    max_score = rubric_spec["scale"]["max_score"]

    subjects: dict[tuple[str, str, str], tuple[str, list[str], Any]] = {}
    for binding, raw_bytes in subject_artifacts:
        norm = raw_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n")
        try:
            parsed_json = json.loads(norm)
        except ValueError:
            parsed_json = _NO_JSON
        key = (
            binding["output_id"],
            binding["artifact"]["artifact_id"],
            binding["artifact"]["content_digest"]["value"],
        )
        subjects[key] = (norm, norm.split("\n"), parsed_json)

    raw_scores: dict[str, int] = {}
    citation_invalid_reasons: list[str] = []
    staged: list[Record] = []

    for dim_spec, item in zip(expected_dims, raw_assessments):
        if not isinstance(item, dict):
            raise ExecutorError("evaluator_invalid_output", "dimension_assessment deve ser objeto")
        dim_id = dim_spec["dimension_id"]
        if item.get("dimension_id") != dim_id:
            raise ExecutorError("evaluator_invalid_output", f"dimensão esperada {dim_id}, recebida {item.get('dimension_id')}")
        score = item.get("raw_score")
        if type(score) is not int or not (min_score <= score <= max_score):
            raise ExecutorError("evaluator_invalid_output", f"raw_score inválido em {dim_id}")
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip() or not 10 <= len(rationale) <= 1024:
            raise ExecutorError("evaluator_invalid_output", f"rationale inválido em {dim_id}")
        citations = item.get("citations")
        if not isinstance(citations, list):
            raise ExecutorError("evaluator_invalid_output", f"citations deve ser lista em {dim_id}")

        raw_scores[dim_id] = score
        if (rubric_spec["require_evidence_citations"] or dim_spec["min_citations"] > 0) and len(citations) < dim_spec["min_citations"]:
            citation_invalid_reasons.append(f"{dim_id}: citações abaixo de min_citations")

        cleaned_citations: list[Record] = []
        for cit in citations:
            if not isinstance(cit, dict):
                raise ExecutorError("evaluator_invalid_output", f"citação malformada em {dim_id}")
            output_id = cit.get("output_id")
            artifact_id = cit.get("artifact_id")
            digest = cit.get("content_digest")
            locator = cit.get("locator")
            excerpt = cit.get("verbatim_excerpt")
            if (
                not isinstance(output_id, str)
                or not isinstance(artifact_id, str)
                or not isinstance(digest, dict)
                or digest.get("algorithm") != "sha256"
                or not isinstance(digest.get("value"), str)
                or not isinstance(locator, dict)
                or not isinstance(excerpt, str)
                or not 1 <= len(excerpt) <= 512
            ):
                citation_invalid_reasons.append(f"{dim_id}: campos de citação inválidos")
                continue

            entry = subjects.get((output_id, artifact_id, digest["value"]))
            if entry is None:
                citation_invalid_reasons.append(f"{dim_id}: citação aponta para artefato/digest fora de subject_bindings")
                continue
            text_norm, lines_norm, parsed_json = entry
            excerpt_norm = excerpt.replace("\r\n", "\n")
            if excerpt_norm not in text_norm:
                citation_invalid_reasons.append(f"{dim_id}: verbatim_excerpt ausente nos bytes reais")

            kind = locator.get("kind")
            if kind == "line_range":
                start_line = locator.get("start_line")
                end_line = locator.get("end_line")
                if (
                    type(start_line) is not int
                    or type(end_line) is not int
                    or start_line < 1
                    or end_line < start_line
                    or end_line > len(lines_norm)
                ):
                    citation_invalid_reasons.append(f"{dim_id}: line_range fora dos limites")
                else:
                    slice_text = "\n".join(lines_norm[start_line - 1 : end_line])
                    if excerpt_norm not in slice_text:
                        citation_invalid_reasons.append(f"{dim_id}: verbatim_excerpt ausente no intervalo line_range")
            elif kind == "json_pointer":
                pointer = locator.get("pointer")
                if parsed_json is _NO_JSON or not _resolve_json_pointer(parsed_json, str(pointer)):
                    citation_invalid_reasons.append(f"{dim_id}: json_pointer inválido no artefato")
            else:
                citation_invalid_reasons.append(f"{dim_id}: tipo de locator inválido")

            cleaned_citations.append({
                "output_id": output_id,
                "artifact_id": artifact_id,
                "content_digest": copy.deepcopy(digest),
                "locator": copy.deepcopy(locator),
                "verbatim_excerpt": excerpt,
            })

        staged.append({
            "dimension_id": dim_id,
            "raw_score": score,
            "rationale": rationale.strip(),
            "citations": cleaned_citations,
        })

    status = "invalid_citation" if citation_invalid_reasons else "verified"
    outcome = compute_rubric_outcome(rubric_spec, raw_scores, citation_verification_status=status)
    final_assessments: list[Record] = []
    for item, dim_out in zip(staged, outcome.dimension_outcomes):
        final_assessments.append({
            **item,
            "normalized_dimension_score": dim_out.normalized_dimension_score,
            "meets_mandatory_minimum": dim_out.meets_mandatory_minimum,
        })

    return CitationAuditResult(
        citation_verification_status=status,
        normalized_assessments=tuple(final_assessments),
        raw_scores=raw_scores,
        failure_detail="; ".join(citation_invalid_reasons) if citation_invalid_reasons else None,
    )


def _is_within_workspace(token: str, ws_resolved: Path) -> bool:
    if token.startswith("~"):
        return False
    ws_posix = ws_resolved.as_posix()
    if token.startswith("/"):
        if token == ws_posix or token.startswith(ws_posix.rstrip("/") + "/"):
            return True
        if ws_resolved.drive.lower() == "c:":
            wsl_ws = "/mnt/c/" + "/".join(ws_resolved.parts[1:])
            if token == wsl_ws or token.startswith(wsl_ws.rstrip("/") + "/"):
                return True
        # Também confere via PurePosixPath para tokens Unix dentro de POSIX
        try:
            return PurePosixPath(token).is_relative_to(PurePosixPath(ws_posix))
        except ValueError:
            return False
    if len(token) >= 3 and token[1:3] == ":\\":
        try:
            return PureWindowsPath(token).is_relative_to(PureWindowsPath(str(ws_resolved)))
        except ValueError:
            return False
    return True


def audit_judge_tool_trail(
    *,
    stdout_text: str,
    stderr_text: str,
    tool_events: Sequence[str],
    eval_workspace: Path,
    store_root: Path | None = None,
) -> tuple[bool, str | None]:
    """Audita chamadas de ferramenta da CLI-juiz contra leituras fora de <eval_workspace>."""
    ws_resolved = eval_workspace.resolve()
    store_resolved = store_root.resolve() if store_root is not None else None
    lines: list[str] = list(tool_events)
    seen = set(lines)
    for stream in (stdout_text, stderr_text):
        for raw in stream.splitlines():
            stripped = raw.strip()
            if stripped.startswith("[tool]") and stripped not in seen:
                lines.append(stripped)
                seen.add(stripped)

    for entry in lines:
        # Placeholder "[tool] read_file ..." emitido por fixture padrão não é traversal "..".
        normalized_entry = entry.replace(" ...", " ")
        lowered = normalized_entry.lower()
        for pattern in _FORBIDDEN_ESCAPES:
            if pattern in lowered:
                return False, f"acesso proibido detectado na trilha de ferramentas: {pattern}"
        if store_resolved is not None:
            for root_str in (str(store_resolved).lower(), store_resolved.as_posix().lower()):
                if root_str in lowered:
                    return False, "referência a store_root detectada na trilha de ferramentas"
        if _DOTDOT_SEGMENT.search(normalized_entry):
            return False, "traversal '..' detectado na trilha de ferramentas"
        for regex in (_HOME_TOKEN, _ABS_POSIX_TOKEN, _ABS_WIN_TOKEN):
            for match in regex.finditer(normalized_entry):
                token = match.group(1).rstrip(",\"';)]}")
                if not _is_within_workspace(token, ws_resolved):
                    return False, f"caminho fora de <eval_workspace>: {token}"
    return True, None


class RubricModelEvaluator:
    """Coordena invocação read-only de juiz, auditoria pós-coleta e registros de uso/cota."""

    def __init__(self, *, clock: Callable[[], str]) -> None:
        self.clock = clock

    def build_usage_and_quota(
        self,
        *,
        dispatch_intent: Record,
        raw_outcome: JudgeRawOutcome,
        evaluator_target: Record,
        profile: Record,
        quota_exhausted: bool,
        accepted_output: bool,
        now: str,
    ) -> tuple[Record, Record]:
        runtime_ref = {
            "id": evaluator_target.get("runtime_ref", {}).get("id", f"runtime-{profile['cli_executable']}"),
            "version": evaluator_target.get("runtime_ref", {}).get("version", "0.1"),
            "content_digest": evaluator_target.get("runtime_ref", {}).get("content_digest", canonical_digest(profile["cli_executable"])),
        }
        measurements = [
            {"metric_id": key, "quantity": str(value), "unit": "token", "status": "reported", "source_ref": runtime_ref}
            for key, value in (("input_tokens", raw_outcome.input_tokens), ("output_tokens", raw_outcome.output_tokens))
            if type(value) is int and value >= 0
        ]
        usage = seal_record({
            "schema_version": "0.1",
            "usage_id": f"usage-{dispatch_intent['judge_call_id']}",
            "run_id": dispatch_intent["run_id"],
            "scope_type": "evaluation",
            "scope_ref": dispatch_intent["evaluation_id"],
            "subscription_id": profile["subscription_id"],
            "provider_ref": copy.deepcopy(evaluator_target.get("provider_ref")),
            "observed_at": raw_outcome.observed_at,
            "recorded_at": now,
            "measurements": measurements,
            "cost_lines": [{
                "status": "unavailable",
                "amount": None,
                "currency": None,
                "pricing_ref": None,
                "method": "personal_subscription_unpriced",
            }],
            "coverage": {
                "reported": [item["metric_id"] for item in measurements],
                "estimated": [],
                "unavailable": ["cost"],
            },
            "supersedes_usage_id": None,
        }, "usage_record")
        usage_issues = validate_instance(usage, "usage_record")
        if usage_issues:
            raise ExecutorError(usage_issues[0].code, usage_issues[0].message)

        consumption = seal_record({
            "schema_version": "0.1",
            "quota_consumption_id": stable_id("consumption", dispatch_intent["dispatch_idempotency_key"]),
            "scope_type": "evaluation",
            "run_id": dispatch_intent["run_id"],
            "task_id": dispatch_intent["task_id"],
            "attempt_id": dispatch_intent["attempt_id"],
            "evaluation_id": dispatch_intent["evaluation_id"],
            "judge_call_id": dispatch_intent["judge_call_id"],
            "subscription_id": profile["subscription_id"],
            "family_id": profile["family_id"],
            "quota_pool_id": profile["quota_pool_id"],
            "usage_record_ref": {
                "id": usage["usage_id"],
                "version": "0.1",
                "content_digest": canonical_digest(usage),
            },
            "messages_consumed": 1,
            "input_tokens": raw_outcome.input_tokens,
            "output_tokens": raw_outcome.output_tokens,
            "credit_multiplier": None,
            "quota_outcome": (
                "quota_exhausted_on_call"
                if quota_exhausted
                else ("accepted" if accepted_output else "unknown")
            ),
            "recorded_at": now,
        }, "quota_consumption_record")
        cons_issues = validate_quota_consumption_record(consumption, usage_record=usage)
        if cons_issues:
            raise ExecutorError(cons_issues[0].code, cons_issues[0].message)
        return usage, consumption

    def _eval_error(
        self,
        *,
        run_id: str,
        evaluation_id: str,
        code: str,
        category: str,
        message: str,
        timestamp: str,
        retryability: str = "retryable",
        certainty: str = "confirmed",
        external_call_id: str | None = None,
        quota_state: Record | None = None,
    ) -> Record:
        record = error_record(
            run_id=run_id,
            scope_type="evaluation",
            scope_ref=evaluation_id,
            phase="evaluation",
            category=category,
            code="execution_failed" if quota_state else code,
            message=message[:4096],
            timestamp=timestamp,
            retryability=retryability,
            termination_certainty=certainty,
            external_call_id=external_call_id,
        )
        if quota_state is not None:
            record["code"] = code
            record["quota_state"] = quota_state
            return seal_record(record, "error_record")
        return record

    def _detect_quota(
        self,
        *,
        raw_outcome: JudgeRawOutcome,
        dispatch_intent: Record,
        profile: Record,
        now: str,
    ) -> tuple[bool, Record | None, str | None, str]:
        detector = QuotaLimitDetector(
            profile["cli_executable"],
            local_timezone=profile["local_timezone"],
            default_cooldown_ms=profile["default_cooldown_ms"],
        )
        if raw_outcome.quota_exhausted_detail is not None:
            detail = raw_outcome.quota_exhausted_detail
            detection = detector.detect(
                returncode=1,
                final_response=False,
                diagnostics=(detail["excerpt"],),
                observed_at=now,
            )
            if detection.reason == "quota_exhausted":
                state = detector.state_record(
                    detection, profile=profile, intent=dispatch_intent, observed_at=now,
                    evaluation_id=dispatch_intent["evaluation_id"],
                )
                return True, state, detection.excerpt, "quota_exhausted"
            status = "cooldown" if detail.get("reset_source") == "cooldown_policy" else "quota_exhausted"
            state_body = {
                "schema_version": "0.1",
                "quota_event_id": stable_id("quota", dispatch_intent["dispatch_idempotency_key"]),
                "run_id": dispatch_intent["run_id"],
                "source_attempt_id": None,
                "source_evaluation_id": dispatch_intent["evaluation_id"],
                **{k: profile[k] for k in ("subscription_id", "family_id", "quota_pool_id")},
                "status": status,
                "observed_at": now,
                "reset_at": None if status == "cooldown" else detail.get("blocked_until"),
                "blocked_until": detail.get("blocked_until") or now,
                "reset_source": detail.get("reset_source", "cooldown_policy"),
                "raw_message_excerpt": detail["excerpt"],
            }
            if status == "cooldown":
                state_body["default_cooldown_ms"] = profile["default_cooldown_ms"]
            return True, seal_record(state_body, "quota_state_record"), detail["excerpt"], "quota_exhausted"

        final_valid = False
        if raw_outcome.final_text:
            parsed_ev, _ = _extract_final_rubric_evidence(raw_outcome)
            final_valid = parsed_ev is not None
        detection = detector.detect(
            returncode=raw_outcome.returncode,
            final_response=final_valid,
            diagnostics=raw_outcome.diagnostics,
            observed_at=now,
        )
        if detection.reason == "quota_exhausted":
            state = detector.state_record(
                detection,
                profile=profile,
                intent=dispatch_intent,
                observed_at=now,
                evaluation_id=dispatch_intent["evaluation_id"],
            )
            return True, state, detection.excerpt, "quota_exhausted"
        return False, None, None, detection.reason

    def collect_and_verify(
        self,
        *,
        dispatch_intent: Record,
        raw_outcome: JudgeRawOutcome,
        eval_workspace: IsolatedEvalWorkspace,
        rubric_spec: Record,
        prompt_template_ref: Record,
        rendered_prompt_digest: Record,
        subject_artifacts: Sequence[tuple[Record, bytes]],
        author_family_ids: Sequence[str],
        author_model_vendor: str,
        evaluator_target: Record,
        profile: Record,
        runtime_ref: Record,
        runtime_version: str,
        store_root: Path | None = None,
    ) -> JudgeEvaluationOutcome:
        """Executa auditoria completa do retorno do juiz, sem confiar em notas livres."""
        now = self.clock()
        run_id = dispatch_intent["run_id"]
        evaluation_id = dispatch_intent["evaluation_id"]
        criterion_id = dispatch_intent["criterion_id"]
        judge_call_id = dispatch_intent["judge_call_id"]
        ext_id = raw_outcome.external_call_id

        errors: list[Record] = []
        observations: list[str] = []
        evidence: Record | None = None
        criterion_status = "not_run"
        exec_status = "failed"
        accepted_output = False

        quota_exhausted, quota_state, quota_excerpt, detector_reason = self._detect_quota(
            raw_outcome=raw_outcome,
            dispatch_intent=dispatch_intent,
            profile=profile,
            now=now,
        )

        # Sempre verificar integridade do workspace e trilha antes de aceitar qualquer resultado
        intact, ws_diffs = eval_workspace.verify_integrity()
        trail_ok, trail_reason = audit_judge_tool_trail(
            stdout_text=raw_outcome.stdout_text,
            stderr_text=raw_outcome.stderr_text,
            tool_events=raw_outcome.tool_events,
            eval_workspace=eval_workspace.path,
            store_root=store_root,
        )

        if quota_exhausted:
            errors.append(self._eval_error(
                run_id=run_id,
                evaluation_id=evaluation_id,
                code="quota_limit_reached",
                category="provider",
                message=quota_excerpt or "quota exhausted",
                timestamp=now,
                retryability="retryable",
                certainty="confirmed",
                external_call_id=ext_id,
                quota_state=quota_state,
            ))
            exec_status = "failed"
        elif raw_outcome.execution_status == "stopped":
            if raw_outcome.stop_reason == "timeout":
                errors.append(self._eval_error(
                    run_id=run_id,
                    evaluation_id=evaluation_id,
                    code="timeout",
                    category="runtime",
                    message="avaliação por rubrica excedeu prazo limite",
                    timestamp=now,
                    retryability="retryable",
                    certainty="confirmed",
                    external_call_id=ext_id,
                ))
                exec_status = "timed_out"
            else:
                exec_status = "cancelled"
        elif not intact:
            errors.append(self._eval_error(
                run_id=run_id,
                evaluation_id=evaluation_id,
                code="eval_workspace_mutated",
                category="integrity",
                message=f"workspace de avaliação modificado: {', '.join(ws_diffs)}",
                timestamp=now,
                retryability="retryable",
                certainty="confirmed",
                external_call_id=ext_id,
            ))
            exec_status = "failed"
        elif not trail_ok:
            errors.append(self._eval_error(
                run_id=run_id,
                evaluation_id=evaluation_id,
                code="eval_sandbox_escape_attempt",
                category="policy",
                message=trail_reason or "tentativa de leitura fora do workspace",
                timestamp=now,
                retryability="retryable",
                certainty="confirmed",
                external_call_id=ext_id,
            ))
            exec_status = "failed"
        elif raw_outcome.execution_status != "finished":
            retryability = (
                "retryable"
                if detector_reason in {"network_unavailable", "authentication_unavailable"}
                else "unknown"
            )
            errors.append(self._eval_error(
                run_id=run_id,
                evaluation_id=evaluation_id,
                code="execution_failed",
                category="provider",
                message=detector_reason if detector_reason != "none" else "falha na execução da CLI avaliadora",
                timestamp=now,
                retryability=retryability,
                certainty="confirmed",
                external_call_id=ext_id,
            ))
            exec_status = "failed"
        else:
            parsed_body, parse_err = _extract_final_rubric_evidence(raw_outcome)
            if parsed_body is None:
                errors.append(self._eval_error(
                    run_id=run_id,
                    evaluation_id=evaluation_id,
                    code="evaluator_invalid_output",
                    category="evaluation",
                    message=parse_err or "saída sem ORQ_FINAL válido",
                    timestamp=now,
                    retryability="retryable",
                    certainty="confirmed",
                    external_call_id=ext_id,
                ))
                exec_status = "failed"
            else:
                try:
                    cit_audit = verify_rubric_citations(
                        rubric_spec,
                        parsed_body["dimension_assessments"],
                        subject_artifacts,
                    )
                except ExecutorError as err:
                    errors.append(self._eval_error(
                        run_id=run_id,
                        evaluation_id=evaluation_id,
                        code="evaluator_invalid_output",
                        category="evaluation",
                        message=err.message,
                        timestamp=now,
                        retryability="retryable",
                        certainty="confirmed",
                        external_call_id=ext_id,
                    ))
                    exec_status = "failed"
                else:
                    outcome = compute_rubric_outcome(
                        rubric_spec,
                        cit_audit.raw_scores,
                        citation_verification_status=cit_audit.citation_verification_status,
                    )
                    if cit_audit.citation_verification_status == "invalid_citation":
                        errors.append(self._eval_error(
                            run_id=run_id,
                            evaluation_id=evaluation_id,
                            code="evidence_citation_invalid",
                            category="evaluation",
                            message=cit_audit.failure_detail or "citação literal inválida",
                            timestamp=now,
                            retryability="retryable",
                            certainty="confirmed",
                            external_call_id=ext_id,
                        ))
                    elif outcome.reason == "score_in_inconclusive_band":
                        observations.append("score_in_inconclusive_band")

                    target_ref = {
                        "id": evaluator_target["target_id"],
                        "version": evaluator_target["version"],
                        "content_digest": canonical_digest(evaluator_target),
                    }
                    vendor = model_vendor_for_target(evaluator_target) or "unknown"
                    evidence = seal_record({
                        "schema_version": "0.1",
                        "evidence_id": stable_id("rubric-evidence", f"{evaluation_id}:{criterion_id}:{judge_call_id}"),
                        "judge_call_id": judge_call_id,
                        "run_id": run_id,
                        "evaluation_id": evaluation_id,
                        "criterion_id": criterion_id,
                        "rubric_ref": {
                            "id": rubric_spec["rubric_id"],
                            "version": rubric_spec["version"],
                            "content_digest": copy.deepcopy(rubric_spec["record_hash"]),
                        },
                        "prompt_template_ref": copy.deepcopy(prompt_template_ref),
                        "rendered_prompt_digest": copy.deepcopy(rendered_prompt_digest),
                        "evaluator_family_id": profile["family_id"],
                        "evaluator_model_vendor": vendor,
                        "author_family_ids": list(author_family_ids),
                        "author_model_vendor": author_model_vendor,
                        "evaluator_target_ref": target_ref,
                        "subscription_id": profile["subscription_id"],
                        "runtime_ref": copy.deepcopy(runtime_ref),
                        "runtime_version": runtime_version,
                        "requested_model_id": raw_outcome.requested_model_id,
                        "resolved_model_id": raw_outcome.resolved_model_id,
                        "dimension_assessments": list(cit_audit.normalized_assessments),
                        "normalized_score": outcome.normalized_score,
                        "blocking_dimension_failed": outcome.blocking_dimension_failed,
                        "citation_verification_status": cit_audit.citation_verification_status,
                        "derived_status": outcome.derived_status,
                        "overall_summary": parsed_body["overall_summary"].strip(),
                        "recorded_at": now,
                    }, "rubric_evaluation_evidence")
                    ev_issues = validate_instance(evidence, "rubric_evaluation_evidence")
                    if ev_issues:
                        raise ExecutorError(ev_issues[0].code, ev_issues[0].message)
                    criterion_status = outcome.derived_status
                    exec_status = "completed"
                    accepted_output = True
                    observations.extend([
                        f"normalized_score={outcome.normalized_score}",
                        f"evaluator_family={profile['family_id']}",
                        f"citations_verified={str(cit_audit.citation_verification_status == 'verified').lower()}",
                    ])

        usage_record, quota_consumption = self.build_usage_and_quota(
            dispatch_intent=dispatch_intent,
            raw_outcome=raw_outcome,
            evaluator_target=evaluator_target,
            profile=profile,
            quota_exhausted=quota_exhausted,
            accepted_output=accepted_output,
            now=now,
        )
        return JudgeEvaluationOutcome(
            judge_call_id=judge_call_id,
            raw_outcome=raw_outcome,
            evidence=evidence,
            usage_record=usage_record,
            quota_consumption=quota_consumption,
            quota_state=quota_state,
            errors=tuple(errors),
            criterion_status=criterion_status,
            execution_status=exec_status,
            observations=tuple(observations),
        )

    def run_single_judge(
        self,
        *,
        adapter: Any,
        run_id: str,
        task_id: str,
        attempt_id: str,
        attempt_number: int,
        evaluation_id: str,
        evaluation_number: int,
        criterion_id: str,
        judge_call_id: str,
        judge_index: int,
        evaluator_target: Record,
        profile: Record,
        rubric_spec: Record,
        subject_artifacts: Sequence[tuple[Record, bytes]],
        author_family_ids: Sequence[str],
        author_model_vendor: str,
        deadline_at: str,
        evaluation_timeout_ms: int = 60000,
        evaluator_role_id: str = "code_reviewer",
        prompt_template_ref: Record | None = None,
        normative_context: Record | None = None,
        eval_temp_root: Path | None = None,
        store_root: Path | None = None,
        cleanup_workspace: bool = True,
    ) -> JudgeEvaluationOutcome:
        """Executa fluxo completo ponta a ponta de um único juiz (8C / auditoria)."""
        rendered = render_evaluation_prompt(
            run_id=run_id,
            evaluation_id=evaluation_id,
            criterion_id=criterion_id,
            judge_call_id=judge_call_id,
            rubric_spec=rubric_spec,
            subject_artifacts=subject_artifacts,
            evaluator_role_id=evaluator_role_id,
            prompt_template_ref=prompt_template_ref,
            normative_context=normative_context,
        )
        ws = IsolatedEvalWorkspace.create(
            workspace_id=f"eval-{judge_call_id}",
            rubric_spec=rubric_spec,
            subject_artifacts=subject_artifacts,
            eval_temp_root=eval_temp_root,
            store_root=store_root,
            normative_context=normative_context,
            wsl_compatible=bool(getattr(adapter, "_wsl", False)),
        )
        try:
            prompt_bytes = rendered.prompt_text.encode("utf-8")
            target_ref = {
                "id": evaluator_target["target_id"],
                "version": evaluator_target["version"],
                "content_digest": canonical_digest(evaluator_target),
            }
            dispatch_intent = {
                "run_id": run_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "evaluation_id": evaluation_id,
                "evaluation_number": evaluation_number,
                "criterion_id": criterion_id,
                "judge_call_id": judge_call_id,
                "judge_index": judge_index,
                "dispatch_idempotency_key": f"{run_id}/{judge_call_id}",
                "evaluator_family_id": profile["family_id"],
                "evaluator_model_vendor": model_vendor_for_target(evaluator_target) or "unknown",
                "target_ref": target_ref,
                "subscription_id": profile["subscription_id"],
                "quota_pool_id": profile["quota_pool_id"],
                "requested_model_id": evaluator_target["model_ref"]["id"],
                "prompt_template_ref": rendered.prompt_template_ref,
                "rendered_prompt_digest": rendered.rendered_prompt_digest,
                "eval_workspace_digest": ws.eval_workspace_digest,
                "evaluation_timeout_ms": evaluation_timeout_ms,
                "deadline_at": deadline_at,
            }
            ack = adapter.dispatch_judge(dispatch_intent, prompt_bytes, ws)
            if not ack.acknowledged and ack.errors:
                raise ExecutorError("dispatch_failed", ack.errors[0]["message"])
            obs = adapter.observe_judge(dispatch_intent, deadline_at)
            if obs.status == "running" and obs.termination_certainty != "confirmed":
                cancel_obs = adapter.cancel_judge(dispatch_intent, "evaluation_timeout")
                if cancel_obs.termination_certainty != "confirmed":
                    raise ExecutorError("termination_unknown", "término do juiz incerto", termination_certainty="unknown")
            raw_outcome = adapter.collect_judge(
                dispatch_intent,
                rubric_spec=rubric_spec,
                subject_artifacts=subject_artifacts,
                eval_workspace=ws,
            )
            desc = adapter.describe() if hasattr(adapter, "describe") else None
            runtime_ref = (
                copy.deepcopy(desc.runtime_ref)
                if desc is not None
                else {
                    "id": evaluator_target.get("runtime_ref", {}).get("id", f"runtime-{profile['cli_executable']}"),
                    "version": evaluator_target.get("runtime_ref", {}).get("version", "0.1"),
                    "content_digest": canonical_digest(profile["cli_executable"]),
                }
            )
            runtime_version = desc.runtime_version if desc is not None else "0.1"
            return self.collect_and_verify(
                dispatch_intent=dispatch_intent,
                raw_outcome=raw_outcome,
                eval_workspace=ws,
                rubric_spec=rubric_spec,
                prompt_template_ref=rendered.prompt_template_ref,
                rendered_prompt_digest=rendered.rendered_prompt_digest,
                subject_artifacts=subject_artifacts,
                author_family_ids=author_family_ids,
                author_model_vendor=author_model_vendor,
                evaluator_target=evaluator_target,
                profile=profile,
                runtime_ref=runtime_ref,
                runtime_version=runtime_version,
                store_root=store_root,
            )
        finally:
            if cleanup_workspace:
                ws.cleanup()
