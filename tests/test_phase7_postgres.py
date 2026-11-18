"""7E contra o Postgres de serviço: mesmos runner e MCP, RLS real."""
import hashlib
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("ORQ_MEMORY_DSN"), reason="ORQ_MEMORY_DSN ausente")
pytest.importorskip("psycopg")

from test_knowledge_search_postgres import admin, config, _delete_catalog
from test_phase7_scenarios import data_for, runtime
from test_runner_scenarios import make_runner, events
from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.cli import main
from orq_contracts.memory_store import build_sync_plan, MemoryScope
from orq_contracts.memory_store.postgres import sync_catalog, scoped_transaction
from _memory_vault import build_family_vault
from orq_contracts.knowledge_backend import scope_allows


@pytest.fixture
def postgres_data(tmp_path, admin):
    data, profiles = data_for(tmp_path / "vault")
    catalog = data["knowledge_catalog"]
    catalog["catalog_id"] = "cat-7e-" + uuid.uuid4().hex[:12]
    catalog["catalog_hash"] = canonical_digest({k: v for k, v in catalog.items() if k != "catalog_hash"})
    yield data, profiles
    _delete_catalog(admin, catalog["catalog_id"])


def test_Q26_runner_fallback_rematerializes_under_real_rls(tmp_path, admin, config, postgres_data):
    data, profiles = postgres_data
    sync_catalog(admin, build_sync_plan(data["vault_root"], data["knowledge_catalog"]))
    rt = runtime(profiles)
    rt.memory_config = config
    with make_runner(tmp_path / "store", data, family_runtime=rt) as r:
        assert r.run().state == "succeeded"
        sealed = [e["payload"] for e in events(r, "attempt_context_sealed")]
        assert [e["selection"]["backend"] for e in sealed] == ["postgres_fts_trgm"] * 2
        for entry, own, other in [(sealed[0], "codex-only", "claude-only"), (sealed[1], "claude-only", "codex-only")]:
            sources = {item["knowledge_provenance"]["unit_ref"]["source_id"] for item in entry["manifest"]["items"]}
            assert own in sources and other not in sources
        assert sealed[0]["manifest"]["bundle_hash"] != sealed[1]["manifest"]["bundle_hash"]
        assert not r.projection.cost_summary["total_cost_exact"]


def test_Q27_database_mutation_blocks_runner_before_dispatch(tmp_path, admin, config, postgres_data):
    data, profiles = postgres_data
    catalog = data["knowledge_catalog"]
    sync_catalog(admin, build_sync_plan(data["vault_root"], catalog))
    altered = "# Codex\n\nBytes divergentes do catálogo.\n"
    admin.execute("UPDATE orq_memory.knowledge_units SET content=%s, content_digest=%s, size_bytes=%s"
                  " WHERE catalog_id=%s AND source_id='codex-only'", (altered,
                  hashlib.sha256(altered.encode()).hexdigest(), len(altered.encode()), catalog["catalog_id"]))
    admin.commit()
    rt = runtime(profiles)
    rt.memory_config = config
    with make_runner(tmp_path / "store", data, family_runtime=rt) as r:
        assert r.run().state == "failed"
        assert not events(r, "dispatch_requested") and not r.projection.attempts
        assert events(r, "error_attached")[0]["payload"]["code"] == "integrity_mismatch"


def test_Q18_sync_db_cli_twice_has_exact_vault_digests(tmp_path, admin, postgres_data, capsys, monkeypatch):
    data, _ = postgres_data
    monkeypatch.delenv("ORQ_KNOWLEDGE_BACKEND", raising=False)
    catalog = build_family_vault(data["vault_root"], data["knowledge_catalog"]["catalog_id"])
    data["knowledge_catalog"] = catalog
    path = tmp_path / "catalog.json"
    path.write_bytes(canonicalize(catalog))
    arguments = ["vault", "sync-db", "--root", str(data["vault_root"]), "--catalog", str(path)]
    assert main(arguments) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(arguments) == 0
    second = json.loads(capsys.readouterr().out)
    assert (first["sync"], second["sync"]) == ("inserted", "unchanged")
    assert first["sync_digest"] == second["sync_digest"]
    for family in ("family_codex", "family_claude"):
        scope = MemoryScope(catalog["catalog_id"], data["request"]["project_id"], family, "backend_engineer")
        with scoped_transaction(admin, scope) as cursor:
            cursor.execute("SELECT source_id, ordinal, source_digest, content_digest FROM orq_memory.knowledge_units")
            rows = cursor.fetchall()
        for row in rows:
            entry = next(e for e in catalog["source_entries"] if e["source_metadata"]["source_id"] == row["source_id"])
            unit = next(u for u in entry["units"] if u["ordinal"] == row["ordinal"])
            assert row["source_digest"] == entry["source_metadata"]["content_digest"]["value"]
            assert row["content_digest"] == unit["content_digest"]["value"]
        expected = {(e["source_metadata"]["source_id"], u["ordinal"]) for e in catalog["source_entries"] for u in e["units"]
                    if scope_allows(e, e["source_metadata"], u, scope)}
        assert {(row["source_id"], row["ordinal"]) for row in rows} == expected
        assert "global-ops" in {row["source_id"] for row in rows}
