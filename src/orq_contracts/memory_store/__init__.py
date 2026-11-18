"""Memória por família em Postgres com Row-Level Security (Fase 7, incremento 7B).

Importar este pacote não carrega o psycopg: configuração, escopo, plano de
sincronização e normalização/pesagem da busca (7D, `search`) são puros. O acesso ao banco fica em `orq_contracts.memory_store.postgres`,
importado só quando há backend Postgres configurado.
"""

from .config import (
    ENV_KNOWLEDGE_BACKEND,
    ENV_MEMORY_DSN,
    FAMILY_IDS,
    FAMILY_PARTITIONED_REGIME,
    KNOWLEDGE_BACKENDS,
    MEMORY_REGIMES,
    OFFLINE_BACKEND,
    POSTGRES_BACKEND,
    SCOPE_SETTINGS,
    SHARED_GLOBAL_REGIME,
    MemoryConfig,
    MemoryScope,
    MemoryStoreError,
    resolve_memory_config,
)
from .search import (
    FTS_WEIGHT,
    RANKING_VERSION,
    TRGM_MIN_SIMILARITY,
    TRGM_WEIGHT,
    SearchTerms,
    normalize_search_query,
)
from .sync import DEFAULT_AUDIENCE, SyncPlan, build_sync_plan, effective_audience

__all__ = [
    "DEFAULT_AUDIENCE",
    "ENV_KNOWLEDGE_BACKEND",
    "ENV_MEMORY_DSN",
    "FAMILY_IDS",
    "FAMILY_PARTITIONED_REGIME",
    "FTS_WEIGHT",
    "KNOWLEDGE_BACKENDS",
    "MEMORY_REGIMES",
    "OFFLINE_BACKEND",
    "POSTGRES_BACKEND",
    "RANKING_VERSION",
    "SCOPE_SETTINGS",
    "SHARED_GLOBAL_REGIME",
    "MemoryConfig",
    "MemoryScope",
    "MemoryStoreError",
    "SearchTerms",
    "SyncPlan",
    "TRGM_MIN_SIMILARITY",
    "TRGM_WEIGHT",
    "build_sync_plan",
    "effective_audience",
    "normalize_search_query",
    "resolve_memory_config",
]
