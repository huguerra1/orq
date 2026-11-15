"""Escolha final de conhecimento pelo agente e consolidação em `ContextManifest` (7D).

Fluxo em dois passos, igual para os backends Postgres e offline:

1. `KnowledgeChoiceSession.search(requirement_id, query, limit)` devolve candidatos elegíveis
   (teto de 20) e registra o que foi devolvido;
2. `KnowledgeChoiceSession.choose({requirement_id: [unit_ref, ...]})` valida a escolha do agente
   e produz um `KnowledgeSelectionRecord`. Nada é truncado em silêncio: escolha fora da busca,
   requisito obrigatório descoberto ou orçamento estourado viram erro estruturado.

`materialize_chosen_context` relê o conteúdo pelo backend, confere o `content_digest` de cada
unidade e reutiliza `materialize_knowledge_context` (Fase 2): mesma ordem, mesmos digests JCS,
mesma procedência e bundle canônico reproduzível byte a byte.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_digest
from .context import MaterializedContext, materialize_knowledge_context
from .knowledge_backend import (
    SEARCH_LIMIT_MAX,
    KnowledgeBackend,
    KnowledgeCandidate,
    UnitKey,
    catalog_units,
    requirement_reasons,
    verify_unit_content,
)
from .selection import _estimated_tokens, _source_matches_ref
from .validator import validate_instance


SELECTION_MODE = "agent_mcp_choice"
_UNIT_REF_FIELDS = {"source_id", "revision", "unit_id"}


@dataclass(frozen=True, slots=True)
class KnowledgeChoiceError(Exception):
    """Erro estruturado da busca/escolha (vira o erro da tool MCP no 7E)."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


def _fail(code: str, path: str, message: str) -> None:
    raise KnowledgeChoiceError(code, path, message)


def _unit_ref(candidate: KnowledgeCandidate) -> dict[str, Any]:
    return {
        "source_id": candidate.source_id,
        "revision": candidate.revision,
        "unit_id": candidate.unit_id,
        "source_digest": {"algorithm": "sha256", "value": candidate.source_digest},
        "unit_digest": {"algorithm": "sha256", "value": candidate.unit_digest},
    }


def _parse_unit_ref(value: object, path: str) -> UnitKey:
    if not isinstance(value, Mapping) or set(value) != _UNIT_REF_FIELDS:
        _fail("invalid_unit_ref", path, "unit_ref deve ter exatamente source_id, revision e unit_id")
    if not all(isinstance(value[field], str) and value[field] for field in _UNIT_REF_FIELDS):
        _fail("invalid_unit_ref", path, "campos do unit_ref devem ser texto não vazio")
    return value["source_id"], value["revision"], value["unit_id"]


class KnowledgeChoiceSession:
    """Sessão de uma tentativa: busca, registro dos candidatos e validação da escolha do agente."""

    def __init__(
        self,
        backend: KnowledgeBackend,
        requirements: list[dict[str, Any]],
        *,
        run_id: str,
        workflow_ref: dict[str, Any],
        task_id: str,
        candidate_attempt_number: int,
        created_at: str,
        max_tokens: int,
        policy_snapshot_refs: list[dict[str, Any]],
        margin_tokens: int = 0,
        selection_id: str | None = None,
    ) -> None:
        if max_tokens < 1 or margin_tokens < 0:
            _fail("invalid_context_budget", "/budget", "max_tokens deve ser positivo e a margem não negativa")
        identifiers = [item["knowledge_requirement_id"] for item in requirements]
        if len(set(identifiers)) != len(identifiers):
            _fail("duplicate_requirement", "/requirements", "knowledge_requirement_id repetido")
        self._backend = backend
        self._requirements = requirements
        self._requirement_by_id = {item["knowledge_requirement_id"]: item for item in requirements}
        self._reasons = {
            item["knowledge_requirement_id"]: requirement_reasons(backend.catalog, item, backend.policy)
            for item in requirements
        }
        self._index = catalog_units(backend.catalog)
        self._meta = {
            "run_id": run_id,
            "workflow_ref": workflow_ref,
            "task_id": task_id,
            "candidate_attempt_number": candidate_attempt_number,
            "created_at": created_at,
            "policy_snapshot_refs": policy_snapshot_refs,
            "selection_id": selection_id or f"selection-{run_id}-{task_id}-{candidate_attempt_number}",
        }
        self._max_tokens = max_tokens
        self._margin_tokens = margin_tokens
        self._queries: list[str] = []
        self._found: dict[str, dict[UnitKey, KnowledgeCandidate]] = {item: {} for item in identifiers}
        self.selection: dict[str, Any] | None = None

    # --- passo 1: busca ---------------------------------------------------------------------

    def search(self, requirement_id: str, query: str, limit: int = SEARCH_LIMIT_MAX) -> list[KnowledgeCandidate]:
        requirement = self._requirement_by_id.get(requirement_id)
        if requirement is None:
            _fail("unknown_requirement", "/requirement_id", str(requirement_id))
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SEARCH_LIMIT_MAX:
            _fail("invalid_search_limit", "/limit", f"limit deve ser inteiro de 1 a {SEARCH_LIMIT_MAX}")
        candidates = self._backend.search(requirement, query, limit)
        if len(candidates) > limit:
            _fail("search_limit_exceeded", "/limit", "backend devolveu mais candidatos que o limite")
        for candidate in candidates:
            self._check_candidate(requirement_id, candidate)
        for candidate in candidates:
            known = self._found[requirement_id].get(candidate.key)
            if known is None or candidate.score > known.score:
                self._found[requirement_id][candidate.key] = candidate
        self._queries.append(query)
        return candidates

    def _check_candidate(self, requirement_id: str, candidate: KnowledgeCandidate) -> None:
        """Defesa em profundidade: o candidato precisa ser elegível e coincidir com o catálogo do host."""

        resolved = self._index.get(candidate.key)
        if resolved is None or self._reasons[requirement_id].get(candidate.key):
            _fail("candidate_not_eligible", "/candidates", str(candidate.key))
        _entry, source, unit = resolved
        if (
            candidate.source_digest != source["content_digest"]["value"]
            or candidate.unit_digest != unit["content_digest"]["value"]
            or candidate.estimated_tokens != _estimated_tokens(unit)
        ):
            _fail("integrity_mismatch", "/candidates", f"candidato diverge do catálogo: {candidate.key}")

    # --- passo 2: escolha -------------------------------------------------------------------

    def choose(self, selections: Mapping[str, Sequence[Mapping[str, str]]]) -> dict[str, Any]:
        """Valida a escolha do agente e devolve o `KnowledgeSelectionRecord` (também em `self.selection`)."""

        # Uma escolha nova invalida a anterior: se falhar, não sobra seleção publicada.
        self.selection = None
        if not isinstance(selections, Mapping):
            _fail("invalid_selection", "/selections", "selections deve mapear requisito para lista de unit_refs")
        chosen: dict[str, list[UnitKey]] = {}
        for requirement_id, refs in selections.items():
            if requirement_id not in self._requirement_by_id:
                _fail("unknown_requirement", f"/selections/{requirement_id}", "requisito inexistente na tarefa")
            if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
                _fail("invalid_selection", f"/selections/{requirement_id}", "esperada lista de unit_refs")
            keys: list[UnitKey] = []
            for position, ref in enumerate(refs):
                path = f"/selections/{requirement_id}/{position}"
                key = _parse_unit_ref(ref, path)
                if key in keys:
                    _fail("duplicate_unit_ref", path, str(key))
                if key not in self._found[requirement_id]:
                    _fail(
                        "unit_not_in_search_results",
                        path,
                        f"{key} não foi devolvida pela busca deste requisito neste escopo e catálogo",
                    )
                keys.append(key)
            # Ordem canônica (rank da busca), independente da ordem em que o agente listou.
            chosen[requirement_id] = sorted(keys, key=lambda item, rid=requirement_id: self._rank(rid, item)[0])

        ordered = sorted(enumerate(self._requirements), key=lambda pair: (not pair[1]["required"], pair[0]))
        coverage_status: dict[str, tuple[bool, str | None]] = {}
        for _, requirement in ordered:
            requirement_id = requirement["knowledge_requirement_id"]
            keys = chosen.get(requirement_id, [])
            satisfied, gap = self._coverage(requirement, keys)
            if not satisfied and requirement["required"]:
                _fail(
                    "mandatory_requirement_uncovered",
                    f"/selections/{requirement_id}",
                    f"requisito obrigatório {requirement_id} sem cobertura ({gap})",
                )
            coverage_status[requirement_id] = (satisfied, gap)
            limit = requirement["max_context_tokens"]
            used = sum(self._tokens(key) for key in keys)
            if limit is not None and used > limit:
                _fail(
                    "context_budget_exceeded",
                    f"/selections/{requirement_id}",
                    f"{used} tokens > limite do requisito ({limit})",
                )

        selected, total = self._order_selected(ordered, chosen)
        if total + self._margin_tokens > self._max_tokens:
            _fail(
                "context_budget_exceeded",
                "/selections",
                f"{total} + margem {self._margin_tokens} > {self._max_tokens} tokens",
            )
        self.selection = self._record(ordered, chosen, selected, total, coverage_status)
        return self.selection

    def _tokens(self, key: UnitKey) -> int:
        return _estimated_tokens(self._index[key][2])

    def _coverage(self, requirement: dict[str, Any], keys: list[UnitKey]) -> tuple[bool, str | None]:
        if requirement["coverage_rule"] == "all_source_refs" and requirement["source_refs"]:
            sources = [self._index[key][1] for key in keys]
            missing = [
                reference["id"]
                for reference in requirement["source_refs"]
                if not any(_source_matches_ref(source, reference) for source in sources)
            ]
            return (not missing), (None if not missing else "source_refs_uncovered")
        return (bool(keys), None if keys else "not_selected_by_agent")

    def _rank(self, requirement_id: str, key: UnitKey) -> tuple[int, KnowledgeCandidate]:
        ranked = sorted(self._found[requirement_id].values(), key=KnowledgeCandidate.tie_break)
        for position, candidate in enumerate(ranked, start=1):
            if candidate.key == key:
                return position, candidate
        raise KeyError(key)  # inalcançável: a escolha já foi validada contra `_found`

    def _order_selected(
        self,
        ordered: list[tuple[int, dict[str, Any]]],
        chosen: dict[str, list[UnitKey]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Ordem independe da ordem dada pelo agente: obrigatórios primeiro e, em cada requisito, por rank."""

        selected: list[dict[str, Any]] = []
        by_key: dict[UnitKey, dict[str, Any]] = {}
        total = 0
        for _, requirement in ordered:
            requirement_id = requirement["knowledge_requirement_id"]
            ranked = sorted(
                (self._rank(requirement_id, key) for key in chosen.get(requirement_id, [])),
                key=lambda pair: pair[0],
            )
            for rank, candidate in ranked:
                existing = by_key.get(candidate.key)
                if existing is not None:
                    existing["requirement_ids"].append(requirement_id)
                    continue
                item = {
                    "unit_ref": _unit_ref(candidate),
                    "score": candidate.score,
                    "rank": rank,
                    "selection_reason": "required_coverage" if requirement["required"] else "optional_coverage",
                    "requirement_ids": [requirement_id],
                }
                selected.append(item)
                by_key[candidate.key] = item
                total += self._tokens(candidate.key)
        return selected, total

    def _record(
        self,
        ordered: list[tuple[int, dict[str, Any]]],
        chosen: dict[str, list[UnitKey]],
        selected: list[dict[str, Any]],
        total: int,
        coverage_status: dict[str, tuple[bool, str | None]],
    ) -> dict[str, Any]:
        best: dict[UnitKey, KnowledgeCandidate] = {}
        for found in self._found.values():
            for key, candidate in found.items():
                if key not in best or candidate.score > best[key].score:
                    best[key] = candidate
        global_rank = {
            candidate.key: position
            for position, candidate in enumerate(sorted(best.values(), key=KnowledgeCandidate.tie_break), start=1)
        }
        candidates = []
        for key in self._index:  # ordem do catálogo, independente da ordem das buscas
            candidate = best.get(key)
            if candidate is None:
                continue
            results = []
            for requirement in self._requirements:
                requirement_id = requirement["knowledge_requirement_id"]
                reasons = self._reasons[requirement_id][key]
                found = self._found[requirement_id].get(key)
                results.append(
                    {
                        "knowledge_requirement_id": requirement_id,
                        "eligible": not reasons,
                        "reason_codes": reasons,
                        "score": found.score if found else None,
                        "rank": self._rank(requirement_id, key)[0] if found else None,
                    }
                )
            candidates.append(
                {
                    "unit_ref": _unit_ref(candidate),
                    "eligible": True,
                    "reason_codes": [],
                    "requirement_results": results,
                    "score": candidate.score,
                    "rank": global_rank[key],
                }
            )

        coverage = []
        for _, requirement in ordered:
            requirement_id = requirement["knowledge_requirement_id"]
            satisfied, gap = coverage_status[requirement_id]
            coverage.append(
                {
                    "knowledge_requirement_id": requirement_id,
                    "required": requirement["required"],
                    "status": "satisfied" if satisfied else "unsatisfied",
                    "unit_ids": list(dict.fromkeys(key[2] for key in chosen.get(requirement_id, []))),
                    "gap_reason": None if satisfied else gap,
                }
            )
        optional_unsatisfied = any(not item["required"] and item["status"] != "satisfied" for item in coverage)
        query_text = "\n".join(self._queries)
        record: dict[str, Any] = {
            "schema_version": "0.1",
            "selection_id": self._meta["selection_id"],
            "run_id": self._meta["run_id"],
            "workflow_ref": self._meta["workflow_ref"],
            "task_id": self._meta["task_id"],
            "candidate_attempt_number": self._meta["candidate_attempt_number"],
            "catalog_snapshot_ref": {
                "id": self._backend.catalog["catalog_id"],
                "version": self._backend.catalog["version"],
                "content_digest": self._backend.catalog["catalog_hash"],
            },
            "requirement_snapshots": copy.deepcopy(self._requirements),
            "query_input": {"text": query_text, "query_hash": canonical_digest(query_text)},
            "policy_snapshot_refs": self._meta["policy_snapshot_refs"],
            "candidates": candidates,
            "selected_units": selected,
            "coverage": coverage,
            "budget": {
                "max_tokens": self._max_tokens,
                "estimated_tokens": total,
                "tokenizer_ref": "char4-v1",
                "margin_tokens": self._margin_tokens,
            },
            "status": "partial" if optional_unsatisfied else "satisfied",
            "created_at": self._meta["created_at"],
            "backend": self._backend.backend_id,
            "selection_mode": SELECTION_MODE,
            "ranking_version": self._backend.ranking_version,
        }
        record["selection_hash"] = canonical_digest(record)
        issues = validate_instance(record, "knowledge_selection")
        if issues:
            issue = issues[0]
            _fail(issue.code, issue.path, issue.message)
        return record


def materialize_chosen_context(
    backend: KnowledgeBackend,
    selection: dict[str, Any],
    *,
    routing_decision_ref: dict[str, Any],
    target_ref: dict[str, Any],
    target_context_limit_tokens: int,
    reserved_output_tokens: int,
    margin_tokens: int = 0,
    provider_context_coverage: str = "platform_only",
    context_manifest_id: str | None = None,
) -> MaterializedContext:
    """Relê as unidades escolhidas pelo backend, confere os digests e materializa o `ContextManifest`.

    Falhas de leitura/integridade (`selected_unit_missing`, `unit_integrity_mismatch`,
    `integrity_mismatch`) saem como `VaultError`, antes de qualquer manifesto; erros de contrato
    saem como `ContextError`, ambos os mesmos da Fase 2.
    """

    if selection.get("backend") != backend.backend_id:
        _fail("backend_mismatch", "/selection/backend", "seleção produzida por outro backend")
    keys = [
        (item["unit_ref"]["source_id"], item["unit_ref"]["revision"], item["unit_ref"]["unit_id"])
        for item in selection["selected_units"]
    ]
    contents = backend.load_unit_contents(keys)
    index = catalog_units(backend.catalog)

    def unit_loader(source: dict[str, Any], unit: dict[str, Any]) -> str:
        key = (source["source_id"], source["revision"], unit["unit_id"])
        content = contents.get(key)
        if content is None or key not in index:
            _fail("selected_unit_missing", "/selection/selected_units", str(key))
        return verify_unit_content(unit, content)

    return materialize_knowledge_context(
        None,
        backend.catalog,
        selection,
        routing_decision_ref=routing_decision_ref,
        target_ref=target_ref,
        target_context_limit_tokens=target_context_limit_tokens,
        reserved_output_tokens=reserved_output_tokens,
        margin_tokens=margin_tokens,
        provider_context_coverage=provider_context_coverage,
        context_manifest_id=context_manifest_id,
        unit_loader=unit_loader,
    )
