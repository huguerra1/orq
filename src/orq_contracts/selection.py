"""Filtros determinísticos e ranking lexical do Knowledge Router local."""

from __future__ import annotations

import fnmatch
import hashlib
import math
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .canonical import canonical_digest
from .validator import validate_instance
from .vault import VaultError, _safe_path


TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
CLASSIFICATION_LEVEL = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _tokens(text: str) -> list[str]:
    return [match.group(0).casefold() for match in TOKEN_PATTERN.finditer(text)]


def _unit_ref(source: dict[str, Any], unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source["source_id"],
        "revision": source["revision"],
        "unit_id": unit["unit_id"],
        "source_digest": source["content_digest"],
        "unit_digest": unit["content_digest"],
    }


def materialize_unit(vault_root: Path, source: dict[str, Any], unit: dict[str, Any]) -> str:
    """Lê exatamente o span da unidade e reconfirma seu digest."""

    path = _safe_path(vault_root, source["content_locator"]["path"])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != source["content_digest"]["value"]:
        raise VaultError("integrity_mismatch", "/content_digest", "digest da fonte diverge")
    start = unit["source_span"]["start_byte"]
    end = unit["source_span"]["end_byte"]
    if not (0 <= start <= end <= len(raw)):
        raise VaultError("invalid_unit_span", "/source_span", "span fora da fonte")
    content = raw[start:end]
    if hashlib.sha256(content).hexdigest() != unit["content_digest"]["value"]:
        raise VaultError("unit_integrity_mismatch", "/content_digest", "digest da unidade diverge")
    return content.decode("utf-8", errors="strict")


def _bm25(documents: list[list[str]], query: list[str], *, k1: float = 1.2, b: float = 0.75) -> list[float]:
    if not documents:
        return []
    query_terms = set(query)
    frequencies = Counter()
    for terms in documents:
        for term in query_terms & set(terms):
            frequencies[term] += 1
    average_length = sum(len(document) for document in documents) / len(documents) or 1.0
    scores = []
    for terms in documents:
        term_counts = Counter(terms)
        score = 0.0
        for term in query_terms:
            frequency = term_counts[term]
            if not frequency:
                continue
            inverse_document_frequency = math.log(
                1 + (len(documents) - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
            )
            denominator = frequency + k1 * (1 - b + b * len(terms) / average_length)
            score += inverse_document_frequency * frequency * (k1 + 1) / denominator
        scores.append(score)
    return scores


def _source_matches_ref(source: dict[str, Any], reference: dict[str, Any]) -> bool:
    return (
        source["source_id"] == reference["id"]
        and source["revision"] == reference["version"]
        and source["content_digest"] == reference["content_digest"]
    )


def _applicable(source: dict[str, Any], filters: dict[str, Any]) -> bool:
    applicability = source["applicability"]
    for filter_key, source_key in (("topics", "topics"), ("roles", "roles"), ("task_types", "task_types")):
        requested = set(filters.get(filter_key, []))
        allowed = set(applicability[source_key])
        if requested and allowed and requested.isdisjoint(allowed):
            return False
    domain = filters.get("domain")
    if domain and applicability["domains"] and domain not in applicability["domains"]:
        return False
    requested_paths = filters.get("paths", [])
    allowed_paths = applicability["paths"]
    if requested_paths and allowed_paths and not any(
        fnmatch.fnmatchcase(path, pattern) for path in requested_paths for pattern in allowed_paths
    ):
        return False
    return True


def _requirement_reasons(
    source: dict[str, Any],
    unit: dict[str, Any],
    requirement: dict[str, Any],
    *,
    project_id: str,
    allowed_collections: set[str],
    max_classification: str,
    as_of: datetime,
) -> list[str]:
    reasons = []
    scope = source["project_scope"]
    if scope["kind"] == "project" and scope["project_id"] != project_id:
        reasons.append("wrong_project")
    elif scope["kind"] == "collection" and scope["collection_id"] not in allowed_collections:
        reasons.append("collection_denied")
    elif scope["kind"] == "global_operational" and source["knowledge_kind"] != "operational":
        reasons.append("invalid_global_scope")
    if source["knowledge_kind"] not in requirement["allowed_kinds"]:
        reasons.append("wrong_knowledge_kind")
    if not set(requirement["required_authority_scopes"]).issubset(source["authority"]["authority_scopes"]):
        reasons.append("insufficient_authority")
    if source["data_classification"] == "unknown" or max_classification == "unknown":
        reasons.append("classification_denied")
    elif CLASSIFICATION_LEVEL[source["data_classification"]] > CLASSIFICATION_LEVEL[max_classification]:
        reasons.append("classification_denied")
    if source["effective_from"] and _timestamp(source["effective_from"]) > as_of:
        reasons.append("not_yet_effective")
    if source["expires_at"] and _timestamp(source["expires_at"]) <= as_of:
        reasons.append("expired")
    freshness = requirement["freshness"]
    if freshness:
        if source["verified_at"] is None:
            reasons.append("unverified")
        elif _timestamp(source["verified_at"]) + timedelta(milliseconds=freshness["max_age_ms"]) < as_of:
            reasons.append("stale")
    if requirement["source_refs"] and not any(
        _source_matches_ref(source, reference) for reference in requirement["source_refs"]
    ):
        reasons.append("wrong_source")
    if not _applicable(source, requirement["applicability_filters"]):
        reasons.append("not_applicable")
    if unit["oversized"]:
        reasons.append("oversized")
    return sorted(set(reasons))


def _estimated_tokens(unit: dict[str, Any]) -> int:
    estimates = [item for item in unit["token_estimates"] if item.get("tokenizer_ref") == "char4-v1"]
    if not estimates:
        raise VaultError("missing_token_estimate", "/token_estimates", "char4-v1 ausente")
    return int(estimates[0]["count"])


def select_knowledge(
    vault_root: Path,
    catalog: dict[str, Any],
    requirements: list[dict[str, Any]],
    *,
    query: str,
    run_id: str,
    workflow_ref: dict[str, Any],
    task_id: str,
    candidate_attempt_number: int,
    project_id: str,
    created_at: str,
    max_tokens: int,
    policy_snapshot_refs: list[dict[str, Any]],
    allowed_collections: set[str] | None = None,
    max_classification: str = "internal",
    margin_tokens: int = 0,
) -> dict[str, Any]:
    """Filtra, ranqueia e seleciona unidades sob orçamento."""

    catalog_issues = validate_instance(catalog, "knowledge_catalog")
    if catalog_issues:
        issue = catalog_issues[0]
        raise VaultError(issue.code, issue.path, issue.message)
    if max_classification not in CLASSIFICATION_LEVEL and max_classification != "unknown":
        raise VaultError("invalid_max_classification", "/max_classification", max_classification)
    as_of = _timestamp(created_at)
    collections = allowed_collections or set()
    effective_query = " ".join([query, *(requirement["subject"] for requirement in requirements)]).strip()
    query_terms = _tokens(effective_query)

    raw_candidates: list[dict[str, Any]] = []
    eligible_documents: list[list[str]] = []
    eligible_indexes: list[int] = []
    for entry in catalog["source_entries"]:
        source = entry["source_metadata"]
        for unit in entry["units"]:
            reasons_by_requirement = {
                requirement["knowledge_requirement_id"]: _requirement_reasons(
                    source,
                    unit,
                    requirement,
                    project_id=project_id,
                    allowed_collections=collections,
                    max_classification=max_classification,
                    as_of=as_of,
                )
                for requirement in requirements
            }
            covered = [identifier for identifier, reasons in reasons_by_requirement.items() if not reasons]
            all_reasons = sorted({reason for reasons in reasons_by_requirement.values() for reason in reasons})
            candidate = {
                "source": source,
                "unit": unit,
                "unit_ref": _unit_ref(source, unit),
                "eligible": bool(covered),
                "reason_codes": [] if covered else all_reasons,
                "covered_requirement_ids": covered,
                "reasons_by_requirement": reasons_by_requirement,
                "score": None,
                "rank": None,
                "requirement_scores": {},
                "requirement_ranks": {},
                "document_tokens": None,
            }
            raw_candidates.append(candidate)
            if covered:
                eligible_indexes.append(len(raw_candidates) - 1)
                candidate["document_tokens"] = _tokens(materialize_unit(vault_root, source, unit))
                eligible_documents.append(candidate["document_tokens"])

    for index, score in zip(eligible_indexes, _bm25(eligible_documents, query_terms), strict=True):
        raw_candidates[index]["score"] = score
    ranked = sorted(
        (raw_candidates[index] for index in eligible_indexes),
        key=lambda item: (
            -item["score"],
            item["source"]["source_id"],
            item["source"]["revision"],
            item["unit"]["ordinal"],
            item["unit"]["unit_id"],
        ),
    )
    for rank, candidate in enumerate(ranked, start=1):
        candidate["rank"] = rank

    ranked_by_requirement: dict[str, list[dict[str, Any]]] = {}
    for requirement in requirements:
        requirement_id = requirement["knowledge_requirement_id"]
        requirement_candidates = [
            candidate for candidate in raw_candidates if requirement_id in candidate["covered_requirement_ids"]
        ]
        requirement_documents = [candidate["document_tokens"] for candidate in requirement_candidates]
        requirement_scores = _bm25(requirement_documents, _tokens(requirement["subject"]))
        for candidate, score in zip(requirement_candidates, requirement_scores, strict=True):
            candidate["requirement_scores"][requirement_id] = score
        requirement_candidates.sort(
            key=lambda item: (
                -item["requirement_scores"][requirement_id],
                item["source"]["source_id"],
                item["source"]["revision"],
                item["unit"]["ordinal"],
                item["unit"]["unit_id"],
            )
        )
        for rank, candidate in enumerate(requirement_candidates, start=1):
            candidate["requirement_ranks"][requirement_id] = rank
        ranked_by_requirement[requirement_id] = requirement_candidates

    selected: list[dict[str, Any]] = []
    selected_by_id: dict[str, dict[str, Any]] = {}
    coverage = []
    used_tokens = 0
    ordered_requirements = sorted(enumerate(requirements), key=lambda pair: (not pair[1]["required"], pair[0]))
    for _, requirement in ordered_requirements:
        requirement_id = requirement["knowledge_requirement_id"]
        options = [
            candidate
            for candidate in ranked_by_requirement[requirement_id]
            if requirement_id in candidate["covered_requirement_ids"]
            and (candidate["requirement_scores"][requirement_id] > 0 or requirement["source_refs"])
        ]
        needed: list[dict[str, Any]] = []
        if requirement["coverage_rule"] == "all_source_refs" and requirement["source_refs"]:
            for reference in requirement["source_refs"]:
                match = next(
                    (candidate for candidate in options if _source_matches_ref(candidate["source"], reference)),
                    None,
                )
                if match:
                    needed.append(match)
        elif options:
            needed.append(options[0])

        selected_for_requirement = []
        budget_failed = False
        requirement_tokens = 0
        requirement_limit = requirement["max_context_tokens"]
        for candidate in needed:
            unit_id = candidate["unit"]["unit_id"]
            tokens = _estimated_tokens(candidate["unit"])
            if requirement_limit is not None and requirement_tokens + tokens > requirement_limit:
                budget_failed = True
                candidate["reason_codes"].append("over_budget")
                continue
            if unit_id in selected_by_id:
                selected_for_requirement.append(unit_id)
                selected_by_id[unit_id]["requirement_ids"].append(requirement_id)
                requirement_tokens += tokens
                continue
            if used_tokens + tokens + margin_tokens > max_tokens:
                budget_failed = True
                candidate["reason_codes"].append("over_budget")
                continue
            item = {
                "unit_ref": candidate["unit_ref"],
                "score": candidate["requirement_scores"][requirement_id],
                "rank": candidate["requirement_ranks"][requirement_id],
                "selection_reason": "required_coverage" if requirement["required"] else "optional_coverage",
                "requirement_ids": [requirement_id],
            }
            selected.append(item)
            selected_by_id[unit_id] = item
            selected_for_requirement.append(unit_id)
            used_tokens += tokens
            requirement_tokens += tokens

        expected_count = (
            len(requirement["source_refs"])
            if requirement["coverage_rule"] == "all_source_refs" and requirement["source_refs"]
            else 1
        )
        satisfied = len(set(selected_for_requirement)) >= expected_count
        coverage.append(
            {
                "knowledge_requirement_id": requirement_id,
                "required": requirement["required"],
                "status": "satisfied" if satisfied else "unsatisfied",
                "unit_ids": list(dict.fromkeys(selected_for_requirement)),
                "gap_reason": None if satisfied else ("over_budget" if budget_failed else "no_eligible_match"),
            }
        )

    required_unsatisfied = any(item["required"] and item["status"] != "satisfied" for item in coverage)
    optional_unsatisfied = any(not item["required"] and item["status"] != "satisfied" for item in coverage)
    status = "unsatisfied" if required_unsatisfied else ("partial" if optional_unsatisfied else "satisfied")
    candidates = [
        {
            "unit_ref": candidate["unit_ref"],
            "eligible": candidate["eligible"],
            "reason_codes": sorted(set(candidate["reason_codes"])),
            "requirement_results": [
                {
                    "knowledge_requirement_id": requirement_id,
                    "eligible": not reasons,
                    "reason_codes": reasons,
                    "score": candidate["requirement_scores"].get(requirement_id),
                    "rank": candidate["requirement_ranks"].get(requirement_id),
                }
                for requirement_id, reasons in candidate["reasons_by_requirement"].items()
            ],
            "score": candidate["score"],
            "rank": candidate["rank"],
        }
        for candidate in raw_candidates
    ]
    selection = {
        "schema_version": "0.1",
        "selection_id": f"selection-{run_id}-{task_id}-{candidate_attempt_number}",
        "run_id": run_id,
        "workflow_ref": workflow_ref,
        "task_id": task_id,
        "candidate_attempt_number": candidate_attempt_number,
        "catalog_snapshot_ref": {
            "id": catalog["catalog_id"],
            "version": catalog["version"],
            "content_digest": catalog["catalog_hash"],
        },
        "requirement_snapshots": requirements,
        "query_input": {"text": effective_query, "query_hash": canonical_digest(effective_query)},
        "policy_snapshot_refs": policy_snapshot_refs,
        "candidates": candidates,
        "selected_units": selected,
        "coverage": coverage,
        "budget": {
            "max_tokens": max_tokens,
            "estimated_tokens": used_tokens,
            "tokenizer_ref": "char4-v1",
            "margin_tokens": margin_tokens,
        },
        "status": status,
        "created_at": created_at,
    }
    selection["selection_hash"] = canonical_digest(selection)
    issues = validate_instance(selection, "knowledge_selection")
    if issues:
        issue = issues[0]
        raise VaultError(issue.code, issue.path, issue.message)
    return selection
