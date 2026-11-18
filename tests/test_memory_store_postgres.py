"""Memória por família (7B) contra Postgres real: RLS, papéis, paridade de digests e idempotência.

Exige `ORQ_MEMORY_DSN` apontando para um banco descartável com login superusuário (como o
service container do CI e o `docker-compose.memory.yml`): a migração cria roles do cluster.
Sem `ORQ_MEMORY_DSN`, o arquivo inteiro é pulado e os testes rodam no CI.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    not (os.environ.get("ORQ_MEMORY_DSN") or "").strip(),
    reason="ORQ_MEMORY_DSN não definido: os testes de Postgres/RLS rodam no CI com o service container postgres",
)

psycopg = pytest.importorskip("psycopg")

from _memory_vault import OTHER_PROJECT, PROJECT, build_family_vault  # noqa: E402
from orq_contracts.canonical import canonical_digest  # noqa: E402
from orq_contracts.memory_store import (  # noqa: E402
    SHARED_GLOBAL_REGIME,
    MemoryScope,
    MemoryStoreError,
    build_sync_plan,
    resolve_memory_config,
)
from orq_contracts.memory_store.ddl import (  # noqa: E402
    ALL_MEMORY_ROLES,
    GLOBAL_READER_ROLE,
    MEMORY_ROLES,
    MEMORY_TABLES,
    OWNER_ROLE,
    READER_ROLE,
    SCHEMA_V2_VERSION,
    SCHEMA_VERSION,
    schema_digest,
    schema_v2_digest,
)
from orq_contracts.memory_store.postgres import (  # noqa: E402
    ScopedMemoryReader,
    apply_schema,
    connect,
    scoped_transaction,
    sync_catalog,
)


FAMILIES = ("family_claude", "family_codex", "family_gemini", "family_kiro")


@pytest.fixture(scope="module")
def config():
    resolved = resolve_memory_config({"ORQ_MEMORY_DSN": os.environ["ORQ_MEMORY_DSN"]})
    return resolved


@pytest.fixture(scope="module")
def admin(config):
    connection = connect(config)
    apply_schema(connection)
    yield connection
    connection.close()


def _delete_catalog(connection, catalog_id: str) -> None:
    # Limpeza com o login administrativo (superusuário ignora RLS); o writer não tem DELETE.
    with connection.transaction():
        for table in ("knowledge_units", "knowledge_sources", "knowledge_catalogs"):
            connection.execute(f"DELETE FROM orq_memory.{table} WHERE catalog_id = %s", (catalog_id,))


@pytest.fixture(scope="module")
def synced(admin, tmp_path_factory):
    root = tmp_path_factory.mktemp("family-vault")
    catalog_id = f"cat-7b-{uuid.uuid4().hex[:12]}"
    snapshot = build_family_vault(root, catalog_id)
    plan = build_sync_plan(root, snapshot)
    result = sync_catalog(admin, plan)
    assert result.status == "inserted"
    yield root, snapshot, plan
    _delete_catalog(admin, catalog_id)


def make_scope(plan, family: str, role: str = "backend_engineer", project: str = PROJECT) -> MemoryScope:
    return MemoryScope(catalog_id=plan.catalog_id, project_id=project, family_id=family, role_id=role)


def visible(admin, plan, family: str, role: str = "backend_engineer", project: str = PROJECT) -> set:
    reader = ScopedMemoryReader(admin, make_scope(plan, family, role, project))
    return {(row["source_id"], row["ordinal"]) for row in reader.list_units()}


def sources_of(units: set) -> set:
    return {source for source, _ordinal in units}


# --- migração ---------------------------------------------------------------------------


def test_schema_is_versioned_and_idempotent(admin) -> None:
    assert apply_schema(admin) == "unchanged"
    rows = admin.execute("SELECT version, ddl_digest FROM orq_memory.schema_migrations ORDER BY version").fetchall()
    assert [tuple(row) for row in rows] == [
        (SCHEMA_VERSION, schema_digest()),
        (SCHEMA_V2_VERSION, schema_v2_digest()),
    ]
    assert admin.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'").fetchone() is not None


def test_roles_have_no_superuser_nor_bypass(admin) -> None:
    rows = admin.execute(
        "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = ANY(%s)",
        (list(ALL_MEMORY_ROLES),),
    ).fetchall()

    assert {row[0] for row in rows} == set(ALL_MEMORY_ROLES)
    for _name, superuser, bypass, can_login in rows:
        assert (superuser, bypass, can_login) == (False, False, False)


def test_rls_is_enabled_and_forced_and_tables_belong_to_the_owner(admin) -> None:
    rows = admin.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner)"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'orq_memory' AND c.relname = ANY(%s)",
        (list(MEMORY_TABLES),),
    ).fetchall()

    assert {row[0] for row in rows} == set(MEMORY_TABLES)
    for _name, enabled, forced, owner in rows:
        assert (enabled, forced, owner) == (True, True, OWNER_ROLE)


# --- isolamento entre famílias (Q15) e área comum ------------------------------------------


def test_codex_sees_common_and_own_units_only(admin, synced) -> None:
    _root, _snapshot, plan = synced
    with scoped_transaction(admin, make_scope(plan, "family_codex")) as cursor:
        # SELECT sem nenhum filtro de família: o RLS decide sozinho.
        cursor.execute("SELECT source_id, ordinal FROM orq_memory.knowledge_units")
        units = {(row["source_id"], row["ordinal"]) for row in cursor.fetchall()}

    assert units == {("common-project", 0), ("codex-only", 0), ("codex-only", 1), ("global-ops", 0)}
    assert not any(source.startswith("claude") for source in sources_of(units))


def test_claude_never_reads_codex_exclusive_units(admin, synced) -> None:
    _root, _snapshot, plan = synced
    units = visible(admin, plan, "family_claude")

    assert units == {("common-project", 0), ("claude-only", 0), ("global-ops", 0)}
    assert "codex-only" not in sources_of(units)


def test_exclusive_unit_is_not_reachable_by_key_from_another_family(admin, synced) -> None:
    _root, _snapshot, plan = synced
    codex_row = next(row for row in plan.unit_rows if row["source_id"] == "codex-only")
    key = (codex_row["source_id"], codex_row["revision"], codex_row["unit_id"])

    with ScopedMemoryReader(connect(resolve_memory_config()), make_scope(plan, "family_claude")) as reader:
        assert reader.get_unit(*key) is None
    codex_reader = ScopedMemoryReader(admin, make_scope(plan, "family_codex"))
    assert codex_reader.get_unit(*key)["content_digest"] == codex_row["content_digest"]


def test_sources_and_catalog_tables_follow_the_same_scope(admin, synced) -> None:
    _root, _snapshot, plan = synced
    with scoped_transaction(admin, make_scope(plan, "family_codex")) as cursor:
        cursor.execute("SELECT source_id FROM orq_memory.knowledge_sources")
        sources = {row["source_id"] for row in cursor.fetchall()}
        cursor.execute("SELECT catalog_id FROM orq_memory.knowledge_catalogs")
        catalogs = [row["catalog_id"] for row in cursor.fetchall()]

    assert sources == {"common-project", "codex-only", "global-ops"}
    assert catalogs == [plan.catalog_id]


@pytest.mark.parametrize("family", FAMILIES)
def test_common_area_is_visible_to_every_family(admin, synced, family) -> None:
    _root, _snapshot, plan = synced
    units = visible(admin, plan, family, role="software_architect")

    assert {("common-project", 0), ("global-ops", 0)} <= units


def test_unit_level_audience_overrides_the_source(admin, synced) -> None:
    _root, _snapshot, plan = synced

    assert ("common-project", 1) in visible(admin, plan, "family_gemini")
    for family in ("family_claude", "family_codex", "family_kiro"):
        assert ("common-project", 1) not in visible(admin, plan, family)


# --- eixo de papel (Q16) -----------------------------------------------------------------------


def test_family_and_role_restriction_requires_both_axes(admin, synced) -> None:
    _root, _snapshot, plan = synced

    assert "claude-security" not in sources_of(visible(admin, plan, "family_claude", "backend_engineer"))
    assert "claude-security" in sources_of(visible(admin, plan, "family_claude", "security_reviewer"))
    assert "claude-security" not in sources_of(visible(admin, plan, "family_codex", "security_reviewer"))


def test_role_only_audience_is_visible_to_any_family_with_that_role(admin, synced) -> None:
    _root, _snapshot, plan = synced

    for family in FAMILIES:
        assert "tester-only" in sources_of(visible(admin, plan, family, "tester"))
        assert "tester-only" not in sources_of(visible(admin, plan, family, "backend_engineer"))


# --- projeto, catálogo e escopo ausente --------------------------------------------------------


def test_project_isolation_and_global_operational(admin, synced) -> None:
    _root, _snapshot, plan = synced
    units = visible(admin, plan, "family_codex", project=OTHER_PROJECT)

    assert sources_of(units) == {"other-project", "global-ops"}


def test_collection_scoped_sources_are_not_visible_in_7b(admin, synced) -> None:
    _root, _snapshot, plan = synced

    for family in FAMILIES:
        assert "collection-src" not in sources_of(visible(admin, plan, family))


def test_other_catalog_id_reads_nothing(admin, synced) -> None:
    _root, _snapshot, plan = synced
    other = MemoryScope(
        catalog_id="cat-7b-inexistente", project_id=PROJECT, family_id="family_codex", role_id="backend_engineer"
    )

    assert ScopedMemoryReader(admin, other).list_units() == []


def _count_as_reader(admin, settings: dict[str, str]) -> dict[str, int]:
    counts = {}
    with admin.transaction():
        admin.execute(f"SET LOCAL ROLE {READER_ROLE}")
        for name, value in settings.items():
            admin.execute("SELECT set_config(%s, %s, true)", (name, value))
        for table in MEMORY_TABLES:
            counts[table] = admin.execute(f"SELECT count(*) FROM orq_memory.{table}").fetchone()[0]
    return counts


def test_reader_without_scope_reads_nothing(admin, synced) -> None:
    assert _count_as_reader(admin, {}) == {table: 0 for table in MEMORY_TABLES}


@pytest.mark.parametrize(
    "missing", ["app.catalog_id", "app.current_project_id", "app.current_family_id", "app.current_role_id"]
)
def test_reader_with_incomplete_scope_reads_nothing(admin, synced, missing) -> None:
    _root, _snapshot, plan = synced
    settings = dict(make_scope(plan, "family_codex").settings())
    settings[missing] = ""

    assert _count_as_reader(admin, settings) == {table: 0 for table in MEMORY_TABLES}


def test_scope_does_not_leak_after_the_transaction(admin, synced) -> None:
    _root, _snapshot, plan = synced
    assert visible(admin, plan, "family_codex")

    assert _count_as_reader(admin, {}) == {table: 0 for table in MEMORY_TABLES}
    value = admin.execute("SELECT current_setting('app.current_family_id', true)").fetchone()[0]
    assert value in (None, "")


# --- sem bypass e FORCE RLS --------------------------------------------------------------------


def test_reader_cannot_disable_row_security(admin, synced) -> None:
    _root, _snapshot, plan = synced
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with admin.transaction():
            admin.execute(f"SET LOCAL ROLE {READER_ROLE}")
            admin.execute("SET LOCAL row_security = off")
            for name, value in make_scope(plan, "family_codex").settings():
                admin.execute("SELECT set_config(%s, %s, true)", (name, value))
            admin.execute("SELECT count(*) FROM orq_memory.knowledge_units").fetchone()


def test_reader_cannot_write(admin, synced) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with admin.transaction():
            admin.execute(f"SET LOCAL ROLE {READER_ROLE}")
            admin.execute(
                "INSERT INTO orq_memory.knowledge_catalogs VALUES"
                " ('cat-x', '1', %s, 't', 't', 0, 0, %s)",
                ("0" * 64, "0" * 64),
            )


def test_scoped_transaction_is_read_only_and_runs_as_reader(admin, synced) -> None:
    _root, _snapshot, plan = synced
    with scoped_transaction(admin, make_scope(plan, "family_codex")) as cursor:
        cursor.execute(
            "SELECT current_setting('transaction_read_only') AS read_only, current_user AS who,"
            " current_setting('app.current_family_id') AS family"
        )
        row = cursor.fetchone()

    assert row == {"read_only": "on", "who": READER_ROLE, "family": "family_codex"}


def test_force_rls_applies_to_the_table_owner(admin, synced) -> None:
    _root, _snapshot, plan = synced
    with admin.transaction():
        admin.execute(f"SET LOCAL ROLE {OWNER_ROLE}")
        for name, value in make_scope(plan, "family_codex").settings():
            admin.execute("SELECT set_config(%s, %s, true)", (name, value))
        for table in MEMORY_TABLES:
            assert admin.execute(f"SELECT count(*) FROM orq_memory.{table}").fetchone()[0] == 0


def test_scope_values_are_bound_parameters_not_sql(admin, synced) -> None:
    _root, _snapshot, plan = synced
    reader = ScopedMemoryReader(admin, make_scope(plan, "family_kiro"))
    # A busca recebe texto do agente como parâmetro; mesmo um "SET LOCAL" literal não muda o escopo.
    injected = "x' OR true; SET LOCAL app.current_family_id = 'family_claude'; --"

    assert reader.get_unit(injected, "1", injected) is None
    assert "claude-only" not in sources_of(visible(admin, plan, "family_kiro"))


# --- paridade de digests e idempotência (Q18) ---------------------------------------------------


def test_database_digests_match_the_vault(admin, synced) -> None:
    _root, snapshot, plan = synced
    rows = admin.execute(
        "SELECT source_id, revision, unit_id, ordinal, content_digest, source_digest, unit_record,"
        " unit_record_digest, encode(sha256(convert_to(content, 'UTF8')), 'hex')"
        " FROM orq_memory.knowledge_units WHERE catalog_id = %s",
        (plan.catalog_id,),
    ).fetchall()
    stored = {(row[0], row[1], row[2]): row for row in rows}
    units = [
        (entry["source_metadata"], unit) for entry in snapshot["source_entries"] for unit in entry["units"]
    ]

    assert len(stored) == len(units)
    for source, unit in units:
        row = stored[(source["source_id"], source["revision"], unit["unit_id"])]
        assert row[3] == unit["ordinal"]
        assert row[4] == row[8] == unit["content_digest"]["value"]
        assert row[5] == source["content_digest"]["value"]
        assert canonical_digest(row[6])["value"] == row[7] == canonical_digest(unit)["value"]


def test_source_metadata_digest_survives_jsonb(admin, synced) -> None:
    _root, snapshot, plan = synced
    rows = admin.execute(
        "SELECT source_id, source_metadata, metadata_digest FROM orq_memory.knowledge_sources"
        " WHERE catalog_id = %s",
        (plan.catalog_id,),
    ).fetchall()
    expected = {
        entry["source_metadata"]["source_id"]: canonical_digest(entry["source_metadata"])["value"]
        for entry in snapshot["source_entries"]
    }

    assert {row[0]: canonical_digest(row[1])["value"] for row in rows} == expected
    assert {row[0]: row[2] for row in rows} == expected


def test_resync_is_idempotent_without_duplicates(admin, synced) -> None:
    root, snapshot, plan = synced

    again = sync_catalog(admin, build_sync_plan(root, snapshot))

    assert again.status == "unchanged"
    assert again.sync_digest == plan.sync_digest
    count = admin.execute(
        "SELECT count(*) FROM orq_memory.knowledge_units WHERE catalog_id = %s", (plan.catalog_id,)
    ).fetchone()[0]
    assert count == len(plan.unit_rows)


def test_resync_with_different_content_for_same_catalog_is_rejected(admin, synced, tmp_path: Path) -> None:
    _root, _snapshot, plan = synced
    changed = build_family_vault(
        tmp_path, plan.catalog_id, overrides={"codex-only": "# Codex\n\nOutro conteúdo.\n"}
    )

    with pytest.raises(MemoryStoreError) as error:
        sync_catalog(admin, build_sync_plan(tmp_path, changed))
    assert error.value.code == "catalog_sync_integrity_error"
    count = admin.execute(
        "SELECT count(*) FROM orq_memory.knowledge_units WHERE catalog_id = %s", (plan.catalog_id,)
    ).fetchone()[0]
    assert count == len(plan.unit_rows)


def test_reader_rows_carry_vault_digests(admin, synced) -> None:
    _root, _snapshot, plan = synced
    expected = {(row["source_id"], row["unit_id"]): row["content_digest"] for row in plan.unit_rows}

    for row in ScopedMemoryReader(admin, make_scope(plan, "family_codex")).list_units():
        assert row["content_digest"] == expected[(row["source_id"], row["unit_id"])]


def test_shared_global_reader_sees_cross_family_units_in_same_catalog_and_project(admin, synced) -> None:
    _root, _snapshot, plan = synced
    shared_scope = MemoryScope(
        catalog_id=plan.catalog_id,
        project_id=PROJECT,
        family_id="family_codex",
        role_id="backend_engineer",
        memory_regime=SHARED_GLOBAL_REGIME,
    )
    partitioned = {row["source_id"] for row in ScopedMemoryReader(admin, make_scope(plan, "family_codex")).list_units()}
    shared = {row["source_id"] for row in ScopedMemoryReader(admin, shared_scope).list_units()}

    assert "claude-only" not in partitioned
    assert {"codex-only", "claude-only", "common-rule", "global-ops"} <= shared
    assert {"other-project", "deprecated-doc", "draft-doc"} & shared == set()
    with scoped_transaction(admin, shared_scope) as cursor:
        cursor.execute("SELECT current_user AS who")
        assert cursor.fetchone()["who"] == GLOBAL_READER_ROLE
