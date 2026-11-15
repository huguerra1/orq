"""DDL versionado da memória por família, sem dependência do driver."""

from __future__ import annotations

import hashlib
from pathlib import Path


SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_NAME = "orq_memory"
OWNER_ROLE = "orq_memory_owner"
WRITER_ROLE = "orq_memory_writer"
READER_ROLE = "orq_memory_reader"
MEMORY_ROLES = (OWNER_ROLE, WRITER_ROLE, READER_ROLE)
MEMORY_TABLES = ("knowledge_catalogs", "knowledge_sources", "knowledge_units")


def load_schema_sql() -> str:
    """Texto do DDL com fins de linha normalizados para LF."""

    return SCHEMA_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")


def schema_digest() -> str:
    """SHA-256 do DDL (LF), registrado em `orq_memory.schema_migrations`."""

    return hashlib.sha256(load_schema_sql().encode("utf-8")).hexdigest()
