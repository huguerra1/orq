"""Ingestão segura e determinística do Vault Markdown."""

from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from markdown_it import MarkdownIt

from .canonical import canonical_digest
from .validator import validate_instance, validate_knowledge_source


ALLOWED_FRONTMATTER_FIELDS = {"title", "language", "topics", "relationships"}
SENSITIVE_FRONTMATTER_FIELDS = {
    "project_scope",
    "authority",
    "instruction_scope",
    "data_classification",
    "status",
    "owner_ref",
    "verified_at",
    "effective_from",
    "expires_at",
}


@dataclass(frozen=True, slots=True)
class VaultError(Exception):
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


@dataclass(frozen=True, slots=True)
class IngestedSource:
    metadata: dict[str, Any]
    frontmatter: dict[str, Any]
    materialized_artifact_ref: dict[str, Any]
    units: list[dict[str, Any]]


def _safe_path(vault_root: Path, logical_path: str) -> Path:
    pure = PurePosixPath(logical_path)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise VaultError("invalid_locator", "/content_locator/path", "caminho relativo inválido")
    root = vault_root.resolve(strict=True)
    candidate = (root / Path(*pure.parts)).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise VaultError("locator_escape", "/content_locator/path", "caminho escapa da raiz do Vault") from error
    if not candidate.is_file():
        raise VaultError("not_regular_file", "/content_locator/path", "fonte não é arquivo regular")
    return candidate


def _yaml_node_count(value: Any, limit: int, count: list[int] | None = None) -> int:
    state = count if count is not None else [0]
    state[0] += 1
    if state[0] > limit:
        raise VaultError("frontmatter_too_complex", "/frontmatter", "estrutura excede o limite de nós")
    if isinstance(value, dict):
        for key, child in value.items():
            _yaml_node_count(key, limit, state)
            _yaml_node_count(child, limit, state)
    elif isinstance(value, list):
        for child in value:
            _yaml_node_count(child, limit, state)
    return state[0]


def _frontmatter(
    text: str,
    *,
    max_bytes: int,
    max_nodes: int,
) -> tuple[dict[str, Any], int]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, 0

    closing_index = None
    accumulated = len(lines[0].encode("utf-8"))
    for index in range(1, len(lines)):
        accumulated += len(lines[index].encode("utf-8"))
        if accumulated > max_bytes:
            raise VaultError("frontmatter_too_large", "/frontmatter", "frontmatter excede limite")
        if lines[index].rstrip("\r\n") == "---":
            closing_index = index
            break
    if closing_index is None:
        raise VaultError("frontmatter_unclosed", "/frontmatter", "delimitador final ausente")

    raw = "".join(lines[1:closing_index])
    try:
        loaded = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as error:
        raise VaultError("invalid_frontmatter_yaml", "/frontmatter", str(error)) from error
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise VaultError("invalid_frontmatter_type", "/frontmatter", "frontmatter deve ser objeto textual")
    _yaml_node_count(loaded, max_nodes)

    sensitive = sorted(set(loaded) & SENSITIVE_FRONTMATTER_FIELDS)
    if sensitive:
        raise VaultError(
            "sensitive_frontmatter_field",
            "/frontmatter",
            "campos atribuídos somente pelo catálogo: " + ", ".join(sensitive),
        )
    unknown = sorted(set(loaded) - ALLOWED_FRONTMATTER_FIELDS)
    if unknown:
        raise VaultError("unknown_frontmatter_field", "/frontmatter", ", ".join(unknown))
    if "title" in loaded and (not isinstance(loaded["title"], str) or not loaded["title"].strip()):
        raise VaultError("invalid_frontmatter_title", "/frontmatter/title", "título deve ser texto não vazio")
    if "language" in loaded and (not isinstance(loaded["language"], str) or len(loaded["language"]) < 2):
        raise VaultError("invalid_frontmatter_language", "/frontmatter/language", "idioma inválido")
    if "topics" in loaded and (
        not isinstance(loaded["topics"], list)
        or not all(isinstance(topic, str) and topic for topic in loaded["topics"])
    ):
        raise VaultError("invalid_frontmatter_topics", "/frontmatter/topics", "topics deve ser lista textual")
    if "relationships" in loaded and not isinstance(loaded["relationships"], list):
        raise VaultError(
            "invalid_frontmatter_relationships",
            "/frontmatter/relationships",
            "relationships deve ser lista",
        )
    return loaded, closing_index + 1


def _effective_metadata(base: dict[str, Any], frontmatter: dict[str, Any]) -> dict[str, Any]:
    metadata = copy.deepcopy(base)
    provenance = metadata.setdefault("metadata_provenance", {})
    if "title" in frontmatter:
        metadata["title"] = frontmatter["title"].strip()
        provenance["title"] = {"origin": "accepted_frontmatter", "evidence_ref": "frontmatter:title"}
    if "language" in frontmatter:
        metadata["language"] = frontmatter["language"]
        provenance["language"] = {"origin": "accepted_frontmatter", "evidence_ref": "frontmatter:language"}
    if "topics" in frontmatter:
        metadata["applicability"]["topics"] = list(dict.fromkeys(frontmatter["topics"]))
        provenance["applicability.topics"] = {
            "origin": "accepted_frontmatter",
            "evidence_ref": "frontmatter:topics",
        }
    if "relationships" in frontmatter:
        metadata["relationships"] = frontmatter["relationships"]
        provenance["relationships"] = {
            "origin": "accepted_frontmatter",
            "evidence_ref": "frontmatter:relationships",
        }
    return metadata


def _line_offsets(lines: list[str]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    return offsets


def _unit_kind(tokens: list[Any], start: int, end: int) -> str:
    categories: set[str] = set()
    for token in tokens:
        if token.map is None or token.map[1] <= start or token.map[0] >= end:
            continue
        if token.type in {"fence", "code_block"}:
            categories.add("code")
        elif token.type == "table_open":
            categories.add("table")
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            categories.add("list")
        elif token.type in {"paragraph_open", "heading_open"}:
            categories.add("prose")
    if not categories:
        return "prose"
    if len(categories) == 1:
        return next(iter(categories))
    return "mixed"


def _derive_units(
    text: str,
    body_start_line: int,
    metadata: dict[str, Any],
    *,
    max_unit_tokens: int,
) -> list[dict[str, Any]]:
    all_lines = text.splitlines(keepends=True)
    body_lines = all_lines[body_start_line:]
    body = "".join(body_lines)
    if not body.strip():
        return []

    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = parser.parse(body)
    headings: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        title = tokens[index + 1].content.strip() if index + 1 < len(tokens) else ""
        headings.append((token.map[0], int(token.tag[1]), title or "(sem título)"))

    starts = [heading[0] for heading in headings]
    if not starts or "".join(body_lines[: starts[0]]).strip():
        starts.insert(0, 0)
    starts = sorted(set(starts))
    ends = starts[1:] + [len(body_lines)]
    heading_by_start = {start: (level, title) for start, level, title in headings}
    full_offsets = _line_offsets(all_lines)
    heading_stack: list[tuple[int, str]] = []
    last_unit_by_level: dict[int, str] = {}
    units: list[dict[str, Any]] = []
    source_ref = {
        "source_id": metadata["source_id"],
        "revision": metadata["revision"],
        "content_digest": metadata["content_digest"],
    }

    for start, end in zip(starts, ends, strict=True):
        content = "".join(body_lines[start:end])
        if not content.strip():
            continue
        heading = heading_by_start.get(start)
        parent_unit_id = None
        if heading:
            level, title = heading
            heading_stack = [(old_level, old_title) for old_level, old_title in heading_stack if old_level < level]
            heading_stack.append((level, title))
            parent_levels = [candidate for candidate in last_unit_by_level if candidate < level]
            if parent_levels:
                parent_unit_id = last_unit_by_level[max(parent_levels)]
            for candidate in list(last_unit_by_level):
                if candidate >= level:
                    del last_unit_by_level[candidate]
            heading_path = [item[1] for item in heading_stack]
        else:
            level = 0
            heading_path = []

        content_bytes = content.encode("utf-8")
        digest = {"algorithm": "sha256", "value": hashlib.sha256(content_bytes).hexdigest()}
        seed = f"{metadata['source_id']}\0{metadata['revision']}\0{len(units)}\0{digest['value']}"
        unit_id = "unit-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
        estimated_tokens = math.ceil(len(content) / 4)
        absolute_start = body_start_line + start
        absolute_end = body_start_line + end
        unit = {
            "unit_id": unit_id,
            "source_ref": source_ref,
            "ordinal": len(units),
            "unit_kind": _unit_kind(tokens, start, end),
            "heading_path": heading_path,
            "source_span": {
                "start_line": absolute_start + 1,
                "end_line": absolute_end,
                "start_byte": full_offsets[absolute_start],
                "end_byte": full_offsets[absolute_end],
            },
            "content_digest": digest,
            "size_bytes": len(content_bytes),
            "parent_unit_id": parent_unit_id,
            "effective_topics": metadata["applicability"]["topics"],
            "effective_applicability": metadata["applicability"],
            "effective_classification": metadata["data_classification"],
            "instruction_scope": metadata["instruction_scope"],
            "token_estimates": [
                {"tokenizer_ref": "char4-v1", "count": estimated_tokens, "status": "estimated"}
            ],
            "derivation": {
                "parser": "markdown-it-py",
                "parser_version": "4.2.0",
                "policy": "heading-sections-v1",
            },
            "oversized": estimated_tokens > max_unit_tokens,
        }
        units.append(unit)
        if heading:
            last_unit_by_level[level] = unit_id
    return units


def ingest_markdown_source(
    vault_root: Path,
    source_metadata: dict[str, Any],
    *,
    max_source_bytes: int = 1_048_576,
    max_frontmatter_bytes: int = 65_536,
    max_frontmatter_nodes: int = 10_000,
    max_unit_tokens: int = 2_000,
) -> IngestedSource:
    """Ingere uma revisão exata e deriva unidades sem efeitos fora do Vault."""

    metadata_issues = validate_knowledge_source(source_metadata)
    if metadata_issues:
        issue = metadata_issues[0]
        raise VaultError(issue.code, issue.path, issue.message)
    path = _safe_path(vault_root, source_metadata["content_locator"]["path"])
    raw = path.read_bytes()
    if len(raw) > max_source_bytes:
        raise VaultError("source_too_large", "/size_bytes", "fonte excede limite")
    if len(raw) != source_metadata["size_bytes"]:
        raise VaultError("size_mismatch", "/size_bytes", "tamanho difere da metadata")
    digest_value = hashlib.sha256(raw).hexdigest()
    if digest_value != source_metadata["content_digest"]["value"]:
        raise VaultError("integrity_mismatch", "/content_digest", "digest da fonte diverge")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise VaultError("invalid_utf8", "/encoding", str(error)) from error

    frontmatter, body_start_line = _frontmatter(
        text,
        max_bytes=max_frontmatter_bytes,
        max_nodes=max_frontmatter_nodes,
    )
    effective = _effective_metadata(source_metadata, frontmatter)
    effective_issues = validate_knowledge_source(effective)
    if effective_issues:
        issue = effective_issues[0]
        raise VaultError(issue.code, issue.path, issue.message)
    units = _derive_units(text, body_start_line, effective, max_unit_tokens=max_unit_tokens)
    artifact_seed = f"{effective['source_id']}\0{effective['revision']}\0{digest_value}"
    artifact_ref = {
        "artifact_id": "artifact-" + hashlib.sha256(artifact_seed.encode("utf-8")).hexdigest()[:20],
        "content_digest": effective["content_digest"],
    }
    return IngestedSource(effective, frontmatter, artifact_ref, units)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _verify_status_assertion(assertion: dict[str, Any]) -> None:
    required = {
        "assertion_id",
        "source_ref",
        "status",
        "effective_at",
        "issuer_ref",
        "evidence_ref",
        "assertion_digest",
    }
    if set(assertion) != required:
        raise VaultError("invalid_status_assertion", "/status_assertions", "campos inválidos")
    unsigned = {key: value for key, value in assertion.items() if key != "assertion_digest"}
    if canonical_digest(unsigned) != assertion["assertion_digest"]:
        raise VaultError("status_assertion_digest_mismatch", "/status_assertions", "digest inválido")


def build_catalog_snapshot(vault_root: Path, definition: dict[str, Any]) -> dict[str, Any]:
    """Constrói snapshot fechado a partir de definição autorizada e fontes locais."""

    assertions = definition["status_assertions"]
    for assertion in assertions:
        _verify_status_assertion(assertion)
    cutoff = _timestamp(definition["status_cutoff"])
    source_entries = []
    exclusions = []
    seen_sources: set[tuple[str, str]] = set()

    for source_metadata in definition["sources"]:
        key = (source_metadata["source_id"], source_metadata["revision"])
        if key in seen_sources:
            raise VaultError("duplicate_source_revision", "/sources", f"fonte duplicada: {key}")
        seen_sources.add(key)
        applicable = [
            assertion
            for assertion in assertions
            if assertion["source_ref"]["source_id"] == key[0]
            and assertion["source_ref"]["revision"] == key[1]
            and assertion["source_ref"]["content_digest"] == source_metadata["content_digest"]
            and _timestamp(assertion["effective_at"]) <= cutoff
        ]
        if not applicable:
            exclusions.append(
                {"source_id": key[0], "revision": key[1], "reason_code": "missing_status", "details": ""}
            )
            continue
        applicable.sort(key=lambda item: (_timestamp(item["effective_at"]), item["assertion_id"]))
        latest_time = _timestamp(applicable[-1]["effective_at"])
        latest = [item for item in applicable if _timestamp(item["effective_at"]) == latest_time]
        statuses = {item["status"] for item in latest}
        if len(statuses) != 1:
            exclusions.append(
                {"source_id": key[0], "revision": key[1], "reason_code": "status_conflict", "details": ""}
            )
            continue
        status = latest[-1]["status"]
        if status != "active":
            exclusions.append(
                {"source_id": key[0], "revision": key[1], "reason_code": status, "details": ""}
            )
            continue
        try:
            ingested = ingest_markdown_source(vault_root, source_metadata)
        except VaultError as error:
            exclusions.append(
                {
                    "source_id": key[0],
                    "revision": key[1],
                    "reason_code": error.code,
                    "details": error.message,
                }
            )
            continue
        source_entries.append(
            {
                "source_metadata": ingested.metadata,
                "resolved_status": status,
                "status_assertion_refs": [item["assertion_id"] for item in applicable],
                "materialized_artifact_ref": ingested.materialized_artifact_ref,
                "units": ingested.units,
            }
        )

    snapshot = {
        "schema_version": "0.1",
        "catalog_id": definition["catalog_id"],
        "version": definition["version"],
        "created_at": definition["created_at"],
        "status_cutoff": definition["status_cutoff"],
        "catalog_policy_ref": definition["catalog_policy_ref"],
        "status_assertions": assertions,
        "source_entries": source_entries,
        "parser_chunker_snapshots": definition["parser_chunker_snapshots"],
        "vocabulary_refs": definition.get("vocabulary_refs", []),
        "exclusions": exclusions,
    }
    snapshot["catalog_hash"] = canonical_digest(snapshot)
    issues = validate_instance(snapshot, "knowledge_catalog")
    if issues:
        issue = issues[0]
        raise VaultError(issue.code, issue.path, issue.message)
    return snapshot
