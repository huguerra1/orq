"""Vault de teste em português para a busca e a escolha do 7D (usado com e sem banco)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _memory_vault import PROJECT, audience, rehash, source_metadata, status_assertion, versioned_ref
from orq_contracts.vault import build_catalog_snapshot


CREATED_AT = "2026-11-14T12:00:00Z"
PROJECT_SCOPE = {"kind": "project", "project_id": PROJECT}

# (source_id, audiência ou None, texto Markdown). Uma seção por fonte, logo uma unidade por fonte.
SEARCH_SOURCES: list[tuple[str, dict[str, Any] | None, str]] = [
    (
        "recon",
        None,
        "# Reconciliação\n\nA reconciliação de tentativas expiradas roda a cada ciclo do runner."
        " Use a flag --trust-tools=fs_read para limitar as ferramentas confiáveis.\n",
    ),
    (
        "config",
        None,
        "# Configuração\n\nA configuração do pool de threads define o limite de execuções paralelas.\n",
    ),
    (
        "ident",
        None,
        "# Registros\n\nO tipo QuotaStateRecord guarda o estado de cota de cada assinatura.\n",
    ),
    ("git", None, "# Git\n\nCommits pequenos, revisados e com mensagens descritivas.\n"),
    (
        "codex-only",
        audience(["family_codex"]),
        "# Sandbox do Codex\n\nA reconciliação no sandbox do Codex usa threads exclusivas e a configuração do pool.\n",
    ),
    (
        "claude-only",
        audience(["family_claude"]),
        "# Permissões do Claude\n\nA reconciliação das permissões do Claude Code segue regras próprias.\n",
    ),
]


def requirement(
    identifier: str,
    subject: str = "tema",
    *,
    required: bool = True,
    max_context_tokens: int | None = None,
) -> dict[str, Any]:
    return {
        "knowledge_requirement_id": identifier,
        "subject": subject,
        "required": required,
        "allowed_kinds": ["project"],
        "required_authority_scopes": ["project_architecture"],
        "source_refs": [],
        "applicability_filters": {"topics": [], "paths": [], "domain": None, "roles": [], "task_types": []},
        "freshness": None,
        "coverage_rule": "at_least_one",
        "max_context_tokens": max_context_tokens,
    }


def build_search_vault(root: Path, catalog_id: str) -> dict[str, Any]:
    """Escreve as fontes, monta o snapshot pela Fase 2 e devolve o catálogo."""

    sources = []
    assertions = []
    for source_id, source_audience, text in SEARCH_SOURCES:
        raw = text.encode("utf-8")
        path = f"content/{source_id}.md"
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        metadata = source_metadata(source_id, raw, path, PROJECT_SCOPE, "project", source_audience)
        sources.append(metadata)
        assertions.append(status_assertion(metadata))
    definition = {
        "catalog_id": catalog_id,
        "version": "1",
        "created_at": CREATED_AT,
        "status_cutoff": CREATED_AT,
        "catalog_policy_ref": versioned_ref("catalog-policy"),
        "parser_chunker_snapshots": [versioned_ref("markdown-heading-sections")],
        "vocabulary_refs": [],
        "sources": sources,
        "status_assertions": assertions,
    }
    snapshot = build_catalog_snapshot(root, definition)
    assert not snapshot["exclusions"], snapshot["exclusions"]
    return rehash(snapshot)


def unit_ref(source_id: str, catalog: dict[str, Any]) -> dict[str, str]:
    """`unit_ref` mínimo (como o agente envia) da primeira unidade da fonte."""

    for entry in catalog["source_entries"]:
        metadata = entry["source_metadata"]
        if metadata["source_id"] == source_id:
            return {
                "source_id": source_id,
                "revision": metadata["revision"],
                "unit_id": entry["units"][0]["unit_id"],
            }
    raise KeyError(source_id)


SESSION_ARGUMENTS: dict[str, Any] = {
    "run_id": "run-7d",
    "workflow_ref": versioned_ref("workflow-7d"),
    "task_id": "T1",
    "candidate_attempt_number": 1,
    "created_at": CREATED_AT,
    "max_tokens": 1000,
    "policy_snapshot_refs": [versioned_ref("selection-policy")],
}

MATERIALIZE_ARGUMENTS: dict[str, Any] = {
    "routing_decision_ref": versioned_ref("routing-7d"),
    "target_ref": versioned_ref("target-7d"),
    "target_context_limit_tokens": 2000,
    "reserved_output_tokens": 200,
    "margin_tokens": 10,
}
