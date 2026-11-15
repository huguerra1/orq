"""Configuração e escopo da memória por família, sem dependência do driver.

Este módulo não importa o psycopg: o modo offline (`offline_markdown_bm25`)
precisa continuar funcionando sem banco, sem Docker e sem o driver carregado.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field


ENV_MEMORY_DSN = "ORQ_MEMORY_DSN"
ENV_KNOWLEDGE_BACKEND = "ORQ_KNOWLEDGE_BACKEND"

OFFLINE_BACKEND = "offline_markdown_bm25"
POSTGRES_BACKEND = "postgres"
KNOWLEDGE_BACKENDS = (OFFLINE_BACKEND, POSTGRES_BACKEND)

# Mesmo enum de `common.schema.json#/$defs/familyId` (um teste confere a igualdade).
FAMILY_IDS = (
    "family_claude",
    "family_codex",
    "family_gemini",
    "family_kiro",
    "family_simulated",
)

# Mesmo padrão de `common.schema.json#/$defs/identifier`.
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]*$")
IDENTIFIER_MAX_LENGTH = 128

# Variáveis de transação lidas pelas políticas RLS, na ordem em que são fixadas.
SCOPE_SETTINGS = (
    ("app.catalog_id", "catalog_id"),
    ("app.current_project_id", "project_id"),
    ("app.current_family_id", "family_id"),
    ("app.current_role_id", "role_id"),
)


class MemoryStoreError(Exception):
    """Erro tipado da memória. Não é dataclass congelado: o Python precisa gravar `__traceback__`."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    """Backend escolhido. O DSN fica fora do `repr` porque pode conter senha."""

    backend: str
    dsn: str | None = field(default=None, repr=False)

    @property
    def offline(self) -> bool:
        return self.backend == OFFLINE_BACKEND


def resolve_memory_config(environ: Mapping[str, str] | None = None) -> MemoryConfig:
    """Resolve o backend a partir do ambiente.

    - sem `ORQ_KNOWLEDGE_BACKEND`: Postgres se houver `ORQ_MEMORY_DSN`, senão offline;
    - `ORQ_KNOWLEDGE_BACKEND=offline_markdown_bm25`: offline mesmo com DSN (o DSN é descartado);
    - `ORQ_KNOWLEDGE_BACKEND=postgres`: exige DSN.
    """

    env = os.environ if environ is None else environ
    dsn = (env.get(ENV_MEMORY_DSN) or "").strip() or None
    requested = (env.get(ENV_KNOWLEDGE_BACKEND) or "").strip()
    if not requested:
        return MemoryConfig(POSTGRES_BACKEND, dsn) if dsn else MemoryConfig(OFFLINE_BACKEND)
    if requested == OFFLINE_BACKEND:
        return MemoryConfig(OFFLINE_BACKEND)
    if requested == POSTGRES_BACKEND:
        if dsn is None:
            raise MemoryStoreError(
                "memory_dsn_missing",
                f"{ENV_KNOWLEDGE_BACKEND}={POSTGRES_BACKEND} exige {ENV_MEMORY_DSN}",
            )
        return MemoryConfig(POSTGRES_BACKEND, dsn)
    raise MemoryStoreError(
        "invalid_knowledge_backend",
        f"{ENV_KNOWLEDGE_BACKEND} deve ser um de: {', '.join(KNOWLEDGE_BACKENDS)}",
    )


def _check_identifier(field_name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) > IDENTIFIER_MAX_LENGTH
        or IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise MemoryStoreError("invalid_memory_scope", f"{field_name} não é um identificador válido")


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Escopo de leitura fixado pelo host (runner) a partir do RunManifest e da RoutingDecision.

    Nunca deve ser montado com valores vindos de argumentos do modelo. As APIs de
    leitura recebem o escopo só na construção e não aceitam família/papel por chamada.
    """

    catalog_id: str
    project_id: str
    family_id: str
    role_id: str

    def __post_init__(self) -> None:
        for _setting, field_name in SCOPE_SETTINGS:
            _check_identifier(field_name, getattr(self, field_name))
        if self.family_id not in FAMILY_IDS:
            raise MemoryStoreError("invalid_memory_scope", "family_id fora do enum de famílias")

    def settings(self) -> list[tuple[str, str]]:
        """Pares (variável, valor) aplicados com `set_config(..., true)` (equivale a `SET LOCAL`)."""

        return [(setting, getattr(self, field_name)) for setting, field_name in SCOPE_SETTINGS]
