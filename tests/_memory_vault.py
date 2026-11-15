"""Vault de teste com audiências por família para os testes do 7B (sem banco)."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from orq_contracts.canonical import canonical_digest
from orq_contracts.vault import build_catalog_snapshot


HASH = "a" * 64
PROJECT = "proj-a"
OTHER_PROJECT = "proj-b"


def versioned_ref(identifier: str) -> dict[str, Any]:
    return {"id": identifier, "version": "1", "content_digest": {"algorithm": "sha256", "value": HASH}}


def audience(families: list[str] | None = None, roles: list[str] | None = None) -> dict[str, Any]:
    families = families or []
    roles = roles or []
    return {
        "schema_version": "0.1",
        "area_scope": "family" if families or roles else "common",
        "target_families": families,
        "target_roles": roles,
    }


# (source_id, escopo de projeto, tipo de conhecimento, audiência ou None, texto Markdown)
SOURCES: list[tuple[str, dict[str, Any], str, dict[str, Any] | None, str]] = [
    (
        "common-project",
        {"kind": "project", "project_id": PROJECT},
        "project",
        None,
        "# Convenções\n\nRegras gerais do repositório.\n\n## Git\n\nCommits pequenos e revisados.\n",
    ),
    (
        "codex-only",
        {"kind": "project", "project_id": PROJECT},
        "project",
        audience(["family_codex"]),
        "# Codex no Windows\n\nUse o pool de threads.\n\n## Sandbox\n\nLimites do sandbox do Codex.\n",
    ),
    (
        "claude-only",
        {"kind": "project", "project_id": PROJECT},
        "project",
        audience(["family_claude"]),
        "# Claude Code\n\nPermissões do Claude Code.\n",
    ),
    (
        "claude-security",
        {"kind": "project", "project_id": PROJECT},
        "project",
        audience(["family_claude"], ["security_reviewer"]),
        "# Checklist de segurança\n\nRevisão de segredos e permissões.\n",
    ),
    (
        "tester-only",
        {"kind": "project", "project_id": PROJECT},
        "project",
        audience(roles=["tester"]),
        "# Testes\n\nCobertura mínima e testes negativos.\n",
    ),
    (
        "global-ops",
        {"kind": "global_operational"},
        "operational",
        audience(),
        "# Operação global\n\nNunca extrair tokens das CLIs.\n",
    ),
    (
        "other-project",
        {"kind": "project", "project_id": OTHER_PROJECT},
        "project",
        None,
        "# Outro projeto\n\nConteúdo de outro projeto.\n",
    ),
    (
        "collection-src",
        {"kind": "collection", "collection_id": "shared-notes"},
        "project",
        None,
        "# Coleção\n\nNotas compartilhadas por coleção.\n",
    ),
]

# Unidade com audiência própria (sobrepõe a da fonte): 2ª unidade de common-project só para Gemini.
UNIT_OVERRIDE = ("common-project", 1, audience(["family_gemini"]))


def source_metadata(
    source_id: str,
    raw: bytes,
    path: str,
    project_scope: dict[str, Any],
    knowledge_kind: str,
    source_audience: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata = {
        "schema_version": "0.1",
        "source_id": source_id,
        "revision": "1",
        "title": f"Fonte {source_id}",
        "knowledge_kind": knowledge_kind,
        "content_locator": {"root_id": "vault", "path": path},
        "media_type": "text/markdown",
        "encoding": "utf-8",
        "language": "pt-BR",
        "content_digest": {"algorithm": "sha256", "value": hashlib.sha256(raw).hexdigest()},
        "size_bytes": len(raw),
        "project_scope": project_scope,
        "authority": {
            "authority_level": "project_authoritative",
            "authority_scopes": ["project_architecture"],
            "issuer_ref": "catalog-admin",
            "assigned_at": "2026-11-01T10:00:00Z",
            "evidence_ref": f"catalog-entry:{source_id}:1",
        },
        "instruction_scope": "project" if knowledge_kind == "project" else "none",
        "applicability": {
            "roles": [],
            "task_types": [],
            "topics": [],
            "paths": [],
            "domains": [],
            "capabilities": [],
        },
        "data_classification": "internal",
        "owner_ref": "team-demo",
        "verified_at": "2026-11-01T10:00:00Z",
        "effective_from": None,
        "expires_at": None,
        "relationships": [],
        "chunking_policy_ref": versioned_ref("heading-sections-v1"),
        "metadata_provenance": {
            "authority": {"origin": "catalog_rule", "evidence_ref": f"catalog-entry:{source_id}:1"}
        },
    }
    if source_audience is not None:
        metadata["audience"] = source_audience
    return metadata


def status_assertion(metadata: dict[str, Any]) -> dict[str, Any]:
    assertion = {
        "assertion_id": f"status-{metadata['source_id']}",
        "source_ref": {
            "source_id": metadata["source_id"],
            "revision": metadata["revision"],
            "content_digest": metadata["content_digest"],
        },
        "status": "active",
        "effective_at": "2026-11-01T10:00:00Z",
        "issuer_ref": "catalog-admin",
        "evidence_ref": f"approval:{metadata['source_id']}",
    }
    assertion["assertion_digest"] = canonical_digest(assertion)
    return assertion


def rehash(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Recalcula o catalog_hash depois de uma alteração autorizada no snapshot."""

    updated = copy.deepcopy(snapshot)
    updated.pop("catalog_hash", None)
    updated["catalog_hash"] = canonical_digest(updated)
    return updated


def build_family_vault(
    root: Path,
    catalog_id: str,
    *,
    overrides: dict[str, str] | None = None,
    with_unit_override: bool = True,
) -> dict[str, Any]:
    """Escreve as fontes em `root`, monta o snapshot pela Fase 2 e aplica a audiência por unidade."""

    overrides = overrides or {}
    sources = []
    assertions = []
    for source_id, scope, kind, source_audience, text in SOURCES:
        raw = overrides.get(source_id, text).encode("utf-8")
        path = f"content/{source_id}.md"
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        metadata = source_metadata(source_id, raw, path, scope, kind, source_audience)
        sources.append(metadata)
        assertions.append(status_assertion(metadata))
    definition = {
        "catalog_id": catalog_id,
        "version": "1",
        "created_at": "2026-11-14T12:00:00Z",
        "status_cutoff": "2026-11-14T12:00:00Z",
        "catalog_policy_ref": versioned_ref("catalog-policy"),
        "parser_chunker_snapshots": [versioned_ref("markdown-heading-sections")],
        "vocabulary_refs": [],
        "sources": sources,
        "status_assertions": assertions,
    }
    snapshot = build_catalog_snapshot(root, definition)
    assert not snapshot["exclusions"], snapshot["exclusions"]
    if with_unit_override:
        source_id, ordinal, unit_audience = UNIT_OVERRIDE
        for entry in snapshot["source_entries"]:
            if entry["source_metadata"]["source_id"] == source_id:
                entry["units"][ordinal]["audience"] = unit_audience
        snapshot = rehash(snapshot)
    return snapshot
