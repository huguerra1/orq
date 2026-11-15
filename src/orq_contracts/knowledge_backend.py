"""Interface comum de busca de conhecimento (Fase 7, incremento 7D).

Dois backends devolvem candidatos no mesmo formato e leem o conteúdo das unidades
escolhidas com conferência de digest:

- `postgres_fts_trgm`: FTS em português + `pg_trgm` sob Row-Level Security
  (`orq_contracts.memory_store.knowledge`, que só carrega o driver quando há DSN);
- `offline_markdown_bm25`: `bm25-v1` da Fase 2 sobre o Vault Markdown, com o mesmo filtro
  de visibilidade do RLS aplicado em memória.

`select_knowledge` (Fases 2–6) não muda: este módulo só acrescenta o caminho em que o
agente escolhe dentre candidatos.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .selection import (
    _bm25,
    _estimated_tokens,
    _requirement_reasons,
    _timestamp,
    _tokens,
    CLASSIFICATION_LEVEL,
    materialize_unit,
)
from .vault import VaultError

if TYPE_CHECKING:
    from .memory_store.config import MemoryConfig, MemoryScope


BACKEND_POSTGRES = "postgres_fts_trgm"
BACKEND_OFFLINE = "offline_markdown_bm25"
KNOWLEDGE_BACKEND_IDS = (BACKEND_OFFLINE, BACKEND_POSTGRES)

RANKING_VERSION_POSTGRES = "pg-fts-trgm-v1"
RANKING_VERSION_OFFLINE = "bm25-v1"

SEARCH_LIMIT_MAX = 20
SCORE_DECIMALS = 6
EXCERPT_CHARS = 240

UnitKey = tuple[str, str, str]  # (source_id, revision, unit_id)


@dataclass(frozen=True, slots=True)
class KnowledgeCandidate:
    """Candidato elegível devolvido pela busca (o que o agente enxerga antes de escolher)."""

    source_id: str
    revision: str
    unit_id: str
    ordinal: int
    source_digest: str  # SHA-256 hexadecimal da fonte
    unit_digest: str  # SHA-256 hexadecimal da unidade
    area_scope: str
    unit_kind: str
    heading_path: tuple[str, ...]
    estimated_tokens: int
    score: float
    excerpt: str
    fts: float | None = None  # componentes do ranking Postgres (None no modo offline)
    trgm: float | None = None

    @property
    def key(self) -> UnitKey:
        return self.source_id, self.revision, self.unit_id

    def tie_break(self) -> tuple[Any, ...]:
        """Ordem determinística: score decrescente e, no empate, fonte, revisão, ordinal, unidade."""

        return -self.score, self.source_id, self.revision, self.ordinal, self.unit_id

    def public(self) -> dict[str, Any]:
        """Forma serializável para a tool MCP (sem escopo de família/papel do host)."""

        return {
            "source_id": self.source_id,
            "revision": self.revision,
            "unit_id": self.unit_id,
            "area_scope": self.area_scope,
            "unit_kind": self.unit_kind,
            "heading_path": list(self.heading_path),
            "estimated_tokens": self.estimated_tokens,
            "content_digest": {"algorithm": "sha256", "value": self.unit_digest},
            "score": self.score,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True, slots=True)
class EligibilityPolicy:
    """Filtros do host aplicados ao catálogo além do escopo RLS (autoridade, classificação, validade)."""

    project_id: str
    as_of: str  # instante UTC (RFC 3339) usado em vigência e frescor
    allowed_collections: frozenset[str] = frozenset()
    max_classification: str = "internal"

    def __post_init__(self) -> None:
        if self.max_classification not in CLASSIFICATION_LEVEL and self.max_classification != "unknown":
            raise VaultError("invalid_max_classification", "/max_classification", self.max_classification)
        _timestamp(self.as_of)


class KnowledgeBackend(Protocol):
    """Contrato comum aos backends de busca (Postgres e offline)."""

    backend_id: str
    ranking_version: str
    catalog: dict[str, Any]
    policy: EligibilityPolicy

    def search(self, requirement: dict[str, Any], query: str, limit: int) -> list[KnowledgeCandidate]:
        """Candidatos elegíveis para o requisito, já ordenados e limitados."""

    def load_unit_contents(self, keys: list[UnitKey]) -> dict[UnitKey, str]:
        """Relê o conteúdo das unidades; a origem é responsável pelo escopo (RLS ou filtro offline)."""


def round_score(value: float) -> float:
    return round(float(value), SCORE_DECIMALS)


def excerpt_of(content: str, limit: int = EXCERPT_CHARS) -> str:
    """Trecho curto com espaços colapsados, para o agente avaliar o candidato."""

    return " ".join(content.split())[:limit]


def catalog_units(catalog: dict[str, Any]) -> dict[UnitKey, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Índice (fonte, revisão, unidade) -> (entrada do catálogo, metadata da fonte, unidade)."""

    index: dict[UnitKey, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
    for entry in catalog["source_entries"]:
        source = entry["source_metadata"]
        for unit in entry["units"]:
            index[(source["source_id"], source["revision"], unit["unit_id"])] = (entry, source, unit)
    return index


def requirement_reasons(
    catalog: dict[str, Any],
    requirement: dict[str, Any],
    policy: EligibilityPolicy,
) -> dict[UnitKey, list[str]]:
    """Motivos de inelegibilidade por unidade para o requisito (lista vazia = elegível)."""

    as_of = _timestamp(policy.as_of)
    reasons: dict[UnitKey, list[str]] = {}
    for key, (entry, source, unit) in catalog_units(catalog).items():
        found = _requirement_reasons(
            source,
            unit,
            requirement,
            project_id=policy.project_id,
            allowed_collections=set(policy.allowed_collections),
            max_classification=policy.max_classification,
            as_of=as_of,
        )
        if entry["resolved_status"] != "active":
            found = sorted({*found, "not_active"})
        reasons[key] = found
    return reasons


def eligible_keys(
    catalog: dict[str, Any],
    requirement: dict[str, Any],
    policy: EligibilityPolicy,
) -> list[UnitKey]:
    """Unidades do catálogo elegíveis ao requisito, na ordem do catálogo."""

    return [key for key, found in requirement_reasons(catalog, requirement, policy).items() if not found]


def scope_allows(entry: dict[str, Any], source: dict[str, Any], unit: dict[str, Any], scope: MemoryScope) -> bool:
    """Espelha em memória a política RLS do 7B (status ativo, projeto e audiência nos dois eixos)."""

    from .memory_store.sync import effective_audience

    if entry["resolved_status"] != "active":
        return False
    project_scope = source["project_scope"]
    in_project = (
        project_scope["kind"] == "global_operational" and source["knowledge_kind"] == "operational"
    ) or (project_scope["kind"] == "project" and project_scope["project_id"] == scope.project_id)
    if not in_project:
        return False
    audience = effective_audience(source, unit)
    if audience["area_scope"] == "common":
        return True
    families, roles = audience["target_families"], audience["target_roles"]
    return (not families or scope.family_id in families) and (not roles or scope.role_id in roles)


def candidate_from_catalog(
    source: dict[str, Any],
    unit: dict[str, Any],
    audience: dict[str, Any],
    *,
    score: float,
    excerpt: str,
) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        source_id=source["source_id"],
        revision=source["revision"],
        unit_id=unit["unit_id"],
        ordinal=unit["ordinal"],
        source_digest=source["content_digest"]["value"],
        unit_digest=unit["content_digest"]["value"],
        area_scope=audience["area_scope"],
        unit_kind=unit["unit_kind"],
        heading_path=tuple(unit["heading_path"]),
        estimated_tokens=_estimated_tokens(unit),
        score=score,
        excerpt=excerpt,
    )


def verify_unit_content(unit: dict[str, Any], content: str) -> str:
    """Confere o SHA-256 do conteúdo contra o digest da unidade no catálogo (sem truncar nem normalizar)."""

    if hashlib.sha256(content.encode("utf-8")).hexdigest() != unit["content_digest"]["value"]:
        raise VaultError("unit_integrity_mismatch", "/content_digest", "digest da unidade diverge")
    return content


class OfflineMarkdownBackend:
    """`bm25-v1` sobre o Vault Markdown, com o filtro de audiência do RLS aplicado em memória.

    O ranking considera só os termos da query (como no Postgres); o score é o BM25 da Fase 2
    sobre as unidades elegíveis e visíveis ao escopo, arredondado a 6 casas.
    """

    backend_id = BACKEND_OFFLINE
    ranking_version = RANKING_VERSION_OFFLINE

    def __init__(
        self,
        vault_root: Path,
        catalog: dict[str, Any],
        policy: EligibilityPolicy,
        scope: MemoryScope,
    ) -> None:
        if catalog["catalog_id"] != scope.catalog_id:
            raise VaultError("catalog_scope_mismatch", "/catalog_id", "catálogo diferente do escopo do host")
        if policy.project_id != scope.project_id:
            raise VaultError("project_scope_mismatch", "/project_id", "projeto da política diferente do escopo")
        self.vault_root = vault_root
        self.catalog = catalog
        self.policy = policy
        self.scope = scope
        self._index = catalog_units(catalog)

    def search(self, requirement: dict[str, Any], query: str, limit: int) -> list[KnowledgeCandidate]:
        from .memory_store.sync import effective_audience

        terms = _tokens(query)
        visible: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for key in eligible_keys(self.catalog, requirement, self.policy):
            entry, source, unit = self._index[key]
            if scope_allows(entry, source, unit, self.scope):
                visible.append((source, unit, materialize_unit(self.vault_root, source, unit)))
        scores = _bm25([_tokens(content) for _, _, content in visible], terms)
        candidates = [
            candidate_from_catalog(
                source,
                unit,
                effective_audience(source, unit),
                score=round_score(score),
                excerpt=excerpt_of(content),
            )
            for (source, unit, content), score in zip(visible, scores, strict=True)
            if score > 0
        ]
        return sorted(candidates, key=KnowledgeCandidate.tie_break)[:limit]

    def load_unit_contents(self, keys: list[UnitKey]) -> dict[UnitKey, str]:
        contents: dict[UnitKey, str] = {}
        for key in keys:
            resolved = self._index.get(key)
            if resolved is None or not scope_allows(*resolved, self.scope):
                raise VaultError("selected_unit_missing", "/selected_units", str(key))
            _entry, source, unit = resolved
            contents[key] = materialize_unit(self.vault_root, source, unit)
        return contents


def create_knowledge_backend(
    config: MemoryConfig,
    *,
    catalog: dict[str, Any],
    policy: EligibilityPolicy,
    scope: MemoryScope,
    vault_root: Path | None = None,
    connection: Any = None,
) -> KnowledgeBackend:
    """Backend conforme a configuração: sem DSN (ou forçado), offline; com DSN, Postgres."""

    if config.offline:
        if vault_root is None:
            raise VaultError("invalid_unit_source", "/vault_root", "modo offline exige o diretório do Vault")
        return OfflineMarkdownBackend(vault_root, catalog, policy, scope)
    from .memory_store.knowledge import PostgresKnowledgeBackend
    from .memory_store.postgres import ScopedMemoryReader

    reader = ScopedMemoryReader(connection, scope) if connection is not None else ScopedMemoryReader.connect(config, scope)
    return PostgresKnowledgeBackend(reader, catalog, policy)
