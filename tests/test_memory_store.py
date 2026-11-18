"""Memória por família (7B): configuração, escopo, DDL, plano de sincronização e modo offline.

Nenhum teste deste arquivo precisa de banco. Os testes contra Postgres ficam em
`test_memory_store_postgres.py` e rodam no CI quando não há `ORQ_MEMORY_DSN` local.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _memory_vault import PROJECT, SOURCES, build_family_vault, rehash
from orq_contracts.canonical import canonical_digest
from orq_contracts.cli import main
from orq_contracts.knowledge_backend import scope_allows
from orq_contracts.memory_store import (
    DEFAULT_AUDIENCE,
    FAMILY_IDS,
    OFFLINE_BACKEND,
    POSTGRES_BACKEND,
    SHARED_GLOBAL_REGIME,
    MemoryConfig,
    MemoryScope,
    MemoryStoreError,
    build_sync_plan,
    resolve_memory_config,
)
from orq_contracts.memory_store.ddl import (
    ALL_MEMORY_ROLES,
    GLOBAL_READER_ROLE,
    MEMORY_ROLES,
    MEMORY_TABLES,
    READER_ROLE,
    SCHEMA_V2_VERSION,
    WRITER_ROLE,
    load_schema_sql,
    load_schema_v2_sql,
    schema_digest,
    schema_v2_digest,
)
from orq_contracts.schemas import load_schemas
from orq_contracts.selection import select_knowledge
from orq_contracts.vault import build_catalog_snapshot


ROOT = Path(__file__).resolve().parents[1]
VAULT = ROOT / "fixtures" / "v0.1" / "vault"
DSN = "postgresql://orq_admin:segredo-de-teste@127.0.0.1:5432/orq_memory"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def scope(**overrides: str) -> MemoryScope:
    values = {
        "catalog_id": "cat-7b",
        "project_id": PROJECT,
        "family_id": "family_codex",
        "role_id": "backend_engineer",
    }
    values.update(overrides)
    return MemoryScope(**values)


# --- configuração -----------------------------------------------------------------


def test_without_dsn_the_backend_is_offline() -> None:
    config = resolve_memory_config({})

    assert config == MemoryConfig(OFFLINE_BACKEND)
    assert config.offline and config.dsn is None


def test_blank_dsn_counts_as_missing() -> None:
    assert resolve_memory_config({"ORQ_MEMORY_DSN": "   "}).backend == OFFLINE_BACKEND


def test_dsn_selects_postgres() -> None:
    config = resolve_memory_config({"ORQ_MEMORY_DSN": DSN})

    assert config.backend == POSTGRES_BACKEND and config.dsn == DSN and not config.offline


def test_explicit_offline_wins_over_dsn_and_drops_it() -> None:
    config = resolve_memory_config({"ORQ_MEMORY_DSN": DSN, "ORQ_KNOWLEDGE_BACKEND": OFFLINE_BACKEND})

    assert config == MemoryConfig(OFFLINE_BACKEND) and config.dsn is None


def test_explicit_postgres_requires_dsn() -> None:
    with pytest.raises(MemoryStoreError) as error:
        resolve_memory_config({"ORQ_KNOWLEDGE_BACKEND": POSTGRES_BACKEND})
    assert error.value.code == "memory_dsn_missing"

    config = resolve_memory_config({"ORQ_KNOWLEDGE_BACKEND": POSTGRES_BACKEND, "ORQ_MEMORY_DSN": DSN})
    assert config.backend == POSTGRES_BACKEND


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(MemoryStoreError) as error:
        resolve_memory_config({"ORQ_KNOWLEDGE_BACKEND": "sqlite"})
    assert error.value.code == "invalid_knowledge_backend"


def test_config_repr_never_shows_the_dsn() -> None:
    config = resolve_memory_config({"ORQ_MEMORY_DSN": DSN})

    assert "segredo-de-teste" not in repr(config)
    assert "segredo-de-teste" not in str(config)


def test_reads_process_environment_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ORQ_MEMORY_DSN", raising=False)
    monkeypatch.delenv("ORQ_KNOWLEDGE_BACKEND", raising=False)
    assert resolve_memory_config().offline

    monkeypatch.setenv("ORQ_MEMORY_DSN", DSN)
    assert resolve_memory_config().backend == POSTGRES_BACKEND


# --- escopo -----------------------------------------------------------------------


def test_scope_settings_are_the_four_rls_variables_in_order() -> None:
    assert scope().settings() == [
        ("app.catalog_id", "cat-7b"),
        ("app.current_project_id", PROJECT),
        ("app.current_family_id", "family_codex"),
        ("app.current_role_id", "backend_engineer"),
    ]


def test_family_ids_match_the_common_schema() -> None:
    assert list(FAMILY_IDS) == load_schemas()["common"]["$defs"]["familyId"]["enum"]


@pytest.mark.parametrize(
    "field, value",
    [
        ("catalog_id", ""),
        ("project_id", "proj a"),
        ("role_id", "x'; SET LOCAL app.current_family_id = 'family_claude"),
        ("family_id", "family_unknown"),
        ("family_id", "family_claude:security_reviewer"),
        ("role_id", "r" * 129),
        ("project_id", None),
    ],
)
def test_scope_rejects_invalid_values(field: str, value) -> None:
    with pytest.raises(MemoryStoreError) as error:
        scope(**{field: value})
    assert error.value.code == "invalid_memory_scope"


def test_scope_is_immutable() -> None:
    fixed = scope()
    with pytest.raises(AttributeError):
        fixed.family_id = "family_claude"  # type: ignore[misc]


# --- DDL ----------------------------------------------------------------------------


def _statements(sql: str) -> str:
    # Remove comentários para que as asserções olhem só para o SQL executado.
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def test_ddl_enables_and_forces_rls_on_every_table() -> None:
    sql = _statements(load_schema_sql())

    for table in MEMORY_TABLES:
        assert f"ALTER TABLE orq_memory.{table} ENABLE ROW LEVEL SECURITY;" in sql
        assert f"ALTER TABLE orq_memory.{table} FORCE ROW LEVEL SECURITY;" in sql


def test_ddl_roles_never_get_bypass_or_superuser() -> None:
    sql = _statements(load_schema_sql())

    for role in MEMORY_ROLES:
        assert re.search(rf"ALTER ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS ", sql)
    assert re.search(r"(?<!NO)BYPASSRLS", sql) is None
    assert re.search(r"(?<!NO)SUPERUSER", sql) is None


def test_ddl_reader_only_gets_select_and_writer_select_insert() -> None:
    sql = " ".join(_statements(load_schema_sql()).split())
    grants = re.findall(r"GRANT ([A-Z, ]+) ON (?!SCHEMA)[^;]+ TO ([a-z_, ]+);", sql)

    assert ("SELECT", READER_ROLE) in grants
    assert ("SELECT, INSERT", WRITER_ROLE) in grants
    assert {privileges for privileges, role in grants if READER_ROLE in role} == {"SELECT"}
    assert "UPDATE" not in "".join(privileges for privileges, _ in grants)
    assert "DELETE" not in "".join(privileges for privileges, _ in grants)


def test_ddl_reader_policies_use_the_four_session_settings() -> None:
    sql = " ".join(_statements(load_schema_sql()).split())
    policies = re.findall(r"CREATE POLICY (\w+) ON orq_memory\.(\w+) (.*?);", sql)
    reader = {table: body for name, table, body in policies if name.endswith("_reader_scope")}

    assert set(reader) == set(MEMORY_TABLES)
    for table, body in reader.items():
        assert "FOR SELECT TO orq_memory_reader" in body
        for setting in ("app.catalog_id", "app.current_project_id", "app.current_family_id", "app.current_role_id"):
            assert f"current_setting('{setting}', true)" in body, (table, setting)
    for table in ("knowledge_sources", "knowledge_units"):
        body = reader[table]
        assert "resolved_status = 'active'" in body
        assert "area_scope = 'common'" in body
        assert "= ANY (target_families)" in body and "= ANY (target_roles)" in body
    # Só o writer tem política fora do leitor; o dono não tem nenhuma (FORCE RLS o bloqueia).
    assert {name for name, _table, _body in policies if not name.endswith("_reader_scope")} == {
        f"{table}_writer" for table in MEMORY_TABLES
    }
    assert all("TO orq_memory_owner" not in body for _name, _table, body in policies)


def test_ddl_creates_objects_as_the_non_bypass_owner() -> None:
    sql = _statements(load_schema_sql())

    owner_switch = sql.index("SET LOCAL ROLE orq_memory_owner;")
    assert owner_switch < sql.index("CREATE TABLE orq_memory.knowledge_catalogs")
    assert sql.index("CREATE EXTENSION IF NOT EXISTS pg_trgm;") < owner_switch
    assert sql.rstrip().endswith("RESET ROLE;")


def test_schema_digest_is_lf_sha256_of_the_ddl() -> None:
    raw = (ROOT / "src" / "orq_contracts" / "memory_store" / "schema.sql").read_bytes()

    assert b"\r" not in raw
    assert schema_digest() == hashlib.sha256(raw).hexdigest()


def test_ddl_has_no_python_placeholders() -> None:
    # O arquivo é executado sem parâmetros pelo protocolo simples; '%' seria ambíguo.
    assert "%" not in load_schema_sql()


# --- plano de sincronização ----------------------------------------------------------


@pytest.fixture
def family_vault(tmp_path) -> tuple[Path, dict]:
    return tmp_path, build_family_vault(tmp_path, "cat-7b")


def test_plan_matches_phase2_digests_of_the_fixture_vault() -> None:
    snapshot = build_catalog_snapshot(VAULT, load_json(VAULT / "catalog-definition.json"))
    plan = build_sync_plan(VAULT, snapshot)
    units = [unit for entry in snapshot["source_entries"] for unit in entry["units"]]

    assert len(plan.unit_rows) == len(units) == plan.catalog_row["unit_count"]
    for row, unit in zip(plan.unit_rows, units, strict=True):
        assert row["unit_id"] == unit["unit_id"]
        assert row["ordinal"] == unit["ordinal"]
        assert row["content_digest"] == unit["content_digest"]["value"]
        assert row["source_digest"] == unit["source_ref"]["content_digest"]["value"]
        assert hashlib.sha256(row["content"].encode("utf-8")).hexdigest() == row["content_digest"]
        assert len(row["content"].encode("utf-8")) == row["size_bytes"]
        assert row["unit_record"] == unit
        assert row["unit_record_digest"] == canonical_digest(unit)["value"]
    source = snapshot["source_entries"][0]["source_metadata"]
    assert plan.source_rows[0]["metadata_digest"] == canonical_digest(source)["value"]
    assert plan.catalog_row["catalog_hash"] == snapshot["catalog_hash"]["value"]


def test_plan_without_audience_defaults_to_common_area() -> None:
    snapshot = load_json(VAULT / "catalog-snapshot.json")
    plan = build_sync_plan(VAULT, snapshot)

    for row in [*plan.source_rows, *plan.unit_rows]:
        assert (row["area_scope"], row["target_families"], row["target_roles"]) == ("common", [], [])
    assert DEFAULT_AUDIENCE["area_scope"] == "common"


def test_plan_is_deterministic(family_vault) -> None:
    root, snapshot = family_vault

    first = build_sync_plan(root, snapshot)
    second = build_sync_plan(root, copy.deepcopy(snapshot))

    assert first == second
    assert first.sync_digest == second.sync_digest
    assert first.sync_digest == canonical_digest(
        {
            "catalog": {key: value for key, value in first.catalog_row.items() if key != "sync_digest"},
            "sources": first.source_rows,
            "units": first.unit_rows,
        }
    )["value"]


def test_plan_carries_both_audience_axes_and_unit_override(family_vault) -> None:
    root, snapshot = family_vault
    plan = build_sync_plan(root, snapshot)
    by_source = {}
    for row in plan.unit_rows:
        by_source.setdefault(row["source_id"], []).append(row)

    def axes(row: dict) -> tuple:
        return row["area_scope"], row["target_families"], row["target_roles"]

    assert axes(by_source["codex-only"][0]) == ("family", ["family_codex"], [])
    assert axes(by_source["claude-security"][0]) == ("family", ["family_claude"], ["security_reviewer"])
    assert axes(by_source["tester-only"][0]) == ("family", [], ["tester"])
    assert axes(by_source["global-ops"][0]) == ("common", [], [])
    assert by_source["global-ops"][0]["project_scope_kind"] == "global_operational"
    assert by_source["global-ops"][0]["project_id"] is None
    assert by_source["collection-src"][0]["collection_id"] == "shared-notes"
    # A fonte é comum, mas a 2ª unidade tem audiência própria só para Gemini.
    assert axes(by_source["common-project"][0]) == ("common", [], [])
    assert axes(by_source["common-project"][1]) == ("family", ["family_gemini"], [])
    assert len(plan.source_rows) == len(SOURCES)


def test_plan_rejects_changed_vault_bytes(family_vault) -> None:
    root, snapshot = family_vault
    (root / "content" / "codex-only.md").write_bytes(b"# Codex\n\nAlterado depois do snapshot.\n")

    with pytest.raises(MemoryStoreError) as error:
        build_sync_plan(root, snapshot)
    assert error.value.code == "catalog_sync_integrity_error"


def test_plan_rejects_tampered_catalog_hash(family_vault) -> None:
    root, snapshot = family_vault
    snapshot["catalog_hash"] = {"algorithm": "sha256", "value": "0" * 64}

    with pytest.raises(MemoryStoreError) as error:
        build_sync_plan(root, snapshot)
    assert error.value.code == "catalog_sync_integrity_error"


def test_plan_rejects_unit_digest_that_differs_from_the_chunker(family_vault) -> None:
    root, snapshot = family_vault
    snapshot["source_entries"][0]["units"][0]["content_digest"] = {"algorithm": "sha256", "value": "0" * 64}

    with pytest.raises(MemoryStoreError) as error:
        build_sync_plan(root, rehash(snapshot))
    assert error.value.code == "catalog_sync_integrity_error"


def test_plan_rejects_unit_span_changes(family_vault) -> None:
    root, snapshot = family_vault
    snapshot["source_entries"][1]["units"][1]["ordinal"] = 7

    with pytest.raises(MemoryStoreError) as error:
        build_sync_plan(root, rehash(snapshot))
    assert error.value.code == "catalog_sync_integrity_error"


def test_plan_rejects_invalid_audience_through_the_7a_schema(family_vault) -> None:
    root, snapshot = family_vault
    snapshot["source_entries"][0]["units"][0]["audience"] = {
        "schema_version": "0.1",
        "area_scope": "common",
        "target_families": ["family_codex"],
        "target_roles": [],
    }

    with pytest.raises(MemoryStoreError) as error:
        build_sync_plan(root, rehash(snapshot))
    assert error.value.code == "invalid_catalog_snapshot"


def test_plan_rejects_duplicate_source_revisions(family_vault) -> None:
    root, snapshot = family_vault
    snapshot["source_entries"].append(copy.deepcopy(snapshot["source_entries"][0]))

    with pytest.raises(MemoryStoreError) as error:
        build_sync_plan(root, rehash(snapshot))
    assert error.value.code == "catalog_sync_integrity_error"


# --- modo offline intacto -----------------------------------------------------------


def test_importing_the_package_and_memory_store_does_not_load_the_driver() -> None:
    code = (
        "import sys, orq_contracts, orq_contracts.cli, orq_contracts.memory_store;"
        "print(any(name == 'psycopg' or name.startswith('psycopg.') for name in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=True,
    )
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"ORQ_MEMORY_DSN": DSN},
        {"ORQ_MEMORY_DSN": DSN, "ORQ_KNOWLEDGE_BACKEND": OFFLINE_BACKEND},
    ],
)
def test_offline_vault_chain_ignores_memory_configuration(monkeypatch, environment) -> None:
    monkeypatch.delenv("ORQ_MEMORY_DSN", raising=False)
    monkeypatch.delenv("ORQ_KNOWLEDGE_BACKEND", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    snapshot = build_catalog_snapshot(VAULT, load_json(VAULT / "catalog-definition.json"))
    assert snapshot == load_json(VAULT / "catalog-snapshot.json")
    request = load_json(VAULT / "selection-request.json")
    request["allowed_collections"] = set(request.get("allowed_collections", []))
    selection = select_knowledge(VAULT, snapshot, **request)
    assert selection["selection_hash"] == canonical_digest(
        {key: value for key, value in selection.items() if key != "selection_hash"}
    )
    monkeypatch.delenv("ORQ_MEMORY_DSN", raising=False)
    monkeypatch.delenv("ORQ_KNOWLEDGE_BACKEND", raising=False)
    assert select_knowledge(VAULT, snapshot, **request) == selection


def test_sync_db_cli_without_dsn_fails_cleanly(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ORQ_MEMORY_DSN", raising=False)
    monkeypatch.delenv("ORQ_KNOWLEDGE_BACKEND", raising=False)

    code = main(
        ["vault", "sync-db", "--root", str(VAULT), "--catalog", str(VAULT / "catalog-snapshot.json")]
    )

    assert code == 2
    assert "memory_dsn_missing" in json.loads(capsys.readouterr().out)["error"]


def test_sync_db_cli_respects_forced_offline_backend(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ORQ_MEMORY_DSN", DSN)
    monkeypatch.setenv("ORQ_KNOWLEDGE_BACKEND", OFFLINE_BACKEND)

    code = main(
        ["vault", "sync-db", "--root", str(VAULT), "--catalog", str(VAULT / "catalog-snapshot.json")]
    )

    assert code == 2
    output = capsys.readouterr().out
    assert "memory_dsn_missing" in json.loads(output)["error"]
    assert "segredo-de-teste" not in output


def test_ddl_v2_creates_global_reader_without_family_filter() -> None:
    raw = (ROOT / "src" / "orq_contracts" / "memory_store" / "schema_v2.sql").read_bytes()
    assert b"\r" not in raw
    assert SCHEMA_V2_VERSION == 2
    assert schema_v2_digest() == hashlib.sha256(raw).hexdigest()
    assert "%" not in load_schema_v2_sql()

    sql = _statements(load_schema_v2_sql())
    assert GLOBAL_READER_ROLE in ALL_MEMORY_ROLES
    assert re.search(rf"ALTER ROLE {GLOBAL_READER_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS ", sql)
    assert re.search(r"(?<!NO)BYPASSRLS", sql) is None
    assert re.search(r"(?<!NO)SUPERUSER", sql) is None

    compact = " ".join(sql.split())
    grants = re.findall(r"GRANT ([A-Z, ]+) ON (?!SCHEMA)[^;]+ TO ([a-z_, ]+);", compact)
    assert ("SELECT", GLOBAL_READER_ROLE) in grants
    assert {privileges for privileges, role in grants if GLOBAL_READER_ROLE in role} == {"SELECT"}

    policies = re.findall(r"CREATE POLICY (\w+) ON orq_memory\.(\w+) (.*?);", compact)
    global_policies = {table: body for name, table, body in policies if name.endswith("_global_reader_scope")}
    assert set(global_policies) == set(MEMORY_TABLES)
    for table, body in global_policies.items():
        assert f"FOR SELECT TO {GLOBAL_READER_ROLE}" in body
        for setting in ("app.catalog_id", "app.current_project_id", "app.current_family_id", "app.current_role_id"):
            assert f"current_setting('{setting}', true)" in body, (table, setting)
        assert "target_families" not in body and "target_roles" not in body


def test_shared_global_regime_allows_cross_family_active_units_of_same_project(family_vault) -> None:
    _root, snapshot = family_vault
    codex_entry = next(e for e in snapshot["source_entries"] if e["source_metadata"]["source_id"] == "codex-only")
    claude_entry = next(e for e in snapshot["source_entries"] if e["source_metadata"]["source_id"] == "claude-only")
    partitioned = scope(family_id="family_claude")
    shared = scope(family_id="family_claude", memory_regime=SHARED_GLOBAL_REGIME)

    assert partitioned.settings() == shared.settings()
    assert not scope_allows(codex_entry, codex_entry["source_metadata"], codex_entry["units"][0], partitioned)
    assert scope_allows(codex_entry, codex_entry["source_metadata"], codex_entry["units"][0], shared)
    assert scope_allows(claude_entry, claude_entry["source_metadata"], claude_entry["units"][0], shared)
    with pytest.raises(MemoryStoreError, match="invalid_memory_scope"):
        scope(memory_regime="invalid_regime")
