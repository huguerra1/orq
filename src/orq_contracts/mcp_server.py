"""Adaptador MCP somente leitura para conhecimento já autorizado pelo ORQ."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

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
    family_session=None,
) -> MCPServer:
    """Cria servidor preso a um snapshot e uma raiz definidos pelo processo."""

    if max_unit_bytes < 1 or max_summary_bytes < 1:
        raise ValueError("limites MCP devem ser positivos")
    root = vault_root.resolve(strict=True)
    snapshot = _validated_catalog(catalog)
    if family_session is not None:
        return create_family_knowledge_server(family_session, max_unit_bytes=max_unit_bytes,
                                             max_summary_bytes=max_summary_bytes)
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


def create_family_knowledge_server(session, *, max_unit_bytes=262_144, max_summary_bytes=1_048_576):
    """Mesmo transporte da Fase 3; escopo só é recebido na construção pelo host."""
    if max_unit_bytes < 1 or max_summary_bytes < 1:
        raise ValueError("limites MCP devem ser positivos")
    from .family_mcp import TOOLS

    class ScopedServer(MCPServer):
        async def call_tool(self, name, arguments, context=None):
            if name in TOOLS:
                result = session.call(name, arguments)
                return CallToolResult(content=[TextContent(type="text", text=canonicalize(result).decode())],
                                      is_error="error" in result)
            return await super().call_tool(name, arguments, context)

    server = ScopedServer("orq-knowledge", version="0.1.0", log_level="WARNING")
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

    @server.tool(annotations=annotations, structured_output=False)
    async def search_family_knowledge(requirement_id: str, query: str, limit: int = 20) -> str:
        """Busca candidatos para um requisito no escopo fixado pelo runner (limite 1–20)."""
        return canonicalize(session.call("search_family_knowledge", {
            "requirement_id": requirement_id, "query": query, "limit": limit})).decode()

    @server.tool(annotations=annotations, structured_output=False)
    async def select_family_knowledge(selections: dict[str, list[dict[str, str]]], rationale: str | None = None) -> str:
        """Escolhe unidades devolvidas pela busca; valida cobertura, orçamento e digests."""
        return canonicalize(session.call("select_family_knowledge", {
            "selections": selections, "rationale": rationale})).decode()

    snapshot = session.visible_catalog()
    summary = canonicalize(_summary(snapshot))
    if len(summary) > max_summary_bytes:
        raise ValueError("catalog_summary_too_large")

    @server.resource("orq://catalog/summary", mime_type="application/json")
    async def catalog_summary() -> str:
        return summary.decode()

    def reader(key):
        async def read_unit() -> str:
            # Resources preservados, mas não fornecem atalho para ignorar escolha/orçamento.
            result, _, _ = session.seal() if session._closed else (None, None, None)
            if result is None:
                entries = session.entries()
                delivered = [e["response"] for e in entries if e["tool"] == "select_family_knowledge" and "error" not in e["response"]]
                result = delivered[0] if delivered else None
            if result:
                for item in result["manifest"]["items"]:
                    ref = item["knowledge_provenance"]["unit_ref"]
                    if (ref.get("source_id"), ref.get("revision"), ref.get("unit_id")) == key:
                        data = next(i for i in result["bundle"]["items"] if i["item_id"] == item["item_id"])
                        content = canonicalize({**item, "content": data["content"]})
                        if len(content) > max_unit_bytes:
                            raise ResourceError("knowledge_unit_too_large")
                        return content.decode()
            raise ResourceError("unit_not_selected")
        return read_unit

    for entry in snapshot["source_entries"]:
        source = entry["source_metadata"]
        for unit in entry["units"]:
            key = source["source_id"], source["revision"], unit["unit_id"]
            server.resource("orq://knowledge/" + "/".join(key), mime_type="application/json")(reader(key))
    return server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orq-knowledge-mcp")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--max-unit-bytes", type=int, default=262_144)
    parser.add_argument("--max-summary-bytes", type=int, default=1_048_576)
    parser.add_argument("--attempt-session", type=Path)
    parser.add_argument("--session-digest")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    session = None
    if arguments.attempt_session:
        from .family_mcp import AttemptKnowledgeSession
        from .knowledge_backend import EligibilityPolicy, create_knowledge_backend
        from .memory_store.config import MemoryScope, resolve_memory_config
        from datetime import datetime, timezone
        config = _load_json(arguments.attempt_session / "config.json")
        if canonical_digest(config)["value"] != arguments.session_digest:
            raise ValueError("knowledge_session_mismatch")
        backend = create_knowledge_backend(resolve_memory_config(), catalog=_load_json(arguments.catalog),
            policy=EligibilityPolicy(**config["eligibility"]), scope=MemoryScope(**config["scope"]), vault_root=arguments.root)
        if backend.backend_id != config["backend_id"]:
            raise ValueError("knowledge_backend_mismatch")
        session = AttemptKnowledgeSession(arguments.attempt_session, config, backend,
            clock=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    server = create_knowledge_server(
        arguments.root,
        _load_json(arguments.catalog),
        max_unit_bytes=arguments.max_unit_bytes,
        max_summary_bytes=arguments.max_summary_bytes,
        family_session=session,
    )
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
