from __future__ import annotations

import copy
import hashlib

import pytest

from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.context import ContextError, materialize_knowledge_context
from orq_contracts.validator import validate_context_manifest, validate_instance
from orq_contracts.vault import VaultError
from test_selection import one_source, requirement, select
from test_vault import versioned_ref


def materialize(vault, catalog, selection, **overrides):
    arguments = {
        "routing_decision_ref": versioned_ref("routing-demo"),
        "target_ref": versioned_ref("target-demo"),
        "target_context_limit_tokens": 1000,
        "reserved_output_tokens": 100,
        "margin_tokens": 10,
    }
    arguments.update(overrides)
    return materialize_knowledge_context(vault, catalog, selection, **arguments)


def test_materializes_ordered_bundle_and_valid_manifest(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    selection = select(vault, catalog, [requirement("K1", "controle acesso")])

    result = materialize(vault, catalog, selection)

    assert validate_instance(result.manifest, "context_manifest") == []
    assert validate_context_manifest(result.manifest) == []
    assert result.manifest["items"][0]["sequence"] == 1
    assert result.manifest["items"][0]["knowledge_provenance"]["requirement_ids"] == ["K1"]
    assert result.manifest["bundle_hash"]["value"] == hashlib.sha256(result.bundle).hexdigest()
    assert result.bundle == canonicalize(
        {
            "schema_version": "0.1",
            "context_manifest_id": result.manifest["context_manifest_id"],
            "items": [
                {
                    "sequence": 1,
                    "item_id": "context-item-1",
                    "category": "project_knowledge",
                    "content": result.item_contents["context-item-1"].decode("utf-8"),
                }
            ],
        }
    )


def test_materialization_is_deterministic(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    selection = select(vault, catalog, [requirement("K1", "controle acesso")])

    first = materialize(vault, catalog, selection)
    second = materialize(vault, catalog, copy.deepcopy(selection))

    assert first.manifest == second.manifest
    assert first.bundle == second.bundle
    assert first.item_contents == second.item_contents


def test_rejects_source_changed_after_selection(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    selection = select(vault, catalog, [requirement("K1", "controle acesso")])
    source_path = vault / catalog["source_entries"][0]["source_metadata"]["content_locator"]["path"]
    source_path.write_bytes(b"# Alterado\n")

    with pytest.raises(VaultError, match="integrity_mismatch"):
        materialize(vault, catalog, selection)


def test_rejects_context_that_exceeds_target_budget(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    selection = select(vault, catalog, [requirement("K1", "controle acesso")])

    with pytest.raises(ContextError, match="context_budget_exceeded"):
        materialize(
            vault,
            catalog,
            selection,
            target_context_limit_tokens=2,
            reserved_output_tokens=1,
            margin_tokens=0,
        )


def test_rejects_tampered_selection_record(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    selection = select(vault, catalog, [requirement("K1", "controle acesso")])
    selection["query_input"]["text"] = "alterado"

    with pytest.raises(ContextError, match="record_digest_mismatch"):
        materialize(vault, catalog, selection)


def test_rejects_selected_unit_with_unknown_requirement(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    selection = select(vault, catalog, [requirement("K1", "controle acesso")])
    selection["selected_units"][0]["requirement_ids"] = ["K-desconhecido"]
    unsigned = {key: value for key, value in selection.items() if key != "selection_hash"}
    selection["selection_hash"] = canonical_digest(unsigned)

    with pytest.raises(ContextError, match="selected_unit_unknown_requirement"):
        materialize(vault, catalog, selection)


def test_rejects_unsatisfied_required_knowledge(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    selection = select(
        vault,
        catalog,
        [requirement("K1", "observabilidade quantica")],
        query="tambem ausente",
    )

    with pytest.raises(ContextError, match="knowledge_selection_not_admissible"):
        materialize(vault, catalog, selection)


def test_semantic_validator_rejects_non_contiguous_sequence(tmp_path) -> None:
    vault, catalog, _ = one_source(tmp_path)
    selection = select(vault, catalog, [requirement("K1", "controle acesso")])
    manifest = materialize(vault, catalog, selection).manifest
    manifest["items"][0]["sequence"] = 2

    issues = validate_context_manifest(manifest)

    assert any(issue.code == "semantic.invalid_item_sequence" for issue in issues)
