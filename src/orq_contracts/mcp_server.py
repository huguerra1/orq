"""Adaptador MCP somente leitura para conhecimento já autorizado pelo ORQ."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError

from .canonical import canonical_digest, canonicalize
from .selection import materialize_unit
from .validator import validate_instance
from .vault import VaultError


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _validated_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    issues = validate_instance(catalog, "knowledge_catalog")
    if issues:
        issue = issues[0]
        raise ValueError(f"{issue.code} at {issue.path}: {issue.message}")
    unsigned = {key: value for key, value in catalog.items() if key != "catalog_hash"}
    if canonical_digest(unsigned) != catalog["catalog_hash"]:
        raise ValueError("record_digest_mismatch at /catalog_hash")
    return catalog


def _summary(catalog: dict[str, Any]) -> dict[str, Any]:
    sources = []
    for entry in catalog["source_entries"]:
        source = entry["source_metadata"]
        sources.append(
            {
                "source_id": source["source_id"],
                "revision": source["revision"],
                "title": source["title"],
                "knowledge_kind": source["knowledge_kind"],
                "language": source["language"],
                "project_scope": source["project_scope"],
                "authority": source["authority"],
                "instruction_scope": source["instruction_scope"],
                "data_classification": source["data_classification"],
                "resolved_status": entry["resolved_status"],
                "units": [
                    {
                        "unit_id": unit["unit_id"],
                        "ordinal": unit["ordinal"],
                        "unit_kind": unit["unit_kind"],
                        "heading_path": unit["heading_path"],
                        "content_digest": unit["content_digest"],
                        "size_bytes": unit["size_bytes"],
                        "token_estimates": unit["token_estimates"],
                        "effective_topics": unit["effective_topics"],
                    }
                    for unit in entry["units"]
                ],
            }
        )
    return {
        "schema_version": "0.1",
        "catalog_ref": {
            "id": catalog["catalog_id"],
            "version": catalog["version"],
            "content_digest": catalog["catalog_hash"],
        },
        "created_at": catalog["created_at"],
        "status_cutoff": catalog["status_cutoff"],
        "sources": sources,
    }


def create_knowledge_server(
    vault_root: Path,
    catalog: dict[str, Any],
    *,
    max_unit_bytes: int = 262_144,
    max_summary_bytes: int = 1_048_576,
) -> MCPServer:
    """Cria servidor preso a um snapshot e uma raiz definidos pelo processo."""

    if max_unit_bytes < 1 or max_summary_bytes < 1:
        raise ValueError("limites MCP devem ser positivos")
    root = vault_root.resolve(strict=True)
    snapshot = _validated_catalog(catalog)
    summary = _summary(snapshot)
    summary_content = canonicalize(summary)
    if len(summary_content) > max_summary_bytes:
        raise ValueError("catalog_summary_too_large")

    units: dict[tuple[str, str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    for entry in snapshot["source_entries"]:
        source = entry["source_metadata"]
        for unit in entry["units"]:
            key = source["source_id"], source["revision"], unit["unit_id"]
            if key in units:
                raise ValueError(f"duplicate_unit_ref: {key}")
            units[key] = (source, unit)

    server = MCPServer(
        "orq-knowledge",
        description="Conhecimento exato já autorizado e fixado pelo Orchestrator ORQ.",
        version="0.1.0",
        log_level="WARNING",
    )

    @server.resource(
        "orq://catalog/summary",
        name="catalog_summary",
        description="Resumo sanitizado do snapshot fixo de conhecimento.",
        mime_type="application/json",
    )
    async def catalog_summary() -> str:
        return summary_content.decode("utf-8")

    def unit_reader(source: dict[str, Any], unit: dict[str, Any]):
        async def read_unit() -> str:
            if unit["size_bytes"] > max_unit_bytes:
                raise ResourceError("knowledge_unit_too_large")
            try:
                content = materialize_unit(root, source, unit)
            except VaultError as error:
                raise ResourceError(f"{error.code}: {error.message}") from error
            payload = {
                "schema_version": "0.1",
                "source_id": source["source_id"],
                "revision": source["revision"],
                "unit_id": unit["unit_id"],
                "source_digest": source["content_digest"],
                "unit_digest": unit["content_digest"],
                "media_type": source["media_type"],
                "data_classification": source["data_classification"],
                "size_bytes": len(content.encode("utf-8")),
                "content": content,
            }
            return canonicalize(payload).decode("utf-8")

        return read_unit

    for index, (key, (source, unit)) in enumerate(sorted(units.items()), start=1):
        source_id, revision, unit_id = key
        server.resource(
            f"orq://knowledge/{source_id}/{revision}/{unit_id}",
            name=f"knowledge_unit_{index}",
            description="Unidade exata do Vault; não pesquisa nem escolhe fontes.",
            mime_type="application/json",
        )(unit_reader(source, unit))

    return server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orq-knowledge-mcp")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--max-unit-bytes", type=int, default=262_144)
    parser.add_argument("--max-summary-bytes", type=int, default=1_048_576)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    server = create_knowledge_server(
        arguments.root,
        _load_json(arguments.catalog),
        max_unit_bytes=arguments.max_unit_bytes,
        max_summary_bytes=arguments.max_summary_bytes,
    )
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
