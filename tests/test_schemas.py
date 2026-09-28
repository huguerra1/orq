from __future__ import annotations

from orq_contracts.schemas import load_schemas, validator_for


def test_all_declared_schemas_load_and_validate_their_meta_schema() -> None:
    assert set(load_schemas()) == {"common", "task", "workflow"}


def test_validators_use_draft_2020_12() -> None:
    assert validator_for("task").META_SCHEMA["$id"] == "https://json-schema.org/draft/2020-12/schema"
    assert validator_for("workflow").META_SCHEMA["$id"] == "https://json-schema.org/draft/2020-12/schema"
