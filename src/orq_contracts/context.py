"""Materialização determinística do contexto observável pelo ORQ."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_digest, canonicalize
from .selection import materialize_unit
from .validator import validate_context_manifest, validate_instance


@dataclass(frozen=True, slots=True)
class ContextError(Exception):
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


@dataclass(frozen=True, slots=True)
class MaterializedContext:
    manifest: dict[str, Any]
    bundle: bytes
    item_contents: dict[str, bytes]


def _fail(code: str, path: str, message: str) -> None:
    raise ContextError(code, path, message)


def _verify_embedded_digest(record: dict[str, Any], field: str, path: str) -> None:
    unsigned = {key: value for key, value in record.items() if key != field}
    if canonical_digest(unsigned) != record[field]:
        _fail("record_digest_mismatch", path, f"{field} não corresponde ao registro")


def _key(unit_ref: dict[str, Any]) -> tuple[str, str, str]:
    return unit_ref["source_id"], unit_ref["revision"], unit_ref["unit_id"]


def _token_count(unit: dict[str, Any]) -> int:
    matches = [item for item in unit["token_estimates"] if item.get("tokenizer_ref") == "char4-v1"]
    if not matches:
        _fail("missing_token_estimate", "/units/token_estimates", "char4-v1 ausente")
    return int(matches[0]["count"])


def materialize_knowledge_context(
    vault_root: Path,
    catalog: dict[str, Any],
    selection: dict[str, Any],
    *,
    routing_decision_ref: dict[str, Any],
    target_ref: dict[str, Any],
    target_context_limit_tokens: int,
    reserved_output_tokens: int,
    margin_tokens: int = 0,
    provider_context_coverage: str = "platform_only",
) -> MaterializedContext:
    """Materializa unidades selecionadas e identifica os bytes ordenados do bundle."""

    for record, schema_name, digest_field, path in (
        (catalog, "knowledge_catalog", "catalog_hash", "/catalog/catalog_hash"),
        (selection, "knowledge_selection", "selection_hash", "/selection/selection_hash"),
    ):
        issues = validate_instance(record, schema_name)
        if issues:
            issue = issues[0]
            _fail(issue.code, issue.path, issue.message)
        _verify_embedded_digest(record, digest_field, path)

    expected_catalog_ref = {
        "id": catalog["catalog_id"],
        "version": catalog["version"],
        "content_digest": catalog["catalog_hash"],
    }
    if selection["catalog_snapshot_ref"] != expected_catalog_ref:
        _fail("catalog_reference_mismatch", "/selection/catalog_snapshot_ref", "snapshot divergente")
    if selection["status"] not in {"satisfied", "partial"}:
        _fail("knowledge_selection_not_admissible", "/selection/status", selection["status"])
    if target_context_limit_tokens < 1 or reserved_output_tokens < 0 or margin_tokens < 0:
        _fail("invalid_context_budget", "/budget", "limites devem ser não negativos e o contexto positivo")
    available_tokens = target_context_limit_tokens - reserved_output_tokens
    if available_tokens < 0:
        _fail("invalid_context_budget", "/budget", "reserva de saída excede o contexto do destino")
    if provider_context_coverage not in {"complete", "platform_only", "unknown"}:
        _fail("invalid_provider_context_coverage", "/provider_context_coverage", provider_context_coverage)

    units: dict[tuple[str, str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for entry in catalog["source_entries"]:
        source = entry["source_metadata"]
        for unit in entry["units"]:
            units[(source["source_id"], source["revision"], unit["unit_id"])] = (source, unit)

    requirement_required = {
        item["knowledge_requirement_id"]: item["required"]
        for item in selection["requirement_snapshots"]
    }
    candidate_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in selection["candidates"]:
        candidate_key = _key(candidate["unit_ref"])
        if candidate_key in candidate_by_key:
            _fail("duplicate_candidate_unit", "/selection/candidates", str(candidate_key))
        candidate_by_key[candidate_key] = candidate
    manifest_id = (
        f"context-{selection['run_id']}-{selection['task_id']}-{selection['candidate_attempt_number']}"
    )
    items = []
    bundle_items = []
    item_contents: dict[str, bytes] = {}
    selected_keys: set[tuple[str, str, str]] = set()
    total_tokens = 0

    for sequence, selected in enumerate(selection["selected_units"], start=1):
        unit_ref = selected["unit_ref"]
        unit_key = _key(unit_ref)
        if unit_key in selected_keys:
            _fail("duplicate_selected_unit", "/selection/selected_units", str(unit_key))
        selected_keys.add(unit_key)
        candidate = candidate_by_key.get(unit_key)
        if candidate is None or not candidate["eligible"]:
            _fail("selected_unit_not_eligible", "/selection/selected_units", str(unit_key))
        unknown_requirements = [
            identifier for identifier in selected["requirement_ids"] if identifier not in requirement_required
        ]
        if unknown_requirements:
            _fail(
                "selected_unit_unknown_requirement",
                "/selection/selected_units",
                ", ".join(unknown_requirements),
            )
        eligible_requirements = {
            result["knowledge_requirement_id"]
            for result in candidate["requirement_results"]
            if result["eligible"]
        }
        if not set(selected["requirement_ids"]).issubset(eligible_requirements):
            _fail(
                "selected_unit_requirement_ineligible",
                "/selection/selected_units",
                str(unit_key),
            )
        resolved = units.get(unit_key)
        if resolved is None:
            _fail("selected_unit_missing", "/selection/selected_units", str(unit_key))
        source, unit = resolved
        if unit_ref["source_digest"] != source["content_digest"] or unit_ref["unit_digest"] != unit["content_digest"]:
            _fail("selected_unit_reference_mismatch", "/selection/selected_units", str(unit_key))
        content = materialize_unit(vault_root, source, unit).encode("utf-8")
        token_count = _token_count(unit)
        total_tokens += token_count
        item_id = f"context-item-{sequence}"
        required = any(requirement_required[identifier] for identifier in selected["requirement_ids"])
        artifact_ref = {
            "artifact_id": "artifact-" + hashlib.sha256(
                f"{manifest_id}\0{item_id}\0{unit['content_digest']['value']}".encode("utf-8")
            ).hexdigest()[:20],
            "content_digest": unit["content_digest"],
        }
        item = {
            "sequence": sequence,
            "item_id": item_id,
            "category": (
                "operational_instructions"
                if source["knowledge_kind"] == "operational"
                else "project_knowledge"
            ),
            "source_ref": source["source_id"],
            "source_revision": source["revision"],
            "source_hash": source["content_digest"],
            "materialized_artifact_ref": artifact_ref,
            "content_hash": unit["content_digest"],
            "required": required,
            "selection_reason": selected["selection_reason"],
            "size": {
                "bytes": len(content),
                "tokens": token_count,
                "tokenizer_ref": "char4-v1",
                "status": "estimated",
            },
            "data_classification": source["data_classification"],
            "knowledge_provenance": {
                "selection_id": selection["selection_id"],
                "requirement_ids": selected["requirement_ids"],
                "unit_ref": unit_ref,
            },
            "transformations": [],
        }
        items.append(item)
        bundle_items.append(
            {
                "sequence": sequence,
                "item_id": item_id,
                "category": item["category"],
                "content": content.decode("utf-8"),
            }
        )
        item_contents[item_id] = content

    if total_tokens + margin_tokens > available_tokens:
        _fail(
            "context_budget_exceeded",
            "/budget/materialized_input_tokens",
            f"{total_tokens} + margem {margin_tokens} > {available_tokens}",
        )

    omitted_items = []
    for candidate in selection["candidates"]:
        if _key(candidate["unit_ref"]) in selected_keys:
            continue
        reasons = candidate["reason_codes"]
        if not reasons:
            per_requirement_scores = [
                result["score"] for result in candidate["requirement_results"] if result["score"] is not None
            ]
            reasons = ["lexical_no_match"] if per_requirement_scores and max(per_requirement_scores) == 0 else ["not_selected"]
        omitted_items.append({"unit_ref": candidate["unit_ref"], "reason_codes": sorted(set(reasons))})

    bundle_object = {
        "schema_version": "0.1",
        "context_manifest_id": manifest_id,
        "items": bundle_items,
    }
    bundle = canonicalize(bundle_object)
    bundle_hash = {"algorithm": "sha256", "value": hashlib.sha256(bundle).hexdigest()}
    manifest = {
        "schema_version": "0.1",
        "context_manifest_id": manifest_id,
        "run_id": selection["run_id"],
        "workflow_ref": selection["workflow_ref"],
        "task_id": selection["task_id"],
        "candidate_attempt_number": selection["candidate_attempt_number"],
        "routing_decision_ref": routing_decision_ref,
        "knowledge_selection_refs": [
            {
                "id": selection["selection_id"],
                "version": str(selection["candidate_attempt_number"]),
                "content_digest": selection["selection_hash"],
            }
        ],
        "target_ref": target_ref,
        "created_at": selection["created_at"],
        "items": items,
        "omitted_items": omitted_items,
        "budget": {
            "target_context_limit_tokens": target_context_limit_tokens,
            "reserved_output_tokens": reserved_output_tokens,
            "available_input_tokens": available_tokens,
            "materialized_input_tokens": total_tokens,
            "tokenizer_ref": "char4-v1",
            "margin_tokens": margin_tokens,
            "status": "estimated",
        },
        "bundle_hash": bundle_hash,
        "provider_context_coverage": provider_context_coverage,
    }
    issues = validate_context_manifest(manifest)
    if issues:
        issue = issues[0]
        _fail(issue.code, issue.path, issue.message)
    return MaterializedContext(manifest=manifest, bundle=bundle, item_contents=item_contents)
