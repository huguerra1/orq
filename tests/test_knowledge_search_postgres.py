"""Busca híbrida (7D) contra Postgres real: relevância em português/trigramas, RLS, escolha e manifesto.

Exige `ORQ_MEMORY_DSN` apontando para um banco descartável com login superusuário (como o
service container do CI e o `docker-compose.memory.yml`). Sem `ORQ_MEMORY_DSN`, o arquivo
inteiro é pulado e os testes rodam no CI. A validação da escolha, o manifesto com linhas
simuladas e o modo offline têm testes locais em `tests/test_knowledge_choice.py`.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    not (os.environ.get("ORQ_MEMORY_DSN") or "").strip(),
    reason="ORQ_MEMORY_DSN não definido: os testes de busca em Postgres rodam no CI com o service container postgres",
)

psycopg = pytest.importorskip("psycopg")

from _memory_vault import PROJECT  # noqa: E402
from _search_vault import (  # noqa: E402
    MATERIALIZE_ARGUMENTS,
    SESSION_ARGUMENTS,
    build_search_vault,
    requirement,
    unit_ref,
)
from orq_contracts.context import materialize_knowledge_context  # noqa: E402
from orq_contracts.knowledge_backend import (  # noqa: E402
    BACKEND_POSTGRES,
    EligibilityPolicy,
    KnowledgeCandidate,
    create_knowledge_backend,
)
from orq_contracts.knowledge_choice import (  # noqa: E402
    KnowledgeChoiceError,
    KnowledgeChoiceSession,
    materialize_chosen_context,
)
from orq_contracts.memory_store import (  # noqa: E402
    FTS_WEIGHT,
    TRGM_MIN_SIMILARITY,
    TRGM_WEIGHT,
    MemoryScope,
    MemoryStoreError,
    build_sync_plan,
    resolve_memory_config,
)
from orq_contracts.memory_store.knowledge import PostgresKnowledgeBackend  # noqa: E402
from orq_contracts.memory_store.postgres import (  # noqa: E402
    ScopedMemoryReader,
    apply_schema,
    connect,
    scoped_transaction,
    sync_catalog,
)
from orq_contracts.validator import validate_instance  # noqa: E402
from orq_contracts.vault import VaultError  # noqa: E402


POLICY = EligibilityPolicy(project_id=PROJECT, as_of="2026-11-14T12:00:00Z")


@pytest.fixture(scope="module")
def config():
    return resolve_memory_config({"ORQ_MEMORY_DSN": os.environ["ORQ_MEMORY_DSN"]})


@pytest.fixture(scope="module")
def admin(config):
    connection = connect(config)
    apply_schema(connection)
    yield connection
    connection.close()


def _delete_catalog(connection, catalog_id: str) -> None:
    with connection.transaction():
        for table in ("knowledge_units", "knowledge_sources", "knowledge_catalogs"):
            connection.execute(f"DELETE FROM orq_memory.{table} WHERE catalog_id = %s", (catalog_id,))


@pytest.fixture(scope="module")
def synced(admin, tmp_path_factory):
    root = tmp_path_factory.mktemp("search-vault")
    catalog = build_search_vault(root, f"cat-7d-{uuid.uuid4().hex[:12]}")
    plan = build_sync_plan(root, catalog)
    assert sync_catalog(admin, plan).status == "inserted"
    yield root, catalog, plan
    _delete_catalog(admin, catalog["catalog_id"])


def backend_for(admin, catalog, family: str, role: str = "backend_engineer", policy: EligibilityPolicy = POLICY):
    scope = MemoryScope(catalog_id=catalog["catalog_id"], project_id=PROJECT, family_id=family, role_id=role)
    return PostgresKnowledgeBackend(ScopedMemoryReader(admin, scope), catalog, policy)


def search(admin, catalog, family: str, query: str, limit: int = 20, req=None, role: str = "backend_engineer"):
    return backend_for(admin, catalog, family, role).search(req or requirement("K1"), query, limit)


def sources(candidates) -> list[str]:
    return [candidate.source_id for candidate in candidates]


def new_session(backend, requirements=None, **overrides) -> KnowledgeChoiceSession:
    return KnowledgeChoiceSession(backend, requirements or [requirement("K1")], **{**SESSION_ARGUMENTS, **overrides})


# --- relevância: FTS em português e trigramas (Q20) --------------------------------------------------


def test_fts_matches_portuguese_inflections(admin, synced) -> None:
    _root, catalog, _plan = synced

    found = search(admin, catalog, "family_gemini", "configurar")

    assert sources(found)[0] == "config"
    assert found[0].fts > 0  # casou pelo stemmer português (configurar × configuração), não só por trigramas


def test_fts_matches_plural_and_gender(admin, synced) -> None:
    _root, catalog, _plan = synced

    found = search(admin, catalog, "family_gemini", "tentativa expirado")

    assert sources(found)[0] == "recon"
    assert found[0].fts > 0


def test_trigram_matches_partial_code_identifier(admin, synced) -> None:
    _root, catalog, _plan = synced

    found = search(admin, catalog, "family_gemini", "QuotaStateRec")

    assert sources(found)[0] == "ident"
    assert found[0].trgm >= TRGM_MIN_SIMILARITY


def test_trigram_matches_cli_flag(admin, synced) -> None:
    _root, catalog, _plan = synced

    found = search(admin, catalog, "family_gemini", "--trust-tools=fs_read")

    assert sources(found)[0] == "recon"


def test_mixed_query_puts_the_stem_and_flag_unit_on_top(admin, synced) -> None:
    _root, catalog, _plan = synced

    found = search(admin, catalog, "family_gemini", "reconciliar tentativa --trust-tools")

    assert sources(found)[0] == "recon"
    assert found[0].score > 0


def test_score_follows_the_documented_formula(admin, synced) -> None:
    _root, catalog, _plan = synced

    found = search(admin, catalog, "family_gemini", "reconciliar tentativa --trust-tools configuração")

    assert found
    for candidate in found:
        assert candidate.score == pytest.approx(FTS_WEIGHT * candidate.fts + TRGM_WEIGHT * candidate.trgm, abs=2e-6)
        assert 0 <= candidate.fts < 1 and 0 <= candidate.trgm <= 1


def test_unrelated_query_returns_no_candidates(admin, synced) -> None:
    _root, catalog, _plan = synced

    assert search(admin, catalog, "family_gemini", "zzzxqwk") == []


def test_order_is_deterministic_and_follows_the_tie_break(admin, synced) -> None:
    _root, catalog, _plan = synced
    query = "reconciliação configuração pool commits"

    first = search(admin, catalog, "family_codex", query)
    second = search(admin, catalog, "family_codex", query)

    assert first == second
    assert first == sorted(first, key=KnowledgeCandidate.tie_break)


def test_limit_is_respected_and_capped_at_twenty(admin, synced) -> None:
    _root, catalog, _plan = synced
    query = "reconciliação configuração pool commits"

    assert len(search(admin, catalog, "family_codex", query, 1)) == 1
    with pytest.raises(MemoryStoreError) as raised:
        search(admin, catalog, "family_codex", query, 21)
    assert raised.value.code == "invalid_search_limit"


def test_catalog_requirement_filters_apply_before_ranking(admin, synced) -> None:
    _root, catalog, _plan = synced
    strict = requirement("K1")
    strict["required_authority_scopes"] = ["autoridade_inexistente"]

    assert search(admin, catalog, "family_gemini", "reconciliação", req=strict) == []
    assert search(admin, catalog, "family_gemini", "reconciliação") != []


# --- isolamento por família (Q15, Q17) ------------------------------------------------------------------


def test_search_never_returns_another_familys_exclusive_units(admin, synced) -> None:
    _root, catalog, _plan = synced
    query = "reconciliação sandbox permissões configuração pool Codex Claude"

    codex = set(sources(search(admin, catalog, "family_codex", query)))
    claude = set(sources(search(admin, catalog, "family_claude", query)))
    gemini = set(sources(search(admin, catalog, "family_gemini", query)))

    assert "codex-only" in codex and "claude-only" not in codex
    assert "claude-only" in claude and "codex-only" not in claude
    assert not {"codex-only", "claude-only"} & gemini


def test_exclusive_unit_text_does_not_leak_through_excerpts(admin, synced) -> None:
    _root, catalog, _plan = synced

    found = search(admin, catalog, "family_claude", "sandbox exclusivas Codex threads")

    assert all("Codex" not in candidate.excerpt for candidate in found)


@pytest.mark.parametrize(
    "hostile",
    [
        "'); DROP TABLE orq_memory.knowledge_units; --",
        "x'; SET LOCAL app.current_family_id = 'family_claude'; --",
        "\" OR 1=1 --",
        "reconciliação'; SELECT set_config('app.current_family_id', 'family_claude', true); --",
        "$$; DELETE FROM orq_memory.knowledge_units; $$",
    ],
)
def test_sql_injection_in_query_has_no_effect(admin, synced, hostile) -> None:
    _root, catalog, plan = synced

    found = search(admin, catalog, "family_codex", hostile)

    assert not {"claude-only"} & set(sources(found))
    with scoped_transaction(
        admin, MemoryScope(plan.catalog_id, PROJECT, "family_codex", "backend_engineer")
    ) as cursor:
        cursor.execute("SELECT count(*) AS total FROM orq_memory.knowledge_units")
        assert cursor.fetchone()["total"] == 5  # recon, config, ident, git e codex-only
        assert cursor.execute("SELECT current_setting('app.current_family_id') AS family").fetchone()["family"] == (
            "family_codex"
        )
    total = admin.execute(
        "SELECT count(*) FROM orq_memory.knowledge_units WHERE catalog_id = %s", (plan.catalog_id,)
    ).fetchone()[0]
    assert total == len(plan.unit_rows)


def test_scope_cannot_be_changed_by_the_search_arguments(admin, synced) -> None:
    _root, catalog, _plan = synced
    backend = backend_for(admin, catalog, "family_codex")

    with pytest.raises(TypeError):
        backend.search(requirement("K1"), "reconciliação", 5, family_id="family_claude")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        backend.reader.search_units([], "x", 5, family_id="family_claude")  # type: ignore[call-arg]


def test_backend_rejects_catalog_or_project_of_another_scope(admin, synced) -> None:
    _root, catalog, _plan = synced
    other_scope = MemoryScope("cat-outro", PROJECT, "family_codex", "backend_engineer")
    with pytest.raises(MemoryStoreError) as wrong_catalog:
        PostgresKnowledgeBackend(ScopedMemoryReader(admin, other_scope), catalog, POLICY)
    assert wrong_catalog.value.code == "catalog_scope_mismatch"

    scope = MemoryScope(catalog["catalog_id"], PROJECT, "family_codex", "backend_engineer")
    with pytest.raises(MemoryStoreError) as wrong_project:
        PostgresKnowledgeBackend(
            ScopedMemoryReader(admin, scope), catalog, EligibilityPolicy(project_id="outro", as_of=POLICY.as_of)
        )
    assert wrong_project.value.code == "project_scope_mismatch"


def test_search_from_another_catalog_scope_finds_nothing(admin, synced) -> None:
    _root, catalog, _plan = synced
    scope = MemoryScope("cat-inexistente", PROJECT, "family_codex", "backend_engineer")
    reader = ScopedMemoryReader(admin, scope)
    eligible = [("recon", "1", "qualquer")]

    assert reader.search_units(eligible, "reconciliação", 5) == []


# --- escolha pelo agente (Q24, Q25) -------------------------------------------------------------------------------


def test_choice_of_unit_from_another_family_is_rejected(admin, synced) -> None:
    _root, catalog, _plan = synced
    choice = new_session(backend_for(admin, catalog, "family_claude"))
    choice.search("K1", "reconciliação", 20)

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose({"K1": [unit_ref("codex-only", catalog)]})

    assert raised.value.code == "unit_not_in_search_results"


def test_choice_of_id_not_returned_by_the_search_is_rejected(admin, synced) -> None:
    _root, catalog, _plan = synced
    choice = new_session(backend_for(admin, catalog, "family_codex"))
    choice.search("K1", "reconciliação", 1)

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose({"K1": [unit_ref("git", catalog)]})  # visível ao Codex, mas fora dos resultados

    assert raised.value.code == "unit_not_in_search_results"


def test_valid_choice_produces_a_valid_record_with_backend_fields(admin, synced) -> None:
    _root, catalog, _plan = synced
    choice = new_session(backend_for(admin, catalog, "family_codex"))
    choice.search("K1", "reconciliar tentativa --trust-tools", 20)

    record = choice.choose({"K1": [unit_ref("recon", catalog)]})

    assert validate_instance(record, "knowledge_selection") == []
    assert (record["backend"], record["selection_mode"], record["ranking_version"]) == (
        BACKEND_POSTGRES,
        "agent_mcp_choice",
        "pg-fts-trgm-v1",
    )
    assert record["status"] == "satisfied"
    candidate_sources = {item["unit_ref"]["source_id"] for item in record["candidates"]}
    assert "claude-only" not in candidate_sources


def test_mandatory_requirement_and_budget_are_enforced_without_silent_truncation(admin, synced) -> None:
    _root, catalog, _plan = synced
    backend = backend_for(admin, catalog, "family_codex")

    uncovered = new_session(backend, [requirement("K1"), requirement("K2", "pool")])
    uncovered.search("K1", "reconciliação", 20)
    uncovered.search("K2", "configuração pool", 20)
    with pytest.raises(KnowledgeChoiceError) as raised:
        uncovered.choose({"K1": [unit_ref("recon", catalog)]})
    assert raised.value.code == "mandatory_requirement_uncovered"

    tight = new_session(backend, max_tokens=5)
    tight.search("K1", "reconciliação", 20)
    with pytest.raises(KnowledgeChoiceError) as exceeded:
        tight.choose({"K1": [unit_ref("recon", catalog)]})
    assert exceeded.value.code == "context_budget_exceeded"
    assert tight.selection is None


# --- manifesto e bundle reproduzíveis (Q24, Q26) ----------------------------------------------------------------------


def chosen_record(admin, catalog, family: str, order=("recon", "config")):
    backend = backend_for(admin, catalog, family)
    choice = new_session(backend, [requirement("K1"), requirement("K2", "pool", required=False)])
    choice.search("K1", "reconciliar tentativa --trust-tools", 20)
    choice.search("K2", "configurar pool", 20)
    refs = [unit_ref(name, catalog) for name in order]
    return backend, choice.choose({"K1": refs[:1], "K2": refs[1:]})


def test_manifest_and_bundle_are_byte_reproducible_from_postgres_units(admin, synced) -> None:
    root, catalog, _plan = synced
    backend_a, record_a = chosen_record(admin, catalog, "family_codex")
    backend_b, record_b = chosen_record(admin, catalog, "family_codex")

    first = materialize_chosen_context(backend_a, record_a, **MATERIALIZE_ARGUMENTS)
    second = materialize_chosen_context(backend_b, record_b, **MATERIALIZE_ARGUMENTS)
    from_vault = materialize_knowledge_context(root, catalog, record_a, **MATERIALIZE_ARGUMENTS)

    assert record_a == record_b
    assert first.bundle == second.bundle == from_vault.bundle
    assert first.manifest == second.manifest == from_vault.manifest
    assert [item["knowledge_provenance"]["unit_ref"]["source_id"] for item in first.manifest["items"]] == [
        "recon",
        "config",
    ]


def test_fallback_to_another_family_builds_a_new_manifest_without_the_old_exclusive_units(admin, synced) -> None:
    _root, catalog, _plan = synced
    codex_backend = backend_for(admin, catalog, "family_codex")
    codex_choice = new_session(codex_backend, selection_id="selection-cm-1")
    codex_choice.search("K1", "reconciliação sandbox", 20)
    codex_record = codex_choice.choose({"K1": [unit_ref("codex-only", catalog)]})
    codex_context = materialize_chosen_context(
        codex_backend, codex_record, context_manifest_id="cm-1", **MATERIALIZE_ARGUMENTS
    )

    claude_backend = backend_for(admin, catalog, "family_claude")
    with pytest.raises(VaultError) as raised:  # o manifesto cm-1 não é reaproveitável sob o RLS da outra família
        materialize_chosen_context(claude_backend, codex_record, context_manifest_id="cm-2", **MATERIALIZE_ARGUMENTS)
    claude_choice = new_session(claude_backend, selection_id="selection-cm-2", candidate_attempt_number=2)
    claude_choice.search("K1", "reconciliação", 20)
    claude_record = claude_choice.choose({"K1": [unit_ref("claude-only", catalog)]})
    claude_context = materialize_chosen_context(
        claude_backend, claude_record, context_manifest_id="cm-2", **MATERIALIZE_ARGUMENTS
    )

    assert raised.value.code == "selected_unit_missing"
    assert codex_context.manifest["context_manifest_id"] == "cm-1"
    assert claude_context.manifest["context_manifest_id"] == "cm-2"
    assert b"Codex" in codex_context.bundle and b"Codex" not in claude_context.bundle


# --- integridade (Q27) ---------------------------------------------------------------------------------------------------


def test_bytes_changed_in_the_database_block_materialization(admin, tmp_path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    catalog = build_search_vault(root, f"cat-7d-int-{uuid.uuid4().hex[:10]}")
    plan = build_sync_plan(root, catalog)
    assert sync_catalog(admin, plan).status == "inserted"
    try:
        backend = backend_for(admin, catalog, "family_gemini")
        choice = new_session(backend)
        choice.search("K1", "commits revisados", 20)
        record = choice.choose({"K1": [unit_ref("git", catalog)]})

        altered = "# Git\n\nConteúdo alterado depois do snapshot.\n"
        admin.execute(
            "UPDATE orq_memory.knowledge_units SET content = %s, content_digest = %s, size_bytes = %s"
            " WHERE catalog_id = %s AND source_id = 'git'",
            (
                altered,
                hashlib.sha256(altered.encode("utf-8")).hexdigest(),
                len(altered.encode("utf-8")),
                catalog["catalog_id"],
            ),
        )

        with pytest.raises(VaultError) as raised:
            materialize_chosen_context(backend, record, **MATERIALIZE_ARGUMENTS)
        assert raised.value.code == "unit_integrity_mismatch"

        later = new_session(backend_for(admin, catalog, "family_gemini"))
        with pytest.raises(KnowledgeChoiceError) as at_search:
            # "Git" continua no cabeçalho adulterado, então a unidade ainda casa na busca.
            later.search("K1", "Git", 20)
        assert at_search.value.code == "integrity_mismatch"
    finally:
        _delete_catalog(admin, catalog["catalog_id"])


# --- fábrica de backend --------------------------------------------------------------------------------------------------


def test_factory_returns_the_postgres_backend_when_a_dsn_is_configured(admin, config, synced) -> None:
    _root, catalog, _plan = synced
    scope = MemoryScope(catalog["catalog_id"], PROJECT, "family_codex", "backend_engineer")

    backend = create_knowledge_backend(config, catalog=catalog, policy=POLICY, scope=scope, connection=admin)

    assert isinstance(backend, PostgresKnowledgeBackend)
    assert backend.backend_id == BACKEND_POSTGRES
