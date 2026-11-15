"""Busca, escolha pelo agente e consolidação em ContextManifest (7D), sem banco.

Cobre localmente o que não depende de Postgres: validação da escolha, orçamento sem truncar,
reprodutibilidade byte a byte, montagem do manifesto a partir de linhas simuladas do banco,
integridade, normalização da query e modo offline. A relevância e o RLS reais ficam em
`tests/test_knowledge_search_postgres.py`.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from _memory_vault import PROJECT
from _search_vault import (
    MATERIALIZE_ARGUMENTS,
    SESSION_ARGUMENTS,
    build_search_vault,
    requirement,
    unit_ref,
)
from orq_contracts.canonical import canonical_digest
from orq_contracts.context import ContextError, materialize_knowledge_context
from orq_contracts.knowledge_backend import (
    BACKEND_OFFLINE,
    BACKEND_POSTGRES,
    EligibilityPolicy,
    KnowledgeCandidate,
    OfflineMarkdownBackend,
    create_knowledge_backend,
    eligible_keys,
    scope_allows,
)
from orq_contracts.knowledge_choice import (
    KnowledgeChoiceError,
    KnowledgeChoiceSession,
    materialize_chosen_context,
)
from orq_contracts.memory_store import (
    ENV_KNOWLEDGE_BACKEND,
    ENV_MEMORY_DSN,
    FTS_WEIGHT,
    RANKING_VERSION,
    TRGM_WEIGHT,
    MemoryScope,
    MemoryStoreError,
    normalize_search_query,
    resolve_memory_config,
)
from orq_contracts.memory_store.search import (
    SEARCH_SQL,
    candidate_from_row,
    search_parameters,
    validate_search_limit,
)
from orq_contracts.selection import _estimated_tokens
from orq_contracts.validator import validate_instance
from orq_contracts.vault import VaultError


CATALOG_ID = "cat-7d-local"
POLICY = EligibilityPolicy(project_id=PROJECT, as_of="2026-11-14T12:00:00Z")


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root, build_search_vault(root, CATALOG_ID)


def scope_for(family: str, role: str = "backend_engineer") -> MemoryScope:
    return MemoryScope(catalog_id=CATALOG_ID, project_id=PROJECT, family_id=family, role_id=role)


def offline(vault, family: str = "family_codex", role: str = "backend_engineer") -> OfflineMarkdownBackend:
    root, catalog = vault
    return OfflineMarkdownBackend(root, catalog, POLICY, scope_for(family, role))


def session(backend, requirements=None, **overrides) -> KnowledgeChoiceSession:
    arguments = {**SESSION_ARGUMENTS, **overrides}
    return KnowledgeChoiceSession(backend, requirements or [requirement("K1")], **arguments)


class FakePostgresBackend:
    """Backend com linhas simuladas no formato do SQL do 7D (sem banco), sob o mesmo filtro do RLS."""

    backend_id = BACKEND_POSTGRES
    ranking_version = RANKING_VERSION

    def __init__(self, vault, scope: MemoryScope) -> None:
        self.root, self.catalog = vault
        self.policy = POLICY
        self.scope = scope
        self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        for entry in self.catalog["source_entries"]:
            source = entry["source_metadata"]
            for unit in entry["units"]:
                if not scope_allows(entry, source, unit, scope):
                    continue  # o RLS não devolve a linha
                content = (self.root / source["content_locator"]["path"]).read_bytes().decode("utf-8")
                audience = unit.get("audience") or source.get("audience") or {"area_scope": "common"}
                self.rows[(source["source_id"], source["revision"], unit["unit_id"])] = {
                    "source_id": source["source_id"],
                    "revision": source["revision"],
                    "unit_id": unit["unit_id"],
                    "ordinal": unit["ordinal"],
                    "unit_kind": unit["unit_kind"],
                    "area_scope": audience["area_scope"],
                    "heading_path": unit["heading_path"],
                    "content": content,
                    "content_digest": unit["content_digest"]["value"],
                    "source_digest": source["content_digest"]["value"],
                    "estimated_tokens": _estimated_tokens(unit),
                }

    def search(self, requirement, query, limit):
        terms = normalize_search_query(query)
        words = [word.casefold() for word in terms.fts_query.split(" or ")]
        eligible = set(eligible_keys(self.catalog, requirement, self.policy))
        found = []
        for key, row in self.rows.items():
            if key not in eligible:
                continue
            hits = sum(word in row["content"].casefold() for word in words)
            if hits:
                found.append(
                    candidate_from_row(
                        {**row, "excerpt": row["content"], "fts": hits / len(words), "trgm": 0.0, "score": hits / len(words)}
                    )
                )
        return sorted(found, key=KnowledgeCandidate.tie_break)[:limit]

    def load_unit_contents(self, keys):
        contents = {}
        for key in keys:
            row = self.rows.get(key)
            if row is None:
                raise VaultError("selected_unit_missing", "/selected_units", str(key))
            source, unit = next(
                (entry["source_metadata"], unit)
                for entry in self.catalog["source_entries"]
                for unit in entry["units"]
                if (entry["source_metadata"]["source_id"], entry["source_metadata"]["revision"], unit["unit_id"]) == key
            )
            if hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != unit["content_digest"]["value"]:
                raise VaultError("unit_integrity_mismatch", "/content_digest", str(key))
            contents[key] = row["content"]
        return contents


# --- modo offline e interface comum (Q22) -------------------------------------------------------


def test_offline_search_applies_family_filter_in_memory(vault) -> None:
    gemini = offline(vault, "family_gemini")
    codex = offline(vault, "family_codex")
    claude = offline(vault, "family_claude")
    req = requirement("K1")

    gemini_ids = {item.source_id for item in gemini.search(req, "reconciliação", 20)}
    codex_ids = {item.source_id for item in codex.search(req, "reconciliação", 20)}
    claude_ids = {item.source_id for item in claude.search(req, "reconciliação", 20)}

    assert gemini_ids == {"recon"}
    assert codex_ids == {"recon", "codex-only"}
    assert claude_ids == {"recon", "claude-only"}


def test_offline_record_declares_backend_and_valid_schema(vault) -> None:
    backend = offline(vault, "family_gemini")
    choice = session(backend)
    candidates = choice.search("K1", "reconciliação tentativas", 20)

    record = choice.choose({"K1": [unit_ref(candidates[0].source_id, vault[1])]})

    assert validate_instance(record, "knowledge_selection") == []
    assert (record["backend"], record["selection_mode"], record["ranking_version"]) == (
        BACKEND_OFFLINE,
        "agent_mcp_choice",
        "bm25-v1",
    )
    assert record["status"] == "satisfied"
    assert {item["unit_ref"]["source_id"] for item in record["candidates"]} == {"recon"}


def test_create_backend_is_offline_without_dsn(vault) -> None:
    root, catalog = vault
    config = resolve_memory_config({})

    backend = create_knowledge_backend(
        config, catalog=catalog, policy=POLICY, scope=scope_for("family_gemini"), vault_root=root
    )

    assert isinstance(backend, OfflineMarkdownBackend)
    assert backend.backend_id == BACKEND_OFFLINE


def test_create_backend_is_offline_when_forced_even_with_dsn(vault) -> None:
    root, catalog = vault
    config = resolve_memory_config(
        {ENV_MEMORY_DSN: "postgresql://x/y", ENV_KNOWLEDGE_BACKEND: "offline_markdown_bm25"}
    )

    backend = create_knowledge_backend(
        config, catalog=catalog, policy=POLICY, scope=scope_for("family_codex"), vault_root=root
    )

    assert backend.backend_id == BACKEND_OFFLINE


def test_offline_backend_rejects_scope_of_another_catalog(vault) -> None:
    root, catalog = vault
    other = MemoryScope(catalog_id="cat-outro", project_id=PROJECT, family_id="family_codex", role_id="r")

    with pytest.raises(VaultError) as raised:
        OfflineMarkdownBackend(root, catalog, POLICY, other)

    assert raised.value.code == "catalog_scope_mismatch"


def test_importing_the_choice_modules_does_not_load_the_driver() -> None:
    code = (
        "import sys; import orq_contracts.knowledge_choice, orq_contracts.knowledge_backend,"
        " orq_contracts.memory_store.knowledge; assert 'psycopg' not in sys.modules"
    )
    src = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": src}, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


# --- validação da escolha (Q24, Q25) ---------------------------------------------------------------


def test_choice_outside_the_search_results_is_rejected(vault) -> None:
    choice = session(offline(vault, "family_claude"))
    choice.search("K1", "reconciliação", 20)

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose({"K1": [unit_ref("codex-only", vault[1])]})  # existe no catálogo, não na busca da família

    assert raised.value.code == "unit_not_in_search_results"
    assert choice.selection is None


def test_unit_found_for_one_family_is_not_choosable_by_another(vault) -> None:
    codex = session(offline(vault, "family_codex"))
    codex_found = {item.source_id for item in codex.search("K1", "reconciliação", 20)}
    claude = session(offline(vault, "family_claude"))
    claude.search("K1", "reconciliação", 20)

    assert "codex-only" in codex_found
    with pytest.raises(KnowledgeChoiceError) as raised:
        claude.choose({"K1": [unit_ref("codex-only", vault[1])]})
    assert raised.value.code == "unit_not_in_search_results"


def test_choice_before_any_search_is_rejected(vault) -> None:
    choice = session(offline(vault))

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose({"K1": [unit_ref("recon", vault[1])]})

    assert raised.value.code == "unit_not_in_search_results"


def test_choice_is_not_transferable_between_requirements(vault) -> None:
    choice = session(offline(vault), [requirement("K1"), requirement("K2", required=False)])
    choice.search("K1", "reconciliação", 20)

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose({"K1": [unit_ref("recon", vault[1])], "K2": [unit_ref("recon", vault[1])]})

    assert raised.value.code == "unit_not_in_search_results"
    assert raised.value.path == "/selections/K2/0"


@pytest.mark.parametrize(
    "selections, code",
    [
        ({"K9": []}, "unknown_requirement"),
        ({"K1": "recon"}, "invalid_selection"),
        ({"K1": [{"source_id": "recon"}]}, "invalid_unit_ref"),
        ({"K1": [{"source_id": "recon", "revision": "1", "unit_id": "u", "family_id": "family_claude"}]}, "invalid_unit_ref"),
        ({"K1": ["recon"]}, "invalid_unit_ref"),
    ],
)
def test_malformed_selection_is_rejected(vault, selections, code) -> None:
    choice = session(offline(vault))
    choice.search("K1", "reconciliação", 20)

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose(selections)

    assert raised.value.code == code


def test_duplicate_unit_ref_is_rejected(vault) -> None:
    choice = session(offline(vault))
    choice.search("K1", "reconciliação", 20)
    reference = unit_ref("recon", vault[1])

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose({"K1": [reference, dict(reference)]})

    assert raised.value.code == "duplicate_unit_ref"


def test_mandatory_requirement_without_coverage_is_never_satisfied(vault) -> None:
    choice = session(offline(vault), [requirement("K1"), requirement("K2", "pool threads")])
    choice.search("K1", "reconciliação", 20)
    choice.search("K2", "configuração pool", 20)

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose({"K1": [unit_ref("recon", vault[1])]})
    assert raised.value.code == "mandatory_requirement_uncovered"
    assert raised.value.path == "/selections/K2"

    with pytest.raises(KnowledgeChoiceError) as empty:
        choice.choose({})
    assert empty.value.code == "mandatory_requirement_uncovered"
    assert choice.selection is None


def test_optional_requirement_without_coverage_makes_the_record_partial(vault) -> None:
    choice = session(offline(vault), [requirement("K1"), requirement("K2", "pool", required=False)])
    choice.search("K1", "reconciliação", 20)

    record = choice.choose({"K1": [unit_ref("recon", vault[1])]})

    assert record["status"] == "partial"
    by_id = {item["knowledge_requirement_id"]: item for item in record["coverage"]}
    assert by_id["K1"]["status"] == "satisfied"
    assert (by_id["K2"]["status"], by_id["K2"]["gap_reason"]) == ("unsatisfied", "not_selected_by_agent")
    assert validate_instance(record, "knowledge_selection") == []


def test_global_budget_overflow_is_an_error_not_a_silent_cut(vault) -> None:
    choice = session(offline(vault), max_tokens=5)
    choice.search("K1", "reconciliação", 20)

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose({"K1": [unit_ref("recon", vault[1])]})

    assert raised.value.code == "context_budget_exceeded"
    assert choice.selection is None


def test_margin_counts_against_the_budget(vault) -> None:
    tokens = tokens_of(vault[1], "recon")
    ok = session(offline(vault), max_tokens=tokens)
    ok.search("K1", "reconciliação", 20)
    assert ok.choose({"K1": [unit_ref("recon", vault[1])]})["budget"]["estimated_tokens"] == tokens

    tight = session(offline(vault), max_tokens=tokens, margin_tokens=1)
    tight.search("K1", "reconciliação", 20)
    with pytest.raises(KnowledgeChoiceError) as raised:
        tight.choose({"K1": [unit_ref("recon", vault[1])]})
    assert raised.value.code == "context_budget_exceeded"


def test_per_requirement_budget_overflow_is_an_error(vault) -> None:
    choice = session(offline(vault), [requirement("K1", max_context_tokens=1)])
    choice.search("K1", "reconciliação", 20)

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.choose({"K1": [unit_ref("recon", vault[1])]})

    assert raised.value.code == "context_budget_exceeded"
    assert raised.value.path == "/selections/K1"


@pytest.mark.parametrize("limit", [0, 21, -1, True, "5", None])
def test_search_limit_is_capped_at_twenty(vault, limit) -> None:
    choice = session(offline(vault))

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.search("K1", "reconciliação", limit)
    assert raised.value.code == "invalid_search_limit"


def test_search_limit_one_returns_one_candidate(vault) -> None:
    assert len(session(offline(vault)).search("K1", "reconciliação configuração", 1)) == 1


def test_search_for_unknown_requirement_is_rejected(vault) -> None:
    with pytest.raises(KnowledgeChoiceError) as raised:
        session(offline(vault)).search("K9", "reconciliação", 5)
    assert raised.value.code == "unknown_requirement"


# --- reprodutibilidade byte a byte e consolidação (Q24) -------------------------------------------


def tokens_of(catalog, source_id: str) -> int:
    return next(
        _estimated_tokens(entry["units"][0])
        for entry in catalog["source_entries"]
        if entry["source_metadata"]["source_id"] == source_id
    )


def choose_both(backend, vault, order):
    choice = session(backend, [requirement("K1"), requirement("K2", "pool", required=False)])
    choice.search("K2", "configuração pool", 20)
    choice.search("K1", "reconciliação tentativas", 20)
    refs = [unit_ref(name, vault[1]) for name in order]
    record = choice.choose({"K1": refs[:1], "K2": refs[1:]})
    return record


def test_manifest_and_bundle_are_byte_reproducible_regardless_of_choice_order(vault) -> None:
    first = choose_both(offline(vault), vault, ["recon", "config"])
    second = choose_both(offline(vault), vault, ["recon", "config"])
    shuffled = session(offline(vault), [requirement("K1"), requirement("K2", "pool", required=False)])
    shuffled.search("K1", "reconciliação tentativas", 20)
    shuffled.search("K2", "configuração pool", 20)
    third = shuffled.choose(
        {"K2": [unit_ref("config", vault[1])], "K1": [unit_ref("recon", vault[1])]}
    )

    assert first == second
    assert third["selected_units"] == first["selected_units"]
    assert third["candidates"] == first["candidates"]
    materialized = [
        materialize_chosen_context(offline(vault), record, **MATERIALIZE_ARGUMENTS)
        for record in (first, second)
    ]
    assert canonical_digest(materialized[0].manifest) == canonical_digest(materialized[1].manifest)
    assert materialized[0].bundle == materialized[1].bundle
    assert [item["knowledge_provenance"]["unit_ref"]["source_id"] for item in materialized[0].manifest["items"]] == [
        "recon",
        "config",
    ]


def test_choice_order_given_by_the_agent_does_not_change_the_selected_order(vault) -> None:
    backend = offline(vault)
    a = session(backend, [requirement("K1")])
    a.search("K1", "reconciliação configuração", 20)
    b = session(backend, [requirement("K1")])
    b.search("K1", "reconciliação configuração", 20)

    record_a = a.choose({"K1": [unit_ref("recon", vault[1]), unit_ref("config", vault[1])]})
    record_b = b.choose({"K1": [unit_ref("config", vault[1]), unit_ref("recon", vault[1])]})

    assert record_a == record_b
    assert record_a["selection_hash"] == record_b["selection_hash"]


def test_manifest_from_chosen_units_matches_the_phase2_materializer(vault) -> None:
    root, catalog = vault
    record = choose_both(offline(vault), vault, ["recon", "config"])

    from_backend = materialize_chosen_context(offline(vault), record, **MATERIALIZE_ARGUMENTS)
    from_vault = materialize_knowledge_context(root, catalog, record, **MATERIALIZE_ARGUMENTS)

    assert from_backend.manifest == from_vault.manifest
    assert from_backend.bundle == from_vault.bundle
    assert from_backend.item_contents == from_vault.item_contents
    assert from_backend.manifest["bundle_hash"]["value"] == hashlib.sha256(from_backend.bundle).hexdigest()
    assert from_backend.manifest["knowledge_selection_refs"][0]["content_digest"] == record["selection_hash"]


# --- manifesto a partir de linhas simuladas do Postgres ---------------------------------------------


def test_manifest_from_simulated_postgres_rows_equals_the_vault_manifest(vault) -> None:
    root, catalog = vault
    backend = FakePostgresBackend(vault, scope_for("family_codex"))
    choice = session(backend, [requirement("K1"), requirement("K2", "pool", required=False)])
    choice.search("K1", "reconciliação tentativas", 20)
    choice.search("K2", "configuração pool", 20)
    record = choice.choose({"K1": [unit_ref("recon", catalog)], "K2": [unit_ref("config", catalog)]})

    from_rows = materialize_chosen_context(backend, record, **MATERIALIZE_ARGUMENTS)
    from_vault = materialize_knowledge_context(root, catalog, record, **MATERIALIZE_ARGUMENTS)

    assert record["backend"] == BACKEND_POSTGRES
    assert record["ranking_version"] == "pg-fts-trgm-v1"
    assert validate_instance(record, "knowledge_selection") == []
    assert from_rows.bundle == from_vault.bundle
    assert from_rows.manifest == from_vault.manifest


def test_simulated_postgres_search_never_returns_other_family_units(vault) -> None:
    backend = FakePostgresBackend(vault, scope_for("family_claude"))
    found = {item.source_id for item in backend.search(requirement("K1"), "reconciliação", 20)}

    assert found == {"recon", "claude-only"}


def test_tampered_row_content_is_blocked_before_materializing(vault) -> None:
    catalog = vault[1]
    backend = FakePostgresBackend(vault, scope_for("family_codex"))
    choice = session(backend)
    choice.search("K1", "reconciliação", 20)
    record = choice.choose({"K1": [unit_ref("recon", catalog)]})
    key = next(key for key in backend.rows if key[0] == "recon")
    backend.rows[key]["content"] += "alterado"

    with pytest.raises(VaultError) as raised:
        materialize_chosen_context(backend, record, **MATERIALIZE_ARGUMENTS)

    assert raised.value.code == "unit_integrity_mismatch"


def test_unit_missing_from_rows_is_blocked(vault) -> None:
    catalog = vault[1]
    backend = FakePostgresBackend(vault, scope_for("family_codex"))
    choice = session(backend)
    choice.search("K1", "reconciliação", 20)
    record = choice.choose({"K1": [unit_ref("recon", catalog)]})
    backend.rows.clear()

    with pytest.raises(VaultError) as raised:
        materialize_chosen_context(backend, record, **MATERIALIZE_ARGUMENTS)

    assert raised.value.code == "selected_unit_missing"


def test_record_of_another_backend_is_not_materialized(vault) -> None:
    record = choose_both(offline(vault), vault, ["recon", "config"])

    with pytest.raises(KnowledgeChoiceError) as raised:
        materialize_chosen_context(FakePostgresBackend(vault, scope_for("family_codex")), record, **MATERIALIZE_ARGUMENTS)

    assert raised.value.code == "backend_mismatch"


def test_manifest_budget_overflow_is_a_context_error(vault) -> None:
    record = choose_both(offline(vault), vault, ["recon", "config"])
    arguments = {**MATERIALIZE_ARGUMENTS, "target_context_limit_tokens": 20, "reserved_output_tokens": 0}

    with pytest.raises(ContextError) as raised:
        materialize_chosen_context(offline(vault), record, **arguments)

    assert raised.value.code == "context_budget_exceeded"


def test_phase2_materializer_requires_a_content_source(vault) -> None:
    record = choose_both(offline(vault), vault, ["recon", "config"])

    with pytest.raises(ContextError) as raised:
        materialize_knowledge_context(None, vault[1], record, **MATERIALIZE_ARGUMENTS)

    assert raised.value.code == "invalid_unit_source"


def test_backend_returning_ineligible_candidate_is_rejected(vault) -> None:
    class Misbehaving(FakePostgresBackend):
        def search(self, requirement, query, limit):
            row = next(iter(self.rows.values()))
            return [candidate_from_row({**row, "excerpt": "", "fts": 1.0, "trgm": 0.0, "score": 1.0, "source_id": "inventada"})]

    choice = session(Misbehaving(vault, scope_for("family_codex")))

    with pytest.raises(KnowledgeChoiceError) as raised:
        choice.search("K1", "reconciliação", 5)
    assert raised.value.code == "candidate_not_eligible"


# --- schema estendido ------------------------------------------------------------------------------------


def test_schema_accepts_phase2_records_without_backend_fields(vault) -> None:
    record = choose_both(offline(vault), vault, ["recon", "config"])
    legacy = {key: value for key, value in record.items() if key not in {"backend", "selection_mode", "ranking_version"}}
    legacy.pop("selection_hash")
    legacy["selection_hash"] = canonical_digest(legacy)

    assert validate_instance(legacy, "knowledge_selection") == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.update(backend="outro"),
        lambda record: record.update(selection_mode="manual"),
        lambda record: record.update(ranking_version=""),
        lambda record: record.pop("selection_mode"),
        lambda record: record.pop("ranking_version"),
    ],
)
def test_schema_rejects_inconsistent_backend_fields(vault, mutation) -> None:
    record = copy.deepcopy(choose_both(offline(vault), vault, ["recon", "config"]))
    mutation(record)

    assert validate_instance(record, "knowledge_selection") != []


# --- query, SQL e ranking (sem banco) ------------------------------------------------------------------------


def test_query_is_split_into_sanitized_words_and_trigram_terms() -> None:
    terms = normalize_search_query("reconciliar tentativa --trust-tools")

    assert terms.fts_query == "reconciliar or tentativa or trust or tools"
    assert terms.trgm_terms == ("reconciliar", "tentativa", "--trust-tools")


def test_websearch_operators_are_neutralized() -> None:
    terms = normalize_search_query('"frase exata" -excluir or ou OR outra')

    assert terms.fts_query == "frase or exata or excluir or ou or outra"
    assert '"' not in terms.fts_query and "-" not in terms.fts_query


def test_sql_injection_text_is_only_ever_a_parameter() -> None:
    hostile = "x'); DROP TABLE orq_memory.knowledge_units; SET LOCAL app.current_family_id = 'family_claude'; --"
    terms = normalize_search_query(hostile)
    parameters = search_parameters(terms, [("recon", "1", "u1")], 5)

    assert "DROP" not in SEARCH_SQL and "family_claude" not in SEARCH_SQL
    assert ";" not in terms.fts_query and "'" not in terms.fts_query
    assert parameters["fts_query"] == terms.fts_query
    placeholders = set(re.findall(r"%\((\w+)\)s", SEARCH_SQL))
    assert placeholders == set(parameters), "todo valor entra como parâmetro nomeado, e só eles"
    assert "%s" not in SEARCH_SQL


@pytest.mark.parametrize("query", ["", "   ", "--", "'; --", "a" * 513, "com\x00nul", None, 7])
def test_unusable_queries_are_rejected_not_truncated(query) -> None:
    with pytest.raises(MemoryStoreError) as raised:
        normalize_search_query(query)

    assert raised.value.code == "invalid_search_query"


def test_term_lists_are_capped_and_deduplicated() -> None:
    terms = normalize_search_query(" ".join(f"palavra{n}" for n in range(40)) + " Palavra0 palavra0")

    assert len(terms.fts_query.split(" or ")) == 16
    assert len(terms.trgm_terms) == 16


@pytest.mark.parametrize("limit", [0, 21, True, 1.5, "3"])
def test_postgres_limit_validation(limit) -> None:
    with pytest.raises(MemoryStoreError) as raised:
        validate_search_limit(limit)
    assert raised.value.code == "invalid_search_limit"


def test_ranking_is_versioned_and_weights_sum_to_one() -> None:
    assert RANKING_VERSION == "pg-fts-trgm-v1"
    assert FTS_WEIGHT + TRGM_WEIGHT == pytest.approx(1.0)
    assert (FTS_WEIGHT, TRGM_WEIGHT) == (0.70, 0.30)


def test_sql_uses_portuguese_fts_trigrams_and_deterministic_order() -> None:
    assert "websearch_to_tsquery('portuguese'::regconfig" in SEARCH_SQL
    assert "ts_rank_cd(u.search_tsv" in SEARCH_SQL
    assert "word_similarity(" in SEARCH_SQL
    assert "ORDER BY score DESC, source_id ASC, revision ASC, ordinal ASC, unit_id ASC" in SEARCH_SQL
    assert "LIMIT %(limit)s::integer" in SEARCH_SQL


def test_ties_break_by_source_revision_ordinal_and_unit() -> None:
    def candidate(source: str, revision: str, ordinal: int, unit: str, score: float) -> KnowledgeCandidate:
        return KnowledgeCandidate(source, revision, unit, ordinal, "a" * 64, "b" * 64, "common", "prose", (), 1, score, "")

    shuffled = [
        candidate("s2", "1", 0, "u1", 0.5),
        candidate("s1", "2", 0, "u9", 0.5),
        candidate("s1", "1", 1, "u3", 0.5),
        candidate("s1", "1", 0, "u2", 0.5),
        candidate("s9", "9", 9, "u0", 0.9),
    ]

    ordered = sorted(shuffled, key=KnowledgeCandidate.tie_break)

    assert [(item.source_id, item.revision, item.ordinal) for item in ordered] == [
        ("s9", "9", 9),
        ("s1", "1", 0),
        ("s1", "1", 1),
        ("s1", "2", 0),
        ("s2", "1", 0),
    ]


def test_candidate_from_row_rounds_and_collapses_whitespace() -> None:
    candidate = candidate_from_row(
        {
            "source_id": "s",
            "revision": "1",
            "unit_id": "u",
            "ordinal": 0,
            "unit_kind": "prose",
            "area_scope": "family",
            "heading_path": ["A", "B"],
            "content_digest": "c" * 64,
            "source_digest": "d" * 64,
            "estimated_tokens": 12,
            "excerpt": "linha um\n  linha   dois",
            "fts": 0.123456789,
            "trgm": 1.0,
            "score": 0.3868518,
        }
    )

    assert (candidate.score, candidate.fts, candidate.trgm) == (0.386852, 0.123457, 1.0)
    assert candidate.excerpt == "linha um linha dois"
    assert candidate.heading_path == ("A", "B")
    assert candidate.public()["content_digest"] == {"algorithm": "sha256", "value": "c" * 64}
    assert "family_id" not in candidate.public()


def test_central_materializer_rejects_loader_bytes_with_a_wrong_digest(vault) -> None:
    """Regressão (revisão do 7D): o unit_loader externo não pode publicar content_hash de bytes diferentes."""
    root, catalog = vault
    record = choose_both(offline(vault), vault, ["recon"])

    with pytest.raises(Exception) as raised:
        materialize_knowledge_context(root, catalog, record, unit_loader=lambda source, unit: "CONTEUDO INCORRETO",
                                      **MATERIALIZE_ARGUMENTS)
    assert getattr(raised.value, "code", None) == "unit_integrity_mismatch"


def test_failed_choice_clears_a_previous_valid_selection(vault) -> None:
    """Regressão (revisão do 7D): escolha inválida depois de uma válida não deixa a anterior publicada."""
    choice = session(offline(vault))
    choice.search("K1", "reconciliação", 20)
    assert choice.choose({"K1": [unit_ref("recon", vault[1])]})
    with pytest.raises(KnowledgeChoiceError):
        choice.choose({})
    assert choice.selection is None
