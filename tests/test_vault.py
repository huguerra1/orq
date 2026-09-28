from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from orq_contracts.canonical import canonical_digest
from orq_contracts.validator import validate_instance
from orq_contracts.vault import VaultError, build_catalog_snapshot, ingest_markdown_source


HASH = "a" * 64


def versioned_ref(identifier: str) -> dict:
    return {
        "id": identifier,
        "version": "1",
        "content_digest": {"algorithm": "sha256", "value": HASH},
    }


def source_metadata(raw: bytes, path: str = "content/projects/demo/guide.md") -> dict:
    return {
        "schema_version": "0.1",
        "source_id": "source-demo",
        "revision": "1",
        "title": "Título do catálogo",
        "knowledge_kind": "project",
        "content_locator": {"root_id": "vault", "path": path},
        "media_type": "text/markdown",
        "encoding": "utf-8",
        "language": "pt-BR",
        "content_digest": {
            "algorithm": "sha256",
            "value": hashlib.sha256(raw).hexdigest(),
        },
        "size_bytes": len(raw),
        "project_scope": {"kind": "project", "project_id": "demo"},
        "authority": {
            "authority_level": "project_authoritative",
            "authority_scopes": ["project_architecture"],
            "issuer_ref": "catalog-admin",
            "assigned_at": "2026-09-28T10:00:00Z",
            "evidence_ref": "catalog-entry:source-demo:1",
        },
        "instruction_scope": "project",
        "applicability": {
            "roles": ["backend_engineer"],
            "task_types": ["implementation"],
            "topics": ["original"],
            "paths": ["src/**"],
            "domains": ["backend"],
            "capabilities": [],
        },
        "data_classification": "internal",
        "owner_ref": "team-demo",
        "verified_at": "2026-09-28T10:00:00Z",
        "effective_from": None,
        "expires_at": None,
        "relationships": [],
        "chunking_policy_ref": versioned_ref("heading-sections-v1"),
        "metadata_provenance": {
            "authority": {"origin": "catalog_rule", "evidence_ref": "catalog-entry:source-demo:1"}
        },
    }


def write_source(vault: Path, raw: bytes, relative: str = "content/projects/demo/guide.md") -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def active_assertion(metadata: dict, *, status: str = "active", assertion_id: str = "status-1") -> dict:
    assertion = {
        "assertion_id": assertion_id,
        "source_ref": {
            "source_id": metadata["source_id"],
            "revision": metadata["revision"],
            "content_digest": metadata["content_digest"],
        },
        "status": status,
        "effective_at": "2026-09-28T10:00:00Z",
        "issuer_ref": "catalog-admin",
        "evidence_ref": "approval:1",
    }
    assertion["assertion_digest"] = canonical_digest(assertion)
    return assertion


def catalog_definition(metadata: dict, assertions: list[dict]) -> dict:
    return {
        "catalog_id": "knowledge-demo",
        "version": "1",
        "created_at": "2026-09-28T11:00:00Z",
        "status_cutoff": "2026-09-28T11:00:00Z",
        "catalog_policy_ref": versioned_ref("catalog-policy"),
        "parser_chunker_snapshots": [versioned_ref("markdown-parser")],
        "vocabulary_refs": [],
        "sources": [metadata],
        "status_assertions": assertions,
    }


def test_ingests_safe_frontmatter_and_preserves_structural_blocks(tmp_path) -> None:
    raw = b"""---
title: Guia efetivo
language: pt-BR
topics: [api, seguranca]
---
# Introducao

Texto inicial.

## Codigo

```python
print(\"ok\")
```

## Tabela

| a | b |
| - | - |
| 1 | 2 |
"""
    vault = tmp_path / "vault"
    vault.mkdir()
    write_source(vault, raw)

    ingested = ingest_markdown_source(vault, source_metadata(raw))

    assert ingested.metadata["title"] == "Guia efetivo"
    assert ingested.metadata["applicability"]["topics"] == ["api", "seguranca"]
    assert len(ingested.units) == 3
    assert ingested.units[1]["heading_path"] == ["Introducao", "Codigo"]
    assert ingested.units[1]["unit_kind"] == "mixed"
    assert ingested.units[2]["unit_kind"] == "mixed"


def test_sensitive_frontmatter_field_is_rejected(tmp_path) -> None:
    raw = b"---\nauthority: platform_authoritative\n---\n# Conteudo\n"
    vault = tmp_path / "vault"
    vault.mkdir()
    write_source(vault, raw)

    with pytest.raises(VaultError, match="sensitive_frontmatter_field"):
        ingest_markdown_source(vault, source_metadata(raw))


def test_unsafe_yaml_tag_is_rejected(tmp_path) -> None:
    raw = b"---\ntitle: !!python/object/apply:os.system ['echo unsafe']\n---\n# Conteudo\n"
    vault = tmp_path / "vault"
    vault.mkdir()
    write_source(vault, raw)

    with pytest.raises(VaultError, match="invalid_frontmatter_yaml"):
        ingest_markdown_source(vault, source_metadata(raw))


def test_path_traversal_is_rejected(tmp_path) -> None:
    raw = b"# Conteudo\n"
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(VaultError, match="invalid_locator"):
        ingest_markdown_source(vault, source_metadata(raw, "../outside.md"))


def test_symlink_escape_is_rejected(tmp_path) -> None:
    raw = b"# Conteudo\n"
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_bytes(raw)
    link = vault / "content" / "projects" / "demo" / "guide.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    with pytest.raises(VaultError, match="locator_escape"):
        ingest_markdown_source(vault, source_metadata(raw))


def test_digest_mismatch_is_rejected(tmp_path) -> None:
    raw = b"# Conteudo\n"
    vault = tmp_path / "vault"
    vault.mkdir()
    write_source(vault, raw)
    metadata = source_metadata(raw)
    metadata["content_digest"]["value"] = "0" * 64

    with pytest.raises(VaultError, match="integrity_mismatch"):
        ingest_markdown_source(vault, metadata)


def test_oversized_unit_is_marked_without_truncation(tmp_path) -> None:
    raw = ("# Grande\n\n" + ("conteudo " * 100)).encode()
    vault = tmp_path / "vault"
    vault.mkdir()
    write_source(vault, raw)

    ingested = ingest_markdown_source(vault, source_metadata(raw), max_unit_tokens=10)

    assert ingested.units[0]["oversized"] is True
    assert ingested.units[0]["size_bytes"] == len(raw)


def test_builds_valid_catalog_snapshot_and_excludes_revoked_source(tmp_path) -> None:
    raw = b"# Conteudo\n\nValido.\n"
    vault = tmp_path / "vault"
    vault.mkdir()
    write_source(vault, raw)
    metadata = source_metadata(raw)

    active = build_catalog_snapshot(vault, catalog_definition(metadata, [active_assertion(metadata)]))
    assert len(active["source_entries"]) == 1
    assert active["exclusions"] == []
    assert validate_instance(active, "knowledge_catalog") == []

    revoked_assertion = active_assertion(metadata, status="revoked", assertion_id="status-2")
    revoked_assertion["effective_at"] = "2026-09-28T10:30:00Z"
    unsigned = {key: value for key, value in revoked_assertion.items() if key != "assertion_digest"}
    revoked_assertion["assertion_digest"] = canonical_digest(unsigned)
    revoked = build_catalog_snapshot(
        vault,
        catalog_definition(metadata, [active_assertion(metadata), revoked_assertion]),
    )
    assert revoked["source_entries"] == []
    assert revoked["exclusions"][0]["reason_code"] == "revoked"


def test_status_assertion_digest_is_verified(tmp_path) -> None:
    raw = b"# Conteudo\n"
    vault = tmp_path / "vault"
    vault.mkdir()
    write_source(vault, raw)
    metadata = source_metadata(raw)
    assertion = active_assertion(metadata)
    assertion["assertion_digest"]["value"] = "0" * 64

    with pytest.raises(VaultError, match="status_assertion_digest_mismatch"):
        build_catalog_snapshot(vault, catalog_definition(metadata, [assertion]))
