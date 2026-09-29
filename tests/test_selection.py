from __future__ import annotations

import copy

import pytest

from orq_contracts.selection import materialize_unit, select_knowledge
from orq_contracts.validator import validate_instance
from orq_contracts.vault import VaultError, build_catalog_snapshot
from test_vault import active_assertion, catalog_definition, source_metadata, versioned_ref, write_source


CREATED_AT = "2026-09-28T11:00:00Z"


def requirement(
    identifier: str,
    subject: str,
    *,
    required: bool = True,
    source_refs: list[dict] | None = None,
    authority_scopes: list[str] | None = None,
    max_context_tokens: int | None = None,
) -> dict:
    return {
        "knowledge_requirement_id": identifier,
        "subject": subject,
        "required": required,
        "allowed_kinds": ["project"],
        "required_authority_scopes": authority_scopes or ["project_architecture"],
        "source_refs": source_refs or [],
        "applicability_filters": {
            "topics": [],
            "paths": [],
            "domain": None,
            "roles": [],
            "task_types": [],
        },
        "freshness": None,
        "coverage_rule": "at_least_one",
        "max_context_tokens": max_context_tokens,
    }


def source_ref(metadata: dict) -> dict:
    return {
        "id": metadata["source_id"],
        "version": metadata["revision"],
        "content_digest": metadata["content_digest"],
    }


def build_catalog(tmp_path, sources: list[tuple[bytes, dict]]) -> tuple:
    vault = tmp_path / "vault"
    vault.mkdir()
    assertions = []
    metadata_items = []
    for index, (raw, metadata) in enumerate(sources, start=1):
        write_source(vault, raw, metadata["content_locator"]["path"])
        metadata_items.append(metadata)
        assertions.append(active_assertion(metadata, assertion_id=f"status-{index}"))
    definition = catalog_definition(metadata_items[0], assertions)
    definition["sources"] = metadata_items
    return vault, build_catalog_snapshot(vault, definition)


def select(vault, catalog, requirements, **overrides) -> dict:
    arguments = {
        "query": "implementar arquitetura segura",
        "run_id": "run-demo",
        "workflow_ref": versioned_ref("workflow-demo"),
        "task_id": "T1",
        "candidate_attempt_number": 1,
        "project_id": "demo",
        "created_at": CREATED_AT,
        "max_tokens": 1000,
        "policy_snapshot_refs": [versioned_ref("selection-policy")],
    }
    arguments.update(overrides)
    return select_knowledge(vault, catalog, requirements, **arguments)


def one_source(tmp_path, raw: bytes = b"# Seguranca\n\nControle de acesso por papeis.\n") -> tuple:
    metadata = source_metadata(raw)
    return (*build_catalog(tmp_path, [(raw, metadata)]), metadata)


def test_selects_relevant_unit_and_emits_valid_record(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)

    result = select(vault, catalog, [requirement("K1", "controle acesso")])

    assert result["status"] == "satisfied"
    assert len(result["selected_units"]) == 1
    assert result["coverage"][0]["status"] == "satisfied"
    assert validate_instance(result, "knowledge_selection") == []


def test_filters_wrong_project_before_ranking(tmp_path) -> None:
    correct_raw = b"# Arquitetura\n\nServico modular local.\n"
    wrong_raw = b"# Arquitetura\n\nServico modular local local local.\n"
    correct = source_metadata(correct_raw)
    wrong = source_metadata(wrong_raw, "content/projects/outro/guide.md")
    wrong["source_id"] = "source-outro"
    wrong["project_scope"]["project_id"] = "outro"
    vault, catalog = build_catalog(tmp_path, [(correct_raw, correct), (wrong_raw, wrong)])

    result = select(vault, catalog, [requirement("K1", "servico modular local")])

    assert result["selected_units"][0]["unit_ref"]["source_id"] == "source-demo"
    excluded = next(item for item in result["candidates"] if item["unit_ref"]["source_id"] == "source-outro")
    assert excluded["eligible"] is False
    assert excluded["requirement_results"][0]["reason_codes"] == ["wrong_project"]


def test_required_authority_is_enforced(tmp_path) -> None:
    raw = b"# Arquitetura\n\nServico modular local.\n"
    metadata = source_metadata(raw)
    metadata["authority"]["authority_scopes"] = []
    vault, catalog = build_catalog(tmp_path, [(raw, metadata)])

    result = select(vault, catalog, [requirement("K1", "servico modular")])

    assert result["status"] == "unsatisfied"
    assert result["selected_units"] == []
    assert result["candidates"][0]["reason_codes"] == ["insufficient_authority"]


def test_scores_each_requirement_independently(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    requirements = [
        requirement("K1", "controle acesso"),
        requirement("K2", "observabilidade quantica", required=False),
    ]

    result = select(vault, catalog, requirements)

    assert result["status"] == "partial"
    assert result["coverage"][0]["status"] == "satisfied"
    assert result["coverage"][1]["status"] == "unsatisfied"
    requirement_results = result["candidates"][0]["requirement_results"]
    assert requirement_results[0]["score"] > 0
    assert requirement_results[1]["score"] == 0


@pytest.mark.parametrize("limit_field", ["global", "requirement"])
def test_budget_gap_is_explicit(tmp_path, limit_field) -> None:
    vault, catalog, _ = one_source(tmp_path)
    local_limit = 1 if limit_field == "requirement" else None
    global_limit = 1 if limit_field == "global" else 1000

    result = select(
        vault,
        catalog,
        [requirement("K1", "controle acesso", max_context_tokens=local_limit)],
        max_tokens=global_limit,
    )

    assert result["status"] == "unsatisfied"
    assert result["coverage"][0]["gap_reason"] == "over_budget"
    assert "over_budget" in result["candidates"][0]["reason_codes"]


def test_exact_source_reference_allows_zero_lexical_score(tmp_path) -> None:
    vault, catalog, metadata = one_source(tmp_path)

    result = select(
        vault,
        catalog,
        [requirement("K1", "termo inexistente", source_refs=[source_ref(metadata)])],
        query="tambem ausente",
    )

    assert result["status"] == "satisfied"
    assert result["selected_units"][0]["score"] == 0


def test_selection_is_deterministic(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    requirements = [requirement("K1", "controle acesso")]

    first = select(vault, catalog, requirements)
    second = select(vault, catalog, copy.deepcopy(requirements))

    assert first == second
    assert first["selection_hash"] == second["selection_hash"]


def test_materialization_rechecks_source_digest(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    entry = catalog["source_entries"][0]
    path = vault / entry["source_metadata"]["content_locator"]["path"]
    path.write_bytes(b"# Seguranca\n\nConteudo alterado.\n")

    with pytest.raises(VaultError, match="integrity_mismatch"):
        materialize_unit(vault, entry["source_metadata"], entry["units"][0])
