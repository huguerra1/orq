"""Carregamento fechado dos schemas locais do ORQ."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


SCHEMA_DIRECTORY = Path(__file__).resolve().parents[2] / "schemas" / "v0.1"
SCHEMA_FILES = {
    "common": "common.schema.json",
    "task": "task.schema.json",
    "workflow": "workflow.schema.json",
}


@lru_cache(maxsize=1)
def load_schemas() -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for name, filename in SCHEMA_FILES.items():
        with (SCHEMA_DIRECTORY / filename).open(encoding="utf-8") as handle:
            schema = json.load(handle)
        Draft202012Validator.check_schema(schema)
        schemas[name] = schema
    return schemas


@lru_cache(maxsize=1)
def schema_registry() -> Registry:
    resources = []
    for schema in load_schemas().values():
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def validator_for(name: str) -> Draft202012Validator:
    schema = load_schemas()[name]
    return Draft202012Validator(
        schema,
        registry=schema_registry(),
        format_checker=FormatChecker(),
    )
