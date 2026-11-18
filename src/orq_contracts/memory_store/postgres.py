"""Acesso ao Postgres da memória por família: migração, sincronização e leitura com escopo.

Só este módulo importa o psycopg. Ele não é carregado no modo offline.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..knowledge_backend import KnowledgeCandidate, UnitKey
from .config import POSTGRES_BACKEND, SHARED_GLOBAL_REGIME, MemoryConfig, MemoryScope, MemoryStoreError
from .ddl import (
    GLOBAL_READER_ROLE,
    READER_ROLE,
    SCHEMA_V2_VERSION,
    SCHEMA_VERSION,
    WRITER_ROLE,
    load_schema_sql,
    load_schema_v2_sql,
    schema_digest,
    schema_v2_digest,
)
from .search import (
    SEARCH_SQL,
    UNITS_BY_KEY_SQL,
    candidate_from_row,
    normalize_search_query,
    search_parameters,
    validate_search_limit,
)
from .sync import SyncPlan


# Chaves fixas de advisory lock (bigint) para serializar migração e sincronização.
MIGRATION_LOCK_KEY = 0x6F72715F6D6967  # "orq_mig"
SYNC_LOCK_KEY = 0x6F72715F73796E  # "orq_syn"


def connect(config: MemoryConfig) -> psycopg.Connection:
    """Abre conexão em autocommit; as transações são sempre explícitas.

    `prepare_threshold=None` desliga prepared statements no servidor: os comandos
    `SET LOCAL`/`SET TRANSACTION` repetidos a cada transação seguem pelo caminho simples.
    """

    if config.backend != POSTGRES_BACKEND or not config.dsn:
        raise MemoryStoreError("memory_backend_offline", "configuração sem backend Postgres")
    try:
        return psycopg.connect(config.dsn, autocommit=True, prepare_threshold=None)
    except psycopg.Error as error:
        # A mensagem do driver não é repassada: pode trazer partes do DSN.
        raise MemoryStoreError("memory_connection_failed", type(error).__name__) from None


def apply_schema(connection: psycopg.Connection) -> str:
    """Aplica o DDL versionado uma única vez. Retorna "applied" ou "unchanged"."""

    ddl_v1 = load_schema_sql()
    digest_v1 = schema_digest()
    ddl_v2 = load_schema_v2_sql()
    digest_v2 = schema_v2_digest()
    v1_entry = (SCHEMA_VERSION, digest_v1)
    v2_entry = (SCHEMA_V2_VERSION, digest_v2)
    expected = [v1_entry, v2_entry]
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            cursor.execute("SELECT pg_catalog.to_regclass('orq_memory.schema_migrations') IS NOT NULL")
            row = cursor.fetchone()
            if row is not None and row[0]:
                cursor.execute("SELECT version, ddl_digest FROM orq_memory.schema_migrations ORDER BY version")
                applied = [tuple(item) for item in cursor.fetchall()]
                if applied == expected:
                    return "unchanged"
                if applied == [v1_entry]:
                    cursor.execute(ddl_v2)
                    cursor.execute(
                        "INSERT INTO orq_memory.schema_migrations (version, ddl_digest) VALUES (%s, %s)",
                        v2_entry,
                    )
                    return "applied"
                raise MemoryStoreError(
                    "memory_schema_version_conflict",
                    f"banco com versões {[version for version, _ in applied]} ou digest diferente do DDL atual",
                )
            # Sem parâmetros, o psycopg usa o protocolo simples e executa o arquivo inteiro.
            cursor.execute(ddl_v1)
            cursor.execute(
                "INSERT INTO orq_memory.schema_migrations (version, ddl_digest) VALUES (%s, %s)",
                v1_entry,
            )
            cursor.execute(ddl_v2)
            cursor.execute(
                "INSERT INTO orq_memory.schema_migrations (version, ddl_digest) VALUES (%s, %s)",
                v2_entry,
            )
    return "applied"


@dataclass(frozen=True, slots=True)
class SyncResult:
    status: str  # "inserted" ou "unchanged"
    catalog_id: str
    sync_digest: str
    source_count: int
    unit_count: int


_INSERT_CATALOG = """
INSERT INTO orq_memory.knowledge_catalogs (
    catalog_id, catalog_version, catalog_hash, created_at, status_cutoff,
    source_count, unit_count, sync_digest
) VALUES (
    %(catalog_id)s, %(catalog_version)s, %(catalog_hash)s, %(created_at)s, %(status_cutoff)s,
    %(source_count)s, %(unit_count)s, %(sync_digest)s
)
"""

_INSERT_SOURCE = """
INSERT INTO orq_memory.knowledge_sources (
    catalog_id, source_id, revision, knowledge_kind, project_scope_kind, project_id, collection_id,
    resolved_status, area_scope, target_families, target_roles, title, source_digest,
    metadata_digest, source_metadata
) VALUES (
    %(catalog_id)s, %(source_id)s, %(revision)s, %(knowledge_kind)s, %(project_scope_kind)s,
    %(project_id)s, %(collection_id)s, %(resolved_status)s, %(area_scope)s,
    %(target_families)s::text[], %(target_roles)s::text[], %(title)s, %(source_digest)s,
    %(metadata_digest)s, %(source_metadata)s
)
"""

_INSERT_UNIT = """
INSERT INTO orq_memory.knowledge_units (
    catalog_id, source_id, revision, unit_id, ordinal, unit_kind, parent_unit_id, knowledge_kind,
    project_scope_kind, project_id, collection_id, resolved_status, area_scope, target_families,
    target_roles, title, heading_path, heading_text, content, content_digest, source_digest,
    size_bytes, estimated_tokens, unit_record, unit_record_digest
) VALUES (
    %(catalog_id)s, %(source_id)s, %(revision)s, %(unit_id)s, %(ordinal)s, %(unit_kind)s,
    %(parent_unit_id)s, %(knowledge_kind)s, %(project_scope_kind)s, %(project_id)s,
    %(collection_id)s, %(resolved_status)s, %(area_scope)s, %(target_families)s::text[],
    %(target_roles)s::text[], %(title)s, %(heading_path)s::text[], %(heading_text)s, %(content)s,
    %(content_digest)s, %(source_digest)s, %(size_bytes)s, %(estimated_tokens)s, %(unit_record)s,
    %(unit_record_digest)s
)
"""

_STORED_UNIT_DIGESTS = """
SELECT source_id, revision, unit_id, ordinal, content_digest, unit_record_digest
FROM orq_memory.knowledge_units
WHERE catalog_id = %s
ORDER BY source_id, revision, unit_id
"""


def _with_json(row: dict[str, Any], field: str) -> dict[str, Any]:
    return {**row, field: Jsonb(row[field])}


def sync_catalog(connection: psycopg.Connection, plan: SyncPlan) -> SyncResult:
    """Grava o plano numa transação única como `orq_memory_writer`; repetir é idempotente.

    Um catalog_id já sincronizado só é aceito com o mesmo `catalog_hash`, o mesmo
    `sync_digest` e as mesmas unidades/digests gravados; qualquer divergência aborta
    com `catalog_sync_integrity_error` sem alterar o banco.
    """

    catalog = plan.catalog_row
    try:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL ROLE {WRITER_ROLE}")
                cursor.execute("SELECT pg_catalog.pg_advisory_xact_lock(%s)", (SYNC_LOCK_KEY,))
                cursor.execute(
                    "SELECT catalog_hash, sync_digest FROM orq_memory.knowledge_catalogs WHERE catalog_id = %s",
                    (plan.catalog_id,),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if tuple(existing) != (catalog["catalog_hash"], catalog["sync_digest"]):
                        raise MemoryStoreError(
                            "catalog_sync_integrity_error",
                            f"{plan.catalog_id} já sincronizado com outro conteúdo; snapshots são imutáveis",
                        )
                    cursor.execute(_STORED_UNIT_DIGESTS, (plan.catalog_id,))
                    stored = sorted(tuple(item) for item in cursor.fetchall())
                    if stored != plan.expected_unit_digests():
                        raise MemoryStoreError(
                            "catalog_sync_integrity_error",
                            f"{plan.catalog_id}: unidades gravadas divergem do plano",
                        )
                    status = "unchanged"
                else:
                    cursor.execute(_INSERT_CATALOG, catalog)
                    cursor.executemany(
                        _INSERT_SOURCE, [_with_json(row, "source_metadata") for row in plan.source_rows]
                    )
                    cursor.executemany(_INSERT_UNIT, [_with_json(row, "unit_record") for row in plan.unit_rows])
                    status = "inserted"
    except psycopg.IntegrityError as error:
        raise MemoryStoreError("catalog_sync_integrity_error", type(error).__name__) from error
    return SyncResult(status, plan.catalog_id, plan.sync_digest, catalog["source_count"], catalog["unit_count"])


@contextmanager
def scoped_transaction(connection: psycopg.Connection, scope: MemoryScope) -> Iterator[psycopg.Cursor]:
    """Abre transação somente leitura como `orq_memory_reader`, com o escopo já fixado.

    O escopo entra por `set_config(nome, valor, true)` (equivalente parametrizado a
    `SET LOCAL`), antes de qualquer SELECT, e some no fim da transação. Recusa a
    transação se a role efetiva puder ignorar RLS.
    """

    if not isinstance(scope, MemoryScope):
        raise MemoryStoreError("invalid_memory_scope", "escopo deve ser MemoryScope montado pelo host")
    reader_role = GLOBAL_READER_ROLE if scope.memory_regime == SHARED_GLOBAL_REGIME else READER_ROLE
    with connection.transaction():
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(f"SET LOCAL ROLE {reader_role}")
            for setting, value in scope.settings():
                cursor.execute("SELECT pg_catalog.set_config(%s, %s, true)", (setting, value))
            cursor.execute(
                "SELECT current_user AS role_name, r.rolsuper, r.rolbypassrls"
                " FROM pg_catalog.pg_roles r WHERE r.rolname = current_user"
            )
            role = cursor.fetchone()
            if role is None or role["role_name"] != reader_role or role["rolsuper"] or role["rolbypassrls"]:
                raise MemoryStoreError("reader_role_can_bypass_rls", "role efetiva não é o leitor restrito")
            yield cursor


_UNIT_COLUMNS = """
source_id, revision, unit_id, ordinal, unit_kind, area_scope, target_families, target_roles,
title, heading_path, content, content_digest, source_digest, size_bytes, estimated_tokens
"""


class ScopedMemoryReader:
    """Leitor com escopo fixado na construção; os métodos não recebem família, papel nem projeto."""

    def __init__(self, connection: psycopg.Connection, scope: MemoryScope) -> None:
        if not isinstance(scope, MemoryScope):
            raise MemoryStoreError("invalid_memory_scope", "escopo deve ser MemoryScope montado pelo host")
        self._connection = connection
        self._scope = scope

    @classmethod
    def connect(cls, config: MemoryConfig, scope: MemoryScope) -> ScopedMemoryReader:
        return cls(connect(config), scope)

    @property
    def scope(self) -> MemoryScope:
        return self._scope

    def list_units(self) -> list[dict[str, Any]]:
        """Unidades visíveis no escopo, em ordem determinística."""

        with scoped_transaction(self._connection, self._scope) as cursor:
            cursor.execute(
                f"SELECT {_UNIT_COLUMNS} FROM orq_memory.knowledge_units"
                " ORDER BY source_id, revision, ordinal, unit_id"
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_unit(self, source_id: str, revision: str, unit_id: str) -> dict[str, Any] | None:
        with scoped_transaction(self._connection, self._scope) as cursor:
            cursor.execute(
                f"SELECT {_UNIT_COLUMNS} FROM orq_memory.knowledge_units"
                " WHERE source_id = %s AND revision = %s AND unit_id = %s",
                (source_id, revision, unit_id),
            )
            row = cursor.fetchone()
            return dict(row) if row is not None else None

    def search_units(self, eligible: list[UnitKey], query: str, limit: int) -> list[KnowledgeCandidate]:
        """Busca híbrida (FTS português + trigramas) dentro do escopo fixado na construção.

        `eligible` são as unidades que o catálogo do host admite para o requisito; o RLS ainda
        decide o que é visível. `query` e `limit` são validados e entram só como parâmetros SQL.
        """

        terms = normalize_search_query(query)
        validated = validate_search_limit(limit)
        if not eligible:
            return []
        with scoped_transaction(self._connection, self._scope) as cursor:
            cursor.execute(SEARCH_SQL, search_parameters(terms, eligible, validated))
            return [candidate_from_row(row) for row in cursor.fetchall()]

    def get_unit_contents(self, keys: list[UnitKey]) -> dict[UnitKey, dict[str, str]]:
        """Conteúdo e digests das unidades visíveis no escopo (chave ausente = invisível ou inexistente)."""

        if not keys:
            return {}
        parameters = {
            "source_ids": [key[0] for key in keys],
            "revisions": [key[1] for key in keys],
            "unit_ids": [key[2] for key in keys],
        }
        with scoped_transaction(self._connection, self._scope) as cursor:
            cursor.execute(UNITS_BY_KEY_SQL, parameters)
            return {
                (row["source_id"], row["revision"], row["unit_id"]): {
                    "content": row["content"],
                    "content_digest": row["content_digest"],
                    "source_digest": row["source_digest"],
                }
                for row in cursor.fetchall()
            }

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> ScopedMemoryReader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
