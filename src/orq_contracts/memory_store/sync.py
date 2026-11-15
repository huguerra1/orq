"""Plano determinístico de sincronização Vault Markdown -> Postgres, sem banco.

O plano reaproveita a cadeia da Fase 2: cada fonte do snapshot é reingerida com
`ingest_markdown_source` (parser e chunker `heading-sections-v1`), as unidades
derivadas precisam ser idênticas às do snapshot e o conteúdo é lido com
`materialize_unit`, que reconfirma os digests da fonte e da unidade. Os digests
JCS (`canonical_digest`) da metadata e de cada unidade vão para as linhas, o que
torna a paridade Vault/banco verificável.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..canonical import canonical_digest
from ..selection import _estimated_tokens, materialize_unit
from ..validator import validate_instance
from ..vault import VaultError, ingest_markdown_source
from .config import MemoryStoreError


# Fonte/unidade sem audiência declarada pelo catálogo autorizado entra na área comum,
# preservando o comportamento das Fases 2–6 (todo conhecimento visível a qualquer agente).
DEFAULT_AUDIENCE: dict[str, Any] = {
    "schema_version": "0.1",
    "area_scope": "common",
    "target_families": [],
    "target_roles": [],
}


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """Linhas prontas para inserção, na ordem do snapshot, e o digest JCS do conjunto."""

    catalog_row: dict[str, Any]
    source_rows: list[dict[str, Any]]
    unit_rows: list[dict[str, Any]]

    @property
    def catalog_id(self) -> str:
        return self.catalog_row["catalog_id"]

    @property
    def sync_digest(self) -> str:
        return self.catalog_row["sync_digest"]

    def expected_unit_digests(self) -> list[tuple[str, str, str, int, str, str]]:
        """Tuplas (fonte, revisão, unidade, ordinal, digest do conteúdo, digest JCS da unidade) ordenadas."""

        return sorted(
            (
                row["source_id"],
                row["revision"],
                row["unit_id"],
                row["ordinal"],
                row["content_digest"],
                row["unit_record_digest"],
            )
            for row in self.unit_rows
        )


def _integrity(message: str) -> MemoryStoreError:
    return MemoryStoreError("catalog_sync_integrity_error", message)


def effective_audience(source_metadata: dict[str, Any], unit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Audiência da unidade, senão da fonte, senão a área comum; sempre validada pelo schema do 7A."""

    audience = None
    if unit is not None:
        audience = unit.get("audience")
    if audience is None:
        audience = source_metadata.get("audience")
    if audience is None:
        audience = DEFAULT_AUDIENCE
    issues = validate_instance(audience, "audience_metadata")
    if issues:
        issue = issues[0]
        raise MemoryStoreError("invalid_audience", f"{source_metadata['source_id']}: {issue.path} {issue.code}")
    return audience


def _project_columns(source_metadata: dict[str, Any]) -> dict[str, Any]:
    scope = source_metadata["project_scope"]
    return {
        "knowledge_kind": source_metadata["knowledge_kind"],
        "project_scope_kind": scope["kind"],
        "project_id": scope.get("project_id"),
        "collection_id": scope.get("collection_id"),
    }


def _audience_columns(audience: dict[str, Any]) -> dict[str, Any]:
    return {
        "area_scope": audience["area_scope"],
        "target_families": list(audience["target_families"]),
        "target_roles": list(audience["target_roles"]),
    }


def _without_audience(unit: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in unit.items() if key != "audience"}


def build_sync_plan(vault_root: Path, snapshot: dict[str, Any]) -> SyncPlan:
    """Valida o snapshot contra o Vault e monta as linhas de catálogo, fontes e unidades."""

    issues = validate_instance(snapshot, "knowledge_catalog")
    if issues:
        issue = issues[0]
        raise MemoryStoreError("invalid_catalog_snapshot", f"{issue.path}: {issue.code}")
    unsigned = {key: value for key, value in snapshot.items() if key != "catalog_hash"}
    if canonical_digest(unsigned) != snapshot["catalog_hash"]:
        raise _integrity("catalog_hash não confere com o snapshot")

    catalog_id = snapshot["catalog_id"]
    source_rows: list[dict[str, Any]] = []
    unit_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in snapshot["source_entries"]:
        metadata = entry["source_metadata"]
        key = (metadata["source_id"], metadata["revision"])
        if key in seen:
            raise _integrity(f"fonte duplicada no snapshot: {key}")
        seen.add(key)
        try:
            ingested = ingest_markdown_source(vault_root, metadata)
        except VaultError as error:
            raise _integrity(f"{key[0]}@{key[1]}: {error}") from error
        if ingested.metadata != metadata:
            raise _integrity(f"{key[0]}@{key[1]}: metadata reingerida diverge do snapshot")
        if ingested.materialized_artifact_ref != entry["materialized_artifact_ref"]:
            raise _integrity(f"{key[0]}@{key[1]}: artefato materializado diverge do snapshot")
        if ingested.units != [_without_audience(unit) for unit in entry["units"]]:
            raise _integrity(f"{key[0]}@{key[1]}: unidades divergem do chunker da Fase 2")

        project = _project_columns(metadata)
        source_rows.append(
            {
                "catalog_id": catalog_id,
                "source_id": key[0],
                "revision": key[1],
                **project,
                "resolved_status": entry["resolved_status"],
                **_audience_columns(effective_audience(metadata)),
                "title": metadata["title"],
                "source_digest": metadata["content_digest"]["value"],
                "metadata_digest": canonical_digest(metadata)["value"],
                "source_metadata": metadata,
            }
        )
        for unit in entry["units"]:
            try:
                content = materialize_unit(vault_root, metadata, unit)
            except VaultError as error:
                raise _integrity(f"{key[0]}@{key[1]}/{unit['unit_id']}: {error}") from error
            unit_rows.append(
                {
                    "catalog_id": catalog_id,
                    "source_id": key[0],
                    "revision": key[1],
                    "unit_id": unit["unit_id"],
                    "ordinal": unit["ordinal"],
                    "unit_kind": unit["unit_kind"],
                    "parent_unit_id": unit["parent_unit_id"],
                    **project,
                    "resolved_status": entry["resolved_status"],
                    **_audience_columns(effective_audience(metadata, unit)),
                    "title": metadata["title"],
                    "heading_path": list(unit["heading_path"]),
                    "heading_text": " / ".join(unit["heading_path"]),
                    "content": content,
                    "content_digest": unit["content_digest"]["value"],
                    "source_digest": metadata["content_digest"]["value"],
                    "size_bytes": unit["size_bytes"],
                    "estimated_tokens": _estimated_tokens(unit),
                    "unit_record": unit,
                    "unit_record_digest": canonical_digest(unit)["value"],
                }
            )

    catalog_row: dict[str, Any] = {
        "catalog_id": catalog_id,
        "catalog_version": snapshot["version"],
        "catalog_hash": snapshot["catalog_hash"]["value"],
        "created_at": snapshot["created_at"],
        "status_cutoff": snapshot["status_cutoff"],
        "source_count": len(source_rows),
        "unit_count": len(unit_rows),
    }
    catalog_row["sync_digest"] = canonical_digest(
        {"catalog": catalog_row, "sources": source_rows, "units": unit_rows}
    )["value"]
    return SyncPlan(catalog_row, source_rows, unit_rows)
