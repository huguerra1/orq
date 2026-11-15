"""Memória por família em Postgres com Row-Level Security (Fase 7, incremento 7B).

Importar este pacote não carrega o psycopg: configuração, escopo e plano de
sincronização são puros. O acesso ao banco fica em `orq_contracts.memory_store.postgres`,
importado só quando há backend Postgres configurado.
"""

from .config import (
    ENV_KNOWLEDGE_BACKEND,
    ENV_MEMORY_DSN,
    FAMILY_IDS,
    KNOWLEDGE_BACKENDS,
    OFFLINE_BACKEND,
    POSTGRES_BACKEND,
    SCOPE_SETTINGS,
    MemoryConfig,
    MemoryScope,
    MemoryStoreError,
    resolve_memory_config,
)
from .sync import DEFAULT_AUDIENCE, SyncPlan, build_sync_plan, effective_audience

__all__ = [
    "DEFAULT_AUDIENCE",
    "ENV_KNOWLEDGE_BACKEND",
    "ENV_MEMORY_DSN",
    "FAMILY_IDS",
    "KNOWLEDGE_BACKENDS",
    "OFFLINE_BACKEND",
    "POSTGRES_BACKEND",
    "SCOPE_SETTINGS",
    "MemoryConfig",
    "MemoryScope",
    "MemoryStoreError",
    "SyncPlan",
    "build_sync_plan",
    "effective_audience",
    "resolve_memory_config",
]
