"""Calibração de juízes por rubrica e auditoria pós-hoc somente leitura (Fase 8 / 8E)."""

from __future__ import annotations

import copy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from ._storage import confined_path, ensure_directory, publish_bytes
from .canonical import canonical_digest, canonicalize
from .executor import ExecutorError, Record, bytes_digest, seal_record, stable_id
from .experiment_7f import wilcoxon_signed_rank_exact
from .rubric_evaluator import RubricModelEvaluator, verify_rubric_citations
from .rubric_prep import (
    IsolatedEvalWorkspace,
    JudgeRawOutcome,
    build_scripted_judge_payload,
    compute_rubric_outcome,
    default_prompt_template_ref,
    model_vendor_for_target,
    render_evaluation_prompt,
    select_evaluator_targets,
)
from .validator import validate_instance, validate_quota_consumption_record


QUANT4 = Decimal("0.0001")
_LOCATOR_RE = re.compile(r"^orq-store:sha256/([a-f0-9]{64})$")
CALIBRATION_FAMILIES = ("family_claude", "family_codex", "family_gemini", "family_kiro")

GATE_CFPR_MAX = Decimal("0.05")
GATE_RECALL_MIN = Decimal("0.80")
GATE_VALID_CITATIONS_MIN = Decimal("0.98")
GATE_ALPHA_GLOBAL_MIN = Decimal("0.67")
GATE_ALPHA_DIM_MIN = Decimal("0.60")
GATE_MAD_INTER_MAX = Decimal("0.15")
GATE_KAPPA_MIN = Decimal("0.70")
GATE_SELF_DELTA_MAX = Decimal("0.10")
GATE_SELF_P_MAX = Decimal("0.05")


def _q4(value: Decimal) -> str:
    return str(value.quantize(QUANT4, rounding=ROUND_HALF_UP))


def _ratio(num: int, den: int) -> Decimal:
    if den <= 0:
        return Decimal("0.0000")
    return (Decimal(num) / Decimal(den)).quantize(QUANT4, rounding=ROUND_HALF_UP)


def compute_cfpr(predicted: Sequence[str], expected: Sequence[str]) -> Decimal:
    """Critical False Pass Rate: fração de seeded_fail classificada como pass."""
    fail_indices = [i for i, exp in enumerate(expected) if exp == "fail"]
    if not fail_indices:
        return Decimal("0.0000")
    false_pass = sum(1 for i in fail_indices if predicted[i] == "pass")
    return _ratio(false_pass, len(fail_indices))


def compute_precision_recall_f1(
    predicted: Sequence[str],
    expected: Sequence[str],
) -> tuple[Decimal, Decimal, Decimal]:
    """Precisão, recall e F1 na detecção da classe negativa (status='fail')."""
    tp = sum(1 for p, e in zip(predicted, expected, strict=True) if p == "fail" and e == "fail")
    fp = sum(1 for p, e in zip(predicted, expected, strict=True) if p == "fail" and e != "fail")
    fn = sum(1 for p, e in zip(predicted, expected, strict=True) if p != "fail" and e == "fail")
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    if precision + recall == Decimal("0"):
        f1 = Decimal("0.0000")
    else:
        f1 = ((Decimal("2") * precision * recall) / (precision + recall)).quantize(QUANT4, rounding=ROUND_HALF_UP)
    return precision, recall, f1


def cohen_kappa_binary(predicted: Sequence[str], expected: Sequence[str]) -> Decimal:
    """Kappa de Cohen para concordância discreta contra rótulo humano."""
    n = len(predicted)
    if n == 0:
        return Decimal("0.0000")
    observed = Decimal(sum(1 for p, e in zip(predicted, expected, strict=True) if p == e)) / Decimal(n)
    labels = sorted(set(predicted) | set(expected))
    expected_agree = Decimal(0)
    for label in labels:
        p_a = Decimal(sum(1 for p in predicted if p == label)) / Decimal(n)
        p_b = Decimal(sum(1 for e in expected if e == label)) / Decimal(n)
        expected_agree += p_a * p_b
    denom = Decimal(1) - expected_agree
    if denom == Decimal(0):
        kappa = Decimal("1.0000") if observed == Decimal(1) else Decimal("0.0000")
    else:
        kappa = (observed - expected_agree) / denom
    return kappa.quantize(QUANT4, rounding=ROUND_HALF_UP)


def krippendorff_alpha_ordinal(ratings_matrix: Sequence[Sequence[int | Decimal]]) -> Decimal:
    """Alfa ordinal exato de Krippendorff para m avaliadores sobre N itens."""
    if not ratings_matrix:
        return Decimal("0.0000")
    counts: dict[Decimal, int] = {}
    unit_values: list[list[Decimal]] = []
    for row in ratings_matrix:
        vals = [v if isinstance(v, Decimal) else Decimal(str(v)) for v in row]
        if len(vals) < 2:
            continue
        unit_values.append(vals)
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
    total_n = sum(counts.values())
    if total_n < 2 or not unit_values:
        return Decimal("0.0000")
    categories = sorted(counts)
    if len(categories) == 1:
        return Decimal("1.0000")
    cat_index = {c: idx for idx, c in enumerate(categories)}
    n_by_idx = [counts[c] for c in categories]

    def delta_sq(a: int, b: int) -> float:
        if a == b:
            return 0.0
        lo, hi = min(a, b), max(a, b)
        between = sum(n_by_idx[lo : hi + 1]) - (n_by_idx[lo] + n_by_idx[hi]) / 2.0
        return between * between

    do_num = 0.0
    for vals in unit_values:
        m_u = len(vals)
        pair_sum = 0.0
        for i in range(m_u):
            ci = cat_index[vals[i]]
            for j in range(m_u):
                if i != j:
                    pair_sum += delta_sq(ci, cat_index[vals[j]])
        do_num += pair_sum / (m_u - 1)
    de_num = 0.0
    for idx_a, count_a in enumerate(n_by_idx):
        for idx_b, count_b in enumerate(n_by_idx):
            if idx_a != idx_b:
                de_num += count_a * count_b * delta_sq(idx_a, idx_b)
    if de_num == 0.0:
        return Decimal("1.0000") if do_num == 0.0 else Decimal("0.0000")
    alpha = 1.0 - ((total_n - 1) * do_num / de_num)
    return Decimal(str(alpha)).quantize(QUANT4, rounding=ROUND_HALF_UP)


def kendall_tau_b(xs: Sequence[Decimal | float | int], ys: Sequence[Decimal | float | int]) -> Decimal:
    """Coeficiente tau-b de Kendall com correção exata para empates."""
    n = len(xs)
    if n < 2:
        return Decimal("0.0000")
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif (dx > 0 and dy > 0) or (dx < 0 and dy < 0):
                concordant += 1
            else:
                discordant += 1
    denom_sq = (concordant + discordant + ties_x) * (concordant + discordant + ties_y)
    if denom_sq <= 0:
        return Decimal("1.0000") if (ties_x == 0 and ties_y == 0) or list(xs) == list(ys) else Decimal("0.0000")
    tau = (concordant - discordant) / math.sqrt(denom_sq)
    return Decimal(str(tau)).quantize(QUANT4, rounding=ROUND_HALF_UP)


def compute_mad_inter(scores_matrix: Sequence[Sequence[Decimal]]) -> Decimal:
    """MAD_inter: média das diferenças absolutas |S_a - S_b| entre pares de avaliadores."""
    total = Decimal(0)
    pair_count = 0
    for row in scores_matrix:
        m = len(row)
        for i in range(m):
            for j in range(i + 1, m):
                total += abs(row[i] - row[j])
                pair_count += 1
    if pair_count == 0:
        return Decimal("0.0000")
    return (total / Decimal(pair_count)).quantize(QUANT4, rounding=ROUND_HALF_UP)


def _item_subject_pairs(item: Mapping[str, Any]) -> list[tuple[Record, bytes]]:
    if "subject_artifacts" in item:
        pairs: list[tuple[Record, bytes]] = []
        for idx, art in enumerate(item["subject_artifacts"], 1):
            raw = art["content"].encode("utf-8")
            pairs.append((
                {
                    "output_id": art.get("output_id", f"output-{idx}"),
                    "artifact": {"artifact_id": art["artifact_id"], "content_digest": bytes_digest(raw)},
                },
                raw,
            ))
        return pairs
    raw = str(item.get("content", "")).encode("utf-8")
    art_id = item.get("artifact_id", f"art-{item['item_id']}")
    return [({"output_id": "report", "artifact": {"artifact_id": art_id, "content_digest": bytes_digest(raw)}}, raw)]


def _evaluate_corpus_entry(
    *,
    rubric: Mapping[str, Any],
    item: Mapping[str, Any],
    response_spec: Mapping[str, Any],
) -> tuple[str, Decimal, dict[str, int], int, int]:
    """Avalia uma resposta de juiz no corpus: citações literais + pontuação determinística."""
    subject_pairs = _item_subject_pairs(item)
    if "dimension_assessments" in response_spec:
        raw_assessments = copy.deepcopy(list(response_spec["dimension_assessments"]))
    else:
        step = {
            "raw_scores": dict(response_spec["raw_scores"]),
            "citation_mode": str(response_spec.get("citation_mode", "valid")),
        }
        final_line, _, _ = build_scripted_judge_payload(
            rubric_spec=dict(rubric),
            subject_artifacts=subject_pairs,
            step=step,
        )
        assert final_line is not None
        raw_assessments = json.loads(final_line.removeprefix("ORQ_FINAL:"))["outputs"]["rubric_evidence"]["dimension_assessments"]

    audit = verify_rubric_citations(dict(rubric), raw_assessments, subject_pairs)
    total_citations = sum(len(a.get("citations", [])) for a in raw_assessments)
    if audit.citation_verification_status == "verified":
        valid_citations = total_citations
    else:
        subjects_idx = {
            (
                b["output_id"],
                b["artifact"]["artifact_id"],
                b["artifact"]["content_digest"]["value"],
            ): raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            for b, raw in subject_pairs
        }
        valid_citations = 0
        for assess in raw_assessments:
            for cit in assess.get("citations", []):
                dig = cit.get("content_digest", {})
                text_norm = subjects_idx.get((cit.get("output_id"), cit.get("artifact_id"), dig.get("value")))
                excerpt = str(cit.get("verbatim_excerpt", "")).replace("\r\n", "\n")
                loc = cit.get("locator", {})
                if text_norm is None or not excerpt or excerpt not in text_norm:
                    continue
                if loc.get("kind") == "line_range":
                    lines_norm = text_norm.split("\n")
                    s_line, e_line = loc.get("start_line"), loc.get("end_line")
                    if type(s_line) is int and type(e_line) is int and 1 <= s_line <= e_line <= len(lines_norm):
                        if excerpt in "\n".join(lines_norm[s_line - 1 : e_line]):
                            valid_citations += 1
                elif loc.get("kind") == "json_pointer":
                    from .rubric_evaluator import _resolve_json_pointer
                    try:
                        parsed = json.loads(text_norm)
                    except ValueError:
                        continue
                    if _resolve_json_pointer(parsed, str(loc.get("pointer"))):
                        valid_citations += 1
    outcome = compute_rubric_outcome(
        dict(rubric),
        audit.raw_scores,
        citation_verification_status=audit.citation_verification_status,
    )
    return (
        outcome.derived_status,
        Decimal(outcome.normalized_score),
        audit.raw_scores,
        valid_citations,
        total_citations,
    )


def _calibration_usage_and_consumption(
    *,
    corpus_id: str,
    family_id: str,
    recorded_at: str,
    run_id: str,
) -> tuple[Record, Record]:
    usage = seal_record({
        "schema_version": "0.1",
        "usage_id": stable_id("usage-cal", [corpus_id, family_id, recorded_at]),
        "run_id": run_id,
        "scope_type": "evaluation",
        "scope_ref": f"eval-cal-{family_id}",
        "subscription_id": f"sub-{family_id.removeprefix('family_')}",
        "provider_ref": {"id": f"provider-{family_id}", "version": "1", "content_digest": canonical_digest(family_id)},
        "observed_at": recorded_at,
        "recorded_at": recorded_at,
        "measurements": [],
        "cost_lines": [{
            "status": "unavailable",
            "amount": None,
            "currency": None,
            "pricing_ref": None,
            "method": "personal_subscription_unpriced",
        }],
        "coverage": {"reported": [], "estimated": [], "unavailable": ["cost"]},
        "supersedes_usage_id": None,
    }, "usage_record")
    consumption = seal_record({
        "schema_version": "0.1",
        "quota_consumption_id": stable_id("cons-cal", [corpus_id, family_id, recorded_at]),
        "scope_type": "evaluation",
        "run_id": run_id,
        "task_id": "calibration",
        "attempt_id": None,
        "evaluation_id": f"eval-cal-{family_id}",
        "judge_call_id": stable_id("judge-cal", [corpus_id, family_id, recorded_at]),
        "subscription_id": f"sub-{family_id.removeprefix('family_')}",
        "family_id": family_id,
        "quota_pool_id": f"pool-{family_id.removeprefix('family_')}",
        "usage_record_ref": {"id": usage["usage_id"], "version": "0.1", "content_digest": canonical_digest(usage)},
        "messages_consumed": 1,
        "input_tokens": None,
        "output_tokens": None,
        "credit_multiplier": None,
        "quota_outcome": "accepted",
        "recorded_at": recorded_at,
    }, "quota_consumption_record")
    issues = validate_quota_consumption_record(consumption, usage_record=usage)
    if issues:
        raise ExecutorError(issues[0].code, issues[0].message)
    return usage, consumption


def run_calibration(
    *,
    corpus: Mapping[str, Any],
    rubric: Mapping[str, Any],
    responses: Mapping[str, Mapping[str, Any]] | None = None,
    output_dir: Path | None = None,
    recorded_at: str = "2026-11-18T00:00:00Z",
    prompt_template_ref: Mapping[str, Any] | None = None,
    families: Sequence[str] = CALIBRATION_FAMILIES,
) -> Record:
    """Executa calibração determinística sobre um corpus rotulado e gera CalibrationReport selado."""
    if rubric.get("applicable_scope") != "task_attempt":
        raise ExecutorError("invalid_rubric", "RubricSpec deve declarar applicable_scope='task_attempt'")
    rubric_issues = validate_instance(dict(rubric), "rubric_spec")
    if rubric_issues:
        raise ExecutorError(rubric_issues[0].code, rubric_issues[0].message)
    if responses is None:
        raise ExecutorError("calibration_responses_required", "Modo offline exige responses estruturado")

    items = list(corpus["items"])
    tested_families = sorted(str(f) for f in families)
    if not tested_families:
        raise ExecutorError("no_evaluator_families", "Calibração exige ao menos uma família avaliadora")

    eval_grid: dict[tuple[str, str], tuple[str, Decimal, dict[str, int], int, int]] = {}
    for family_id in tested_families:
        fam_resp = responses.get(family_id, {})
        for item in items:
            item_id = item["item_id"]
            if item_id not in fam_resp:
                raise ExecutorError("missing_calibration_response", f"{family_id} sem resposta para {item_id}")
            eval_grid[(item_id, family_id)] = _evaluate_corpus_entry(
                rubric=rubric,
                item=item,
                response_spec=fam_resp[item_id],
            )

    binary_items = [it for it in items if it["category"] in {"gold_pass", "seeded_semantic_fail"}]
    expected_bin = [it["human_expected_status"] for it in binary_items]
    expected_all = [it["human_expected_status"] for it in items]

    per_family_metrics: list[Record] = []
    failed_gates: list[str] = []
    for family_id in tested_families:
        pred_bin = [eval_grid[(it["item_id"], family_id)][0] for it in binary_items]
        pred_all = [eval_grid[(it["item_id"], family_id)][0] for it in items]
        cfpr = compute_cfpr(pred_bin, expected_bin)
        precision, recall, f1 = compute_precision_recall_f1(pred_bin, expected_bin)
        valid_cits = sum(eval_grid[(it["item_id"], family_id)][3] for it in items)
        total_cits = sum(eval_grid[(it["item_id"], family_id)][4] for it in items)
        valid_rate = _ratio(valid_cits, total_cits)
        kappa = cohen_kappa_binary(pred_all, expected_all)
        per_family_metrics.append({
            "family_id": family_id,
            "cfpr": _q4(cfpr),
            "precision": _q4(precision),
            "recall": _q4(recall),
            "f1": _q4(f1),
            "valid_citation_rate": _q4(valid_rate),
            "cohen_kappa_vs_human": _q4(kappa),
        })
        if cfpr > GATE_CFPR_MAX and "gate_cfpr" not in failed_gates:
            failed_gates.append("gate_cfpr")
        if recall < GATE_RECALL_MIN and "gate_recall" not in failed_gates:
            failed_gates.append("gate_recall")
        if valid_rate < GATE_VALID_CITATIONS_MIN and "gate_valid_citations" not in failed_gates:
            failed_gates.append("gate_valid_citations")
        if kappa < GATE_KAPPA_MIN and "gate_cohen_kappa" not in failed_gates:
            failed_gates.append("gate_cohen_kappa")

    global_score_matrix = [
        [eval_grid[(it["item_id"], fam)][1] for fam in tested_families]
        for it in items
    ]
    alpha_global = krippendorff_alpha_ordinal(global_score_matrix)
    if alpha_global < GATE_ALPHA_GLOBAL_MIN:
        failed_gates.append("gate_krippendorff_global")

    alpha_by_dim: list[Record] = []
    for dim in rubric["dimensions"]:
        dim_id = dim["dimension_id"]
        dim_matrix = [
            [eval_grid[(it["item_id"], fam)][2][dim_id] for fam in tested_families]
            for it in items
        ]
        dim_alpha = krippendorff_alpha_ordinal(dim_matrix)
        alpha_by_dim.append({"dimension_id": dim_id, "alpha": _q4(dim_alpha)})
        if dim_alpha < GATE_ALPHA_DIM_MIN:
            failed_gates.append(f"gate_krippendorff_dimension:{dim_id}")

    kendall_pairs: list[Record] = []
    for idx_a in range(len(tested_families)):
        for idx_b in range(idx_a + 1, len(tested_families)):
            fa, fb = tested_families[idx_a], tested_families[idx_b]
            sa = [eval_grid[(it["item_id"], fa)][1] for it in items]
            sb = [eval_grid[(it["item_id"], fb)][1] for it in items]
            kendall_pairs.append({"family_a": fa, "family_b": fb, "tau_b": _q4(kendall_tau_b(sa, sb))})

    mad_inter = compute_mad_inter(global_score_matrix)
    if mad_inter > GATE_MAD_INTER_MAX:
        failed_gates.append("gate_mad_inter")

    author_families = sorted({str(it["author_family_id"]) for it in items})
    matrix_cells: list[Record] = []
    for auth in author_families:
        auth_items = [it for it in items if it["author_family_id"] == auth]
        for ev_fam in tested_families:
            vals = [eval_grid[(it["item_id"], ev_fam)][1] for it in auth_items]
            mean_val = (sum(vals, Decimal(0)) / Decimal(len(vals))).quantize(QUANT4, rounding=ROUND_HALF_UP) if vals else Decimal("0.0000")
            matrix_cells.append({
                "author_family_id": auth,
                "evaluator_family_id": ev_fam,
                "mean_normalized_score": _q4(mean_val),
                "sample_count": len(vals),
            })

    by_family_self: list[Record] = []
    disqualified: list[str] = []
    for fam in tested_families:
        fam_items = [it for it in items if it["author_family_id"] == fam]
        others = [g for g in tested_families if g != fam]
        if fam_items and others:
            same_scores = [eval_grid[(it["item_id"], fam)][1] for it in fam_items]
            cross_means = [
                sum((eval_grid[(it["item_id"], g)][1] for g in others), Decimal(0)) / Decimal(len(others))
                for it in fam_items
            ]
            delta = (sum(s - c for s, c in zip(same_scores, cross_means, strict=True)) / Decimal(len(fam_items))).quantize(
                QUANT4, rounding=ROUND_HALF_UP
            )
            w_res = wilcoxon_signed_rank_exact([float(s) for s in same_scores], [float(c) for c in cross_means])
            w_plus = Decimal(str(w_res["w_plus"])).quantize(QUANT4, rounding=ROUND_HALF_UP)
            p_val = Decimal(str(w_res["p_value"])).quantize(QUANT4, rounding=ROUND_HALF_UP)
            is_disq = delta > GATE_SELF_DELTA_MAX and p_val < GATE_SELF_P_MAX
        else:
            delta, w_plus, p_val, is_disq = Decimal("0.0000"), Decimal("0.0000"), Decimal("1.0000"), False
        if is_disq:
            disqualified.append(fam)
        by_family_self.append({
            "family_id": fam,
            "delta_self": _q4(delta),
            "wilcoxon_w_plus": _q4(w_plus),
            "wilcoxon_p_value": _q4(p_val),
            "n_paired": len(fam_items),
            "disqualified": is_disq,
        })

    usage_refs: list[Record] = []
    cons_refs: list[Record] = []
    usages_out: list[Record] = []
    cons_out: list[Record] = []
    corpus_digest = canonical_digest(corpus)
    prompt_ref = copy.deepcopy(dict(prompt_template_ref)) if prompt_template_ref else default_prompt_template_ref()
    for fam in tested_families:
        u_rec, c_rec = _calibration_usage_and_consumption(
            corpus_id=corpus["corpus_id"],
            family_id=fam,
            recorded_at=recorded_at,
            run_id=f"cal-{corpus['corpus_id']}",
        )
        usages_out.append(u_rec)
        cons_out.append(c_rec)
        usage_refs.append({"id": u_rec["usage_id"], "version": "0.1", "content_digest": canonical_digest(u_rec)})
        cons_refs.append({"id": c_rec["quota_consumption_id"], "version": "0.1", "content_digest": canonical_digest(c_rec)})

    band = rubric["inconclusive_band"] or {"lower": rubric["pass_threshold"], "upper": rubric["pass_threshold"]}
    report = seal_record({
        "schema_version": "0.1",
        "calibration_report_id": f"cal-{corpus['corpus_id']}-{rubric['rubric_id']}",
        "corpus_id": corpus["corpus_id"],
        "corpus_digest": corpus_digest,
        "rubric_ref": {"id": rubric["rubric_id"], "version": rubric["version"], "content_digest": copy.deepcopy(rubric["record_hash"])},
        "prompt_template_ref": prompt_ref,
        "evaluator_families_tested": tested_families,
        "sample_counts": {
            "total": len(items),
            "gold_pass": sum(1 for it in items if it["category"] == "gold_pass"),
            "seeded_semantic_fail": sum(1 for it in items if it["category"] == "seeded_semantic_fail"),
            "borderline_cases": sum(1 for it in items if it["category"] == "borderline_case"),
        },
        "per_family_metrics": per_family_metrics,
        "inter_evaluator_metrics": {
            "krippendorff_alpha_ordinal_global": _q4(alpha_global),
            "krippendorff_alpha_by_dimension": alpha_by_dim,
            "kendall_tau_b_pairs": kendall_pairs,
            "mad_inter": _q4(mad_inter),
        },
        "self_preference_metrics": {
            "matrix": matrix_cells,
            "by_family": by_family_self,
        },
        "calibrated_from_pilot": bool(corpus.get("calibrated_from_pilot", False)),
        "curation_status": str(corpus.get("curation_status", "draft_pending_owner_review")),
        "recommended_thresholds": {
            "pass_threshold": rubric["pass_threshold"],
            "inconclusive_band": {"lower": band["lower"], "upper": band["upper"]},
            "disqualified_evaluator_families": sorted(disqualified),
        },
        "gates_passed": len(failed_gates) == 0,
        "failed_gates": failed_gates,
        "usage_record_refs": usage_refs,
        "quota_consumption_refs": cons_refs,
        "recorded_at": recorded_at,
    }, "calibration_report")

    if output_dir is not None:
        out = Path(output_dir)
        ensure_directory(out)
        publish_bytes(confined_path(out, "calibration-report.json"), canonicalize(report))
        publish_bytes(confined_path(out, "usage-records.json"), canonicalize(usages_out))
        publish_bytes(confined_path(out, "quota-consumptions.json"), canonicalize(cons_out))
    return report


def _replay_closed_run_readonly(store_root: Path, run_id: str) -> tuple[Path, dict[str, Any], str]:
    """Lê e verifica a cadeia SHA-256 do journal em memória sem abrir lock nem escrever em run_dir."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,127}", run_id):
        raise ExecutorError("invalid_run_id", f"run_id inválido: {run_id!r}")
    run_dir = confined_path(Path(store_root).resolve(), "runs", run_id)
    journal_path = confined_path(run_dir, "journal.jsonl")
    if not journal_path.is_file():
        raise ExecutorError("journal_missing", f"Journal ausente em {journal_path}")
    lines = [line for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    prev_hash: dict[str, str] | None = None
    cutoff_hash = ""
    run_state = "created"
    closed = False
    intent: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    routing_by_id: dict[str, dict[str, Any]] = {}
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    attempt_headers: dict[str, dict[str, Any]] = {}
    attempt_intents: dict[str, dict[str, Any]] = {}
    eval_reports: list[dict[str, Any]] = []

    for seq, line in enumerate(lines, 1):
        event = json.loads(line)
        if event.get("global_sequence") != seq or event.get("event_id") != f"event-{seq}":
            raise ExecutorError("journal_integrity_error", f"Sequência inválida em event-{seq}")
        if event.get("previous_event_hash") != prev_hash:
            raise ExecutorError("journal_integrity_error", f"previous_event_hash diverge em event-{seq}")
        expected_hash = canonical_digest({k: v for k, v in event.items() if k != "event_hash"})
        if event.get("event_hash") != expected_hash:
            raise ExecutorError("journal_integrity_error", f"event_hash diverge em event-{seq}")
        prev_hash = expected_hash
        cutoff_hash = expected_hash["value"]

        etype = event["event_type"]
        agg_type = event["aggregate_type"]
        agg_id = event["aggregate_id"]
        payload = event["payload"]
        if etype == "run_intent_recorded":
            intent = payload
        elif etype == "run_manifest_closed":
            manifest = payload
        elif etype == "state_transition" and agg_type == "run" and agg_id == run_id:
            run_state = payload["to"]
        elif etype == "run_closed":
            closed = True
        elif etype == "routing_decision_recorded":
            routing_by_id[payload["routing_decision_id"]] = payload
        elif etype == "artifact_ingested":
            artifacts_by_id[payload["artifact_id"]] = payload
        elif etype == "dispatch_requested" and agg_type == "attempt":
            attempt_headers[agg_id] = payload["attempt_header"]
            attempt_intents[agg_id] = payload["dispatch_intent"]
        elif etype == "evaluation_report_recorded":
            eval_reports.append(payload)

    if not closed or run_state not in {"succeeded", "failed", "cancelled", "timed_out", "indeterminate"}:
        raise ExecutorError("run_not_closed", f"Auditoria pós-hoc exige run encerrado; estado atual={run_state}")
    state_view = {
        "intent": intent,
        "manifest": manifest,
        "routing_by_id": routing_by_id,
        "artifacts_by_id": artifacts_by_id,
        "attempt_headers": attempt_headers,
        "attempt_intents": attempt_intents,
        "eval_reports": eval_reports,
    }
    return run_dir, state_view, cutoff_hash


def _read_store_artifact_readonly(run_dir: Path, locator: str, expected_digest: Mapping[str, Any]) -> bytes:
    match = _LOCATOR_RE.fullmatch(locator)
    if not match:
        raise ExecutorError("invalid_artifact_locator", f"Locator inválido: {locator}")
    digest_hex = match.group(1)
    if digest_hex != expected_digest.get("value"):
        raise ExecutorError("integrity_mismatch", "Digest do locator diverge do descriptor")
    blob = confined_path(run_dir, "artifacts", "sha256", digest_hex)
    content = blob.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != digest_hex:
        raise ExecutorError("integrity_mismatch", f"Bytes corrompidos para {locator}")
    return content


def run_post_hoc_rubric_audit(
    *,
    store_root: Path,
    run_id: str,
    rubric: Mapping[str, Any],
    calibration_report: Mapping[str, Any],
    output_dir: Path,
    task_id: str | None = None,
    simulate_script: Mapping[str, Any] | None = None,
    recorded_at: str = "2026-11-18T00:00:00Z",
    prompt_template_ref: Mapping[str, Any] | None = None,
    evaluator_routing_policy: Mapping[str, Any] | None = None,
    executor_fn: Callable[[Record, bytes, IsolatedEvalWorkspace], JudgeRawOutcome] | None = None,
) -> Record:
    """Executa auditoria pós-hoc opcional por model_rubric em um run já encerrado sem mutá-lo."""
    rubric_issues = validate_instance(dict(rubric), "rubric_spec")
    if rubric_issues:
        raise ExecutorError(rubric_issues[0].code, rubric_issues[0].message)
    cal_issues = validate_instance(dict(calibration_report), "calibration_report")
    if cal_issues:
        raise ExecutorError(cal_issues[0].code, cal_issues[0].message)

    run_dir, state_view, cutoff_value = _replay_closed_run_readonly(store_root, run_id)
    chosen_report = None
    for rep in state_view["eval_reports"]:
        if rep["scope"]["kind"] != "task_attempt":
            continue
        if task_id is not None and rep["scope"]["task_id"] != task_id:
            continue
        if rep["verdict"] != "pass":
            continue
        if any(c["method"] == "model_rubric" for c in rep["criteria_snapshot"]["criteria"]):
            continue
        chosen_report = rep
        break
    if chosen_report is None:
        raise ExecutorError("audit_target_not_found", "Nenhuma avaliação task_attempt determinística aprovada elegível")

    rep = chosen_report
    src_task_id = rep["scope"]["task_id"]
    src_attempt_id = rep["scope"]["attempt_id"]
    src_eval_id = rep["evaluation_id"]
    attempt_header = state_view["attempt_headers"][src_attempt_id]
    attempt_intent = state_view["attempt_intents"][src_attempt_id]
    routing_id = (attempt_intent.get("routing_decision_ref") or {}).get("id")
    routing = state_view["routing_by_id"].get(routing_id) if routing_id else None
    target_id = attempt_intent["target_ref"]["id"]
    manifest = state_view["manifest"] or {}
    targets_list = list((manifest.get("target_catalog_snapshot") or {}).get("targets", []))
    targets_by_id = {t["target_id"]: t for t in targets_list}
    target = targets_by_id.get(target_id)
    if target is None and routing and routing.get("effective_selection"):
        target = targets_by_id.get(routing["effective_selection"]["selected_target_id"])
    if target is None:
        target = attempt_header.get("execution_target_snapshot")
    author_family_id = (target or {}).get("family_id") or "family_simulated"
    author_vendor = model_vendor_for_target(target) if target else "simulated"
    if not author_vendor or author_vendor == "unknown":
        author_vendor = "simulated"

    subject_pairs: list[tuple[Record, bytes]] = []
    subject_desc: dict[str, Record] = {}
    for binding in rep["subject_bindings"]:
        art = binding["artifact"]
        art_id = art["artifact_id"]
        desc = state_view["artifacts_by_id"][art_id]
        content = _read_store_artifact_readonly(run_dir, desc["locator"], art["content_digest"])
        subject_pairs.append((copy.deepcopy(binding), content))
        subject_desc[art_id] = art

    disq = set(calibration_report["recommended_thresholds"]["disqualified_evaluator_families"])
    from .composite_evaluator import default_evaluator_routing_policy
    eval_policy = copy.deepcopy(dict(evaluator_routing_policy or default_evaluator_routing_policy()))
    catalog_snap = manifest.get("target_catalog_snapshot") or {"targets": targets_list}
    selection = select_evaluator_targets(
        catalog_snap,
        eval_policy,
        author_family_ids=[author_family_id],
        author_model_vendor=author_vendor,
        panel_size_override=1,
        disqualified_families=disq,
    )
    if not selection.selected_targets:
        fallback_fam = next(
            f for f in ("family_gemini", "family_kiro", "family_claude", "family_codex")
            if f != author_family_id and f not in disq
        )
        eval_target = {
            "target_id": f"audit-target-{fallback_fam}",
            "version": "1",
            "family_id": fallback_fam,
            "subscription_id": f"sub-{fallback_fam.removeprefix('family_')}",
            "quota_pool_id": f"pool-{fallback_fam.removeprefix('family_')}",
            "model_ref": {"id": f"{fallback_fam}-audit-model", "version": "1", "content_digest": canonical_digest(fallback_fam)},
            "provider_ref": {"id": f"provider-{fallback_fam}", "version": "1", "content_digest": canonical_digest(fallback_fam)},
            "runtime_ref": {"id": f"runtime-{fallback_fam}", "version": "1", "content_digest": canonical_digest(fallback_fam)},
        }
    else:
        eval_target = selection.selected_targets[0]

    eval_family_id = eval_target["family_id"]
    cli_map = {"family_claude": "claude", "family_codex": "codex", "family_gemini": "agy", "family_kiro": "kiro-cli"}
    profile = {
        "schema_version": "0.1",
        "subscription_id": eval_target["subscription_id"],
        "family_id": eval_family_id,
        "quota_pool_id": eval_target["quota_pool_id"],
        "cli_executable": cli_map.get(eval_family_id, "codex"),
        "local_timezone": "UTC",
        "default_cooldown_ms": 60000,
    }
    prompt_ref = copy.deepcopy(dict(prompt_template_ref)) if prompt_template_ref else default_prompt_template_ref()
    audit_eval_id = stable_id("audit-eval", [run_id, src_task_id, src_attempt_id, src_eval_id])
    judge_call_id = f"judge-{audit_eval_id}-1"
    rendered = render_evaluation_prompt(
        run_id=run_id,
        evaluation_id=audit_eval_id,
        criterion_id="post-hoc-rubric-audit",
        judge_call_id=judge_call_id,
        rubric_spec=dict(rubric),
        subject_artifacts=subject_pairs,
        evaluator_role_id="code_reviewer",
        prompt_template_ref=prompt_ref,
        normative_context=None,
    )
    intent = {
        "run_id": run_id,
        "task_id": src_task_id,
        "attempt_id": src_attempt_id,
        "evaluation_id": audit_eval_id,
        "judge_call_id": judge_call_id,
        "criterion_id": "post-hoc-rubric-audit",
        "dispatch_idempotency_key": stable_id("audit-dispatch", [run_id, src_eval_id, recorded_at]),
        "evaluator_family_id": eval_family_id,
        "evaluator_model_vendor": model_vendor_for_target(eval_target) or "simulated",
        "rendered_prompt_digest": rendered.rendered_prompt_digest,
        "rubric_ref": {"id": rubric["rubric_id"], "version": rubric["version"], "content_digest": copy.deepcopy(rubric["record_hash"])},
        "requested_at": recorded_at,
    }

    out = Path(output_dir).resolve()
    ensure_directory(out)
    tmp_parent = out / "_eval_tmp"
    ensure_directory(tmp_parent)
    ws = IsolatedEvalWorkspace.create(
        workspace_id=judge_call_id,
        rubric_spec=dict(rubric),
        subject_artifacts=subject_pairs,
        eval_temp_root=tmp_parent,
        store_root=run_dir.parent.parent,
        normative_context=None,
    )
    try:
        if executor_fn is not None:
            raw_outcome = executor_fn(intent, rendered.prompt_bytes, ws)
        else:
            step = copy.deepcopy(dict(simulate_script)) if simulate_script else {
                "raw_scores": {d["dimension_id"]: int(rubric["scale"]["max_score"]) for d in rubric["dimensions"]}
            }
            final_line, stdout, tool_events = build_scripted_judge_payload(
                rubric_spec=dict(rubric),
                subject_artifacts=subject_pairs,
                step=step,
            )
            raw_outcome = JudgeRawOutcome(
                judge_call_id=judge_call_id,
                execution_status="finished",
                termination_certainty="confirmed",
                observed_at=recorded_at,
                stdout_text=stdout,
                stderr_text="",
                final_text=final_line,
                diagnostics=(),
                tool_events=tool_events,
                requested_model_id=eval_target["model_ref"]["id"],
                resolved_model_id=eval_target["model_ref"]["id"],
                provider_context_coverage="complete",
                stop_reason="finished",
            )
        rubric_eval = RubricModelEvaluator(clock=lambda: recorded_at)
        outcome = rubric_eval.collect_and_verify(
            dispatch_intent=intent,
            raw_outcome=raw_outcome,
            eval_workspace=ws,
            rubric_spec=dict(rubric),
            prompt_template_ref=prompt_ref,
            rendered_prompt_digest=rendered.rendered_prompt_digest,
            subject_artifacts=subject_pairs,
            author_family_ids=[author_family_id],
            author_model_vendor=author_vendor,
            evaluator_target=eval_target,
            profile=profile,
            runtime_ref=eval_target.get("runtime_ref", {"id": profile["cli_executable"], "version": "1", "content_digest": canonical_digest(profile["cli_executable"])}),
            runtime_version="1",
            store_root=run_dir.parent.parent,
        )
    finally:
        ws.cleanup()
        if tmp_parent.exists() and not any(tmp_parent.iterdir()):
            tmp_parent.rmdir()

    if outcome.evidence is None:
        code = outcome.errors[0]["code"] if outcome.errors else "post_hoc_audit_failed"
        raise ExecutorError(code, "Auditoria pós-hoc não produziu evidência válida de rubrica")

    evidence = outcome.evidence
    ev_bytes = canonicalize(evidence)
    ev_digest = bytes_digest(ev_bytes)
    publish_bytes(confined_path(out, f"{evidence['evidence_id']}.json"), ev_bytes)
    publish_bytes(confined_path(out, f"{outcome.usage_record['usage_id']}.json"), canonicalize(outcome.usage_record))
    publish_bytes(confined_path(out, f"{outcome.quota_consumption['quota_consumption_id']}.json"), canonicalize(outcome.quota_consumption))

    audit_rec = seal_record({
        "schema_version": "0.1",
        "audit_id": stable_id("audit", [run_id, src_task_id, src_eval_id, recorded_at]),
        "source_run_id": run_id,
        "source_task_id": src_task_id,
        "source_attempt_id": src_attempt_id,
        "source_evaluation_id": src_eval_id,
        "source_run_cutoff_event_hash": {"algorithm": "sha256", "value": cutoff_value},
        "source_deterministic_verdict": "pass",
        "subject_bindings": copy.deepcopy(rep["subject_bindings"]),
        "author_family_id": author_family_id,
        "author_model_vendor": author_vendor,
        "rubric_ref": {"id": rubric["rubric_id"], "version": rubric["version"], "content_digest": copy.deepcopy(rubric["record_hash"])},
        "calibration_report_ref": {
            "id": calibration_report["calibration_report_id"],
            "version": calibration_report["schema_version"],
            "content_digest": copy.deepcopy(calibration_report["record_hash"]),
        },
        "rubric_evidence_refs": [{"artifact_id": evidence["evidence_id"], "content_digest": ev_digest}],
        "audit_derived_status": outcome.criterion_status if outcome.criterion_status in {"pass", "fail", "inconclusive"} else "inconclusive",
        "audit_normalized_score": evidence["normalized_score"],
        "usage_record_refs": [{"id": outcome.usage_record["usage_id"], "version": "0.1", "content_digest": canonical_digest(outcome.usage_record)}],
        "quota_consumption_refs": [{"id": outcome.quota_consumption["quota_consumption_id"], "version": "0.1", "content_digest": canonical_digest(outcome.quota_consumption)}],
        "recorded_at": recorded_at,
    }, "post_hoc_rubric_audit_record")
    publish_bytes(confined_path(out, "post-hoc-rubric-audit.json"), canonicalize(audit_rec))
    return audit_rec
