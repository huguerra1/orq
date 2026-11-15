"""Busca híbrida em português + trigramas (7D): consulta SQL, pesagem e normalização da query.

Módulo puro: não importa o driver. A execução fica em `postgres.ScopedMemoryReader.search_units`,
sempre dentro da transação com escopo do 7B (o RLS filtra catálogo, projeto, status e audiência).

Ranking `pg-fts-trgm-v1`
------------------------
    fts   = ts_rank_cd(search_tsv, tsquery, 32)         em [0, 1): rank / (rank + 1)
    trgm  = max sobre os termos da query de word_similarity(termo, título + cabeçalhos + conteúdo)
                                                        em [0, 1]
    score = round(0.70 * fts + 0.30 * trgm, 6)

Entra como candidato quem casa no FTS ou tem `trgm >= 0.5`. A ordem é determinística:
`(score DESC, source_id, revision, ordinal, unit_id)`. Mudar pesos, limiar, normalização ou
tokenização exige nova `RANKING_VERSION`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..knowledge_backend import (
    EXCERPT_CHARS,
    RANKING_VERSION_POSTGRES,
    SCORE_DECIMALS,
    SEARCH_LIMIT_MAX,
    KnowledgeCandidate,
    UnitKey,
    round_score,
)
from .config import MemoryStoreError


RANKING_VERSION = RANKING_VERSION_POSTGRES
FTS_WEIGHT = 0.70
TRGM_WEIGHT = 0.30
TRGM_MIN_SIMILARITY = 0.5
FTS_NORMALIZATION = 32  # ts_rank_cd: divide o rank por (rank + 1)

QUERY_MAX_CHARS = 512
MAX_QUERY_TERMS = 16
TERM_MAX_CHARS = 64
TRGM_MIN_ALNUM = 3

_WORD_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)

# Todos os valores entram como parâmetros (`%(nome)s`); nenhum texto é interpolado no SQL.
# O filtro `u.catalog_id = current_setting(...)` repete, de propósito, o escopo cravado pelo host
# (o RLS já o impõe); `eligible` vem do catálogo (autoridade, classificação, vigência, frescor).
SEARCH_SQL = """
WITH q AS (
    SELECT websearch_to_tsquery('portuguese'::regconfig, %(fts_query)s::text) AS tsq
),
eligible AS (
    SELECT e.source_id, e.revision, e.unit_id
    FROM unnest(%(source_ids)s::text[], %(revisions)s::text[], %(unit_ids)s::text[])
        AS e(source_id, revision, unit_id)
),
scored AS (
    SELECT
        u.source_id, u.revision, u.unit_id, u.ordinal, u.unit_kind, u.area_scope, u.heading_path,
        u.content_digest, u.source_digest, u.estimated_tokens,
        left(u.content, %(excerpt_chars)s::integer) AS excerpt,
        (u.search_tsv @@ q.tsq) AS fts_match,
        ts_rank_cd(u.search_tsv, q.tsq, %(fts_normalization)s::integer)::double precision AS fts,
        coalesce((
            SELECT max(word_similarity(t.token, u.title || ' ' || u.heading_text || ' ' || u.content))
            FROM unnest(%(trgm_terms)s::text[]) AS t(token)
        ), 0)::double precision AS trgm
    FROM orq_memory.knowledge_units AS u
    JOIN eligible AS e
        ON e.source_id = u.source_id AND e.revision = u.revision AND e.unit_id = u.unit_id
    CROSS JOIN q
    WHERE u.catalog_id = pg_catalog.current_setting('app.catalog_id', true)
)
SELECT
    source_id, revision, unit_id, ordinal, unit_kind, area_scope, heading_path,
    content_digest, source_digest, estimated_tokens, excerpt, fts, trgm,
    round(
        ((%(fts_weight)s::double precision) * fts + (%(trgm_weight)s::double precision) * trgm)::numeric,
        %(score_decimals)s::integer
    )::double precision AS score
FROM scored
WHERE fts_match OR trgm >= %(trgm_min)s::double precision
ORDER BY score DESC, source_id ASC, revision ASC, ordinal ASC, unit_id ASC
LIMIT %(limit)s::integer
"""

UNITS_BY_KEY_SQL = """
SELECT u.source_id, u.revision, u.unit_id, u.content, u.content_digest, u.source_digest
FROM orq_memory.knowledge_units AS u
JOIN unnest(%(source_ids)s::text[], %(revisions)s::text[], %(unit_ids)s::text[])
    AS k(source_id, revision, unit_id)
    ON k.source_id = u.source_id AND k.revision = u.revision AND k.unit_id = u.unit_id
WHERE u.catalog_id = pg_catalog.current_setting('app.catalog_id', true)
"""


@dataclass(frozen=True, slots=True)
class SearchTerms:
    """Query já saneada: `fts_query` (palavras unidas por `or`) e termos para trigramas."""

    fts_query: str
    trgm_terms: tuple[str, ...]


def validate_search_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SEARCH_LIMIT_MAX:
        raise MemoryStoreError("invalid_search_limit", f"limit deve ser inteiro de 1 a {SEARCH_LIMIT_MAX}")
    return limit


def normalize_search_query(query: object) -> SearchTerms:
    """Sanea a query do agente. Operadores do `websearch_to_tsquery` (aspas, `-`, `or`) são neutralizados.

    - FTS: só palavras (`[^\\W_]+`), sem duplicatas, no máximo 16, unidas por ` or ` (qualquer
      termo casa; o `ts_rank_cd` premia quem casa mais). A palavra literal "or" é descartada.
    - Trigramas: pedaços separados por espaço com ao menos 3 caracteres alfanuméricos
      (preserva identificadores como `--trust-tools=fs_read` e `QuotaStateRecord`).
    Query vazia, longa demais ou com NUL é rejeitada, nunca truncada em silêncio.
    """

    if not isinstance(query, str):
        raise MemoryStoreError("invalid_search_query", "query deve ser texto")
    if len(query) > QUERY_MAX_CHARS:
        raise MemoryStoreError("invalid_search_query", f"query excede {QUERY_MAX_CHARS} caracteres")
    if "\x00" in query:
        raise MemoryStoreError("invalid_search_query", "query contém NUL")

    seen: set[str] = set()
    words: list[str] = []
    for word in _WORD_PATTERN.findall(query):
        folded = word.casefold()
        if folded == "or" or len(word) > TERM_MAX_CHARS or folded in seen:
            continue
        seen.add(folded)
        words.append(word)
    if not words:
        raise MemoryStoreError("invalid_search_query", "query sem termos pesquisáveis")

    seen = set()
    terms: list[str] = []
    for chunk in query.split():
        folded = chunk.casefold()
        if sum(char.isalnum() for char in chunk) < TRGM_MIN_ALNUM or len(chunk) > TERM_MAX_CHARS or folded in seen:
            continue
        seen.add(folded)
        terms.append(chunk)
    return SearchTerms(" or ".join(words[:MAX_QUERY_TERMS]), tuple(terms[:MAX_QUERY_TERMS]))


def search_parameters(terms: SearchTerms, keys: list[UnitKey], limit: int) -> dict[str, Any]:
    return {
        "fts_query": terms.fts_query,
        "trgm_terms": list(terms.trgm_terms),
        "source_ids": [key[0] for key in keys],
        "revisions": [key[1] for key in keys],
        "unit_ids": [key[2] for key in keys],
        "fts_weight": FTS_WEIGHT,
        "trgm_weight": TRGM_WEIGHT,
        "trgm_min": TRGM_MIN_SIMILARITY,
        "limit": limit,
        "excerpt_chars": EXCERPT_CHARS,
        "fts_normalization": FTS_NORMALIZATION,
        "score_decimals": SCORE_DECIMALS,
    }


def candidate_from_row(row: dict[str, Any]) -> KnowledgeCandidate:
    """Converte uma linha do `SEARCH_SQL` no candidato comum aos backends."""

    return KnowledgeCandidate(
        source_id=row["source_id"],
        revision=row["revision"],
        unit_id=row["unit_id"],
        ordinal=int(row["ordinal"]),
        source_digest=row["source_digest"],
        unit_digest=row["content_digest"],
        area_scope=row["area_scope"],
        unit_kind=row["unit_kind"],
        heading_path=tuple(row["heading_path"]),
        estimated_tokens=int(row["estimated_tokens"]),
        score=round_score(row["score"]),
        excerpt=" ".join(str(row["excerpt"]).split()),
        fts=round_score(row["fts"]),
        trgm=round_score(row["trgm"]),
    )
