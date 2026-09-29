"""Contratos executáveis do ORQ."""

from .canonical import canonical_digest, canonicalize
from .selection import materialize_unit, select_knowledge
from .validator import (
    ValidationIssue,
    validate_instance,
    validate_knowledge_source,
    validate_task,
    validate_workflow,
)
from .vault import IngestedSource, VaultError, build_catalog_snapshot, ingest_markdown_source

__all__ = [
    "ValidationIssue",
    "IngestedSource",
    "VaultError",
    "build_catalog_snapshot",
    "canonical_digest",
    "canonicalize",
    "ingest_markdown_source",
    "materialize_unit",
    "select_knowledge",
    "validate_instance",
    "validate_knowledge_source",
    "validate_task",
    "validate_workflow",
]
