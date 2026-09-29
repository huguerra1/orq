from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError

from orq_contracts.mcp_server import create_knowledge_server
from orq_contracts.vault import build_catalog_snapshot


ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "fixtures" / "v0.1" / "vault"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def catalog() -> dict:
    definition = json.loads((VAULT / "catalog-definition.json").read_text())
    return build_catalog_snapshot(VAULT, definition)


@pytest.fixture
async def client(catalog):
    server = create_knowledge_server(VAULT, catalog)
    async with Client(server, raise_exceptions=True) as connected:
        yield connected


@pytest.mark.anyio
async def test_exposes_only_catalog_and_exact_unit_resources(client) -> None:
    resources = await client.list_resources()
    templates = await client.list_resource_templates()
    tools = await client.list_tools()
    prompts = await client.list_prompts()

    uris = [str(item.uri) for item in resources.resources]
    assert uris[0] == "orq://catalog/summary"
    assert all(uri.startswith("orq://knowledge/project-demo-architecture/1/unit-") for uri in uris[1:])
    assert len(uris) == 3
    assert templates.resource_templates == []
    assert tools.tools == []
    assert prompts.prompts == []


@pytest.mark.anyio
async def test_catalog_summary_is_sanitized(client) -> None:
    result = await client.read_resource("orq://catalog/summary")
    text = result.contents[0].text
    summary = json.loads(text)

    assert summary["catalog_ref"]["id"] == "knowledge-project-demo"
    assert summary["sources"][0]["units"]
    assert "content_locator" not in text
    assert "content/projects" not in text


@pytest.mark.anyio
async def test_reads_exact_unit_with_integrity_metadata(client, catalog) -> None:
    entry = catalog["source_entries"][0]
    source = entry["source_metadata"]
    unit = entry["units"][0]
    uri = f"orq://knowledge/{source['source_id']}/{source['revision']}/{unit['unit_id']}"

    result = await client.read_resource(uri)
    payload = json.loads(result.contents[0].text)

    assert payload["unit_digest"] == unit["content_digest"]
    assert payload["source_digest"] == source["content_digest"]
    assert payload["content"].startswith("# Arquitetura")


@pytest.mark.anyio
async def test_unknown_unit_is_not_resolved_by_fallback(client) -> None:
    with pytest.raises(MCPError, match="Unknown resource"):
        await client.read_resource("orq://knowledge/project-demo-architecture/1/unit-inexistente")


@pytest.mark.anyio
async def test_mutated_source_is_rejected(catalog, tmp_path) -> None:
    vault = tmp_path / "vault"
    source_path = catalog["source_entries"][0]["source_metadata"]["content_locator"]["path"]
    destination = vault / source_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes((VAULT / source_path).read_bytes())
    server = create_knowledge_server(vault, catalog)
    destination.write_text("# alterado\n")
    unit = catalog["source_entries"][0]["units"][0]

    async with Client(server, raise_exceptions=True) as connected:
        with pytest.raises(MCPError, match="integrity_mismatch"):
            await connected.read_resource(
                f"orq://knowledge/project-demo-architecture/1/{unit['unit_id']}"
            )


@pytest.mark.anyio
async def test_stdio_server_starts_without_corrupting_protocol(catalog, tmp_path) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog))
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "orq_contracts.mcp_server",
            "--root",
            str(VAULT),
            "--catalog",
            str(catalog_path),
        ],
        cwd=ROOT,
    )

    async with Client(parameters, read_timeout_seconds=5, raise_exceptions=True) as connected:
        resources = await connected.list_resources()

    uris = [str(item.uri) for item in resources.resources]
    assert uris[0] == "orq://catalog/summary"
    assert len(uris) == 3
