"""Backend `postgres_fts_trgm`: busca sob RLS e releitura das unidades com conferência de digest.

Não importa o driver (recebe um `ScopedMemoryReader` já construído), então pode ser importado
sem o psycopg carregado.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..knowledge_backend import (
    BACKEND_POSTGRES,
    EligibilityPolicy,
    KnowledgeCandidate,
    UnitKey,
    catalog_units,
    eligible_keys,
    verify_unit_content,
)
from ..vault import VaultError
from .config import MemoryStoreError
from .search import RANKING_VERSION

if TYPE_CHECKING:
    from .postgres import ScopedMemoryReader


class PostgresKnowledgeBackend:
    """Busca e leitura pelo escopo do `ScopedMemoryReader`; família/papel nunca vêm da chamada."""

    backend_id = BACKEND_POSTGRES
    ranking_version = RANKING_VERSION

    def __init__(self, reader: ScopedMemoryReader, catalog: dict[str, Any], policy: EligibilityPolicy) -> None:
        scope = reader.scope
        if catalog["catalog_id"] != scope.catalog_id:
            raise MemoryStoreError("catalog_scope_mismatch", "catálogo diferente do escopo fixado pelo host")
        if policy.project_id != scope.project_id:
            raise MemoryStoreError("project_scope_mismatch", "projeto da política diferente do escopo do host")
        self._reader = reader
        self.catalog = catalog
        self.policy = policy
        self._index = catalog_units(catalog)

    @property
    def reader(self) -> ScopedMemoryReader:
        return self._reader

    def search(self, requirement: dict[str, Any], query: str, limit: int) -> list[KnowledgeCandidate]:
        return self._reader.search_units(eligible_keys(self.catalog, requirement, self.policy), query, limit)

    def load_unit_contents(self, keys: list[UnitKey]) -> dict[UnitKey, str]:
        """Relê as unidades sob RLS e confere o SHA-256 do conteúdo contra o catálogo do host."""

        rows = self._reader.get_unit_contents(keys)
        contents: dict[UnitKey, str] = {}
        for key in keys:
            row = rows.get(key)
            resolved = self._index.get(key)
            if row is None or resolved is None:
                raise VaultError("selected_unit_missing", "/selected_units", str(key))
            _entry, source, unit = resolved
            if row["source_digest"] != source["content_digest"]["value"]:
                raise VaultError("integrity_mismatch", "/content_digest", f"digest da fonte diverge: {key}")
            contents[key] = verify_unit_content(unit, row["content"])
        return contents
