"""Sessão MCP de uma tentativa: escopo confiável e trilha durável antes da resposta.

A configuração é publicada pelo runner fora do workspace. Cada chamada publica
intenção e resultado em arquivos exclusivos; uma chamada interrompida bloqueia a
selagem, nunca é repetida automaticamente. O catálogo/Vault permanecem só leitura.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from threading import RLock
import sys

from ._storage import confined_path, ensure_directory, publish_bytes
from .canonical import canonical_digest, canonicalize
from .context import ContextError
from .knowledge_backend import catalog_units, requirement_reasons, scope_allows, verify_unit_content
from .knowledge_choice import KnowledgeChoiceError, KnowledgeChoiceSession, materialize_chosen_context
from .memory_store.config import MemoryScope, MemoryStoreError
from .vault import VaultError


TOOLS = {"search_family_knowledge", "select_family_knowledge"}


class _SessionBackend:
    """Interseção adicional das revogações do run com a visibilidade do backend."""
    def __init__(self, backend, allowed):
        self.backend, self.allowed = backend, allowed

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def search(self, requirement, query, limit):
        return [candidate for candidate in self.backend.search(requirement, query, limit) if candidate.key in self.allowed]

    def load_unit_contents(self, keys):
        if any(key not in self.allowed for key in keys):
            raise VaultError("selected_unit_missing", "", "unidade fora do escopo efetivo")
        return self.backend.load_unit_contents(keys)


def validate_audit(entries, session_ref):
    """Confere cadeia e bytes da única entrega, sem I/O (também usado no replay)."""
    previous, delivered, found = None, None, {}
    for number, entry in enumerate(entries, 1):
        intent = {k: entry[k] for k in ("session_ref", "sequence", "tool", "arguments", "recorded_at")}
        if (entry["sequence"] != number or entry["session_ref"] != session_ref or
                entry["previous_hash"] != previous or entry["intent_hash"] != canonical_digest(intent) or
                entry["entry_hash"] != canonical_digest({k: v for k, v in entry.items() if k != "entry_hash"})):
            raise ValueError("knowledge_audit_integrity_mismatch")
        previous = entry["entry_hash"]
        response = entry["response"]
        if entry["tool"] == "search_family_knowledge" and "error" not in response:
            rid = entry["arguments"]["requirement_id"]
            for candidate in response["candidates"]:
                found[(rid, candidate["source_id"], candidate["revision"], candidate["unit_id"])] = candidate
        if entry["tool"] == "select_family_knowledge" and "error" not in response:
            from .executor import bytes_digest
            if delivered or bytes_digest(canonicalize(response["bundle"])) != response["manifest"]["bundle_hash"]:
                raise ValueError("knowledge_audit_integrity_mismatch")
            for selected in response["selection"]["selected_units"]:
                ref = selected["unit_ref"]
                for rid in selected["requirement_ids"]:
                    candidate = found.get((rid, ref["source_id"], ref["revision"], ref["unit_id"]))
                    if not candidate or candidate["content_digest"] != ref["unit_digest"]:
                        raise ValueError("knowledge_audit_integrity_mismatch")
            for manifest_item, bundle_item in zip(response["manifest"]["items"], response["bundle"]["items"], strict=True):
                if (manifest_item["item_id"] != bundle_item["item_id"] or
                        bytes_digest(bundle_item["content"].encode()) != manifest_item["content_hash"]):
                    raise ValueError("knowledge_audit_integrity_mismatch")
            delivered = response
    return delivered


class AttemptKnowledgeSession:
    def __init__(self, directory: Path, config: dict, backend, *, clock):
        self.directory, self.config, self.backend = Path(directory), deepcopy(config), backend
        self.scope = MemoryScope(**self.config["scope"])
        if backend.catalog["catalog_id"] != self.scope.catalog_id or backend.policy.project_id != self.scope.project_id:
            raise ValueError("knowledge_scope_mismatch")
        backend_scope = getattr(backend, "scope", getattr(getattr(backend, "reader", None), "scope", None))
        if backend_scope != self.scope:
            raise ValueError("knowledge_scope_mismatch")
        self.clock, self._lock = clock, RLock()
        ensure_directory(self.directory)
        self.reference = {"id": config["session_id"], "version": "0.1", "content_digest": canonical_digest(config)}
        path = confined_path(self.directory, "config.json")
        if not publish_bytes(path, canonicalize(config)) and json.loads(path.read_bytes()) != config:
            raise ValueError("knowledge_session_mismatch")
        self.backend = _SessionBackend(backend, set(catalog_units(self.visible_catalog())))
        self.choice = KnowledgeChoiceSession(self.backend, config["requirements"], **config["choice"])
        self.selection = self.context = None
        self._closed = (self.directory / "closed.json").exists()
        # Reinício de servidor não pode permitir uma segunda seleção ou esquecer
        # candidatos já devolvidos; restaura a partir da trilha, sem nova busca.
        from .knowledge_backend import KnowledgeCandidate
        index = catalog_units(backend.catalog)
        for entry in self.entries():
            response = entry["response"]
            if "error" in response:
                continue
            if entry["tool"] == "search_family_knowledge":
                rid = entry["arguments"]["requirement_id"]
                self.choice._queries.append(entry["arguments"]["query"])
                for item in response["candidates"]:
                    key = item["source_id"], item["revision"], item["unit_id"]
                    _, source, unit = index[key]
                    candidate = KnowledgeCandidate(*key, ordinal=unit["ordinal"],
                        source_digest=source["content_digest"]["value"], unit_digest=item["content_digest"]["value"],
                        area_scope=item["area_scope"], unit_kind=item["unit_kind"], heading_path=tuple(item["heading_path"]),
                        estimated_tokens=item["estimated_tokens"], score=item["score"], excerpt=item["excerpt"])
                    self.choice._found[rid][key] = candidate
            elif entry["tool"] == "select_family_knowledge":
                self.selection = response["selection"]

    def stdio_launch(self, vault_root, *, python_executable=None, path_mapper=str):
        """Descrição de processo para a conexão MCP nativa da CLI (host confiável).

        path_mapper permite mapear C:\\dev para /mnt/c no Kiro/WSL. O DSN nunca
        entra nesta configuração: é recebido pelo processo MCP no ambiente do host.
        """
        catalog_path = self.directory / "catalog.json"
        content = canonicalize(self.backend.catalog)
        if not publish_bytes(catalog_path, content) and catalog_path.read_bytes() != content:
            raise ValueError("knowledge_session_mismatch")
        return {"command": python_executable or sys.executable,
                "args": ["-m", "orq_contracts.mcp_server", "--root", path_mapper(vault_root),
                         "--catalog", path_mapper(catalog_path), "--attempt-session", path_mapper(self.directory),
                         "--session-digest", self.reference["content_digest"]["value"]]}

    def entries(self):
        previous, entries = None, []
        for number, path in enumerate(sorted(self.directory.glob("call-*.json")), 1):
            entry = json.loads(confined_path(self.directory, path.name).read_bytes())
            if (path.name != f"call-{number:06d}.json" or entry["sequence"] != number
                    or entry["session_ref"] != self.reference or entry["previous_hash"] != previous
                    or entry["entry_hash"] != canonical_digest({k: v for k, v in entry.items() if k != "entry_hash"})):
                raise ValueError("knowledge_audit_integrity_mismatch")
            previous = entry["entry_hash"]
            entries.append(entry)
        # Há no máximo uma intenção pendente: nunca presumir que seu resultado foi entregue.
        if len(list(self.directory.glob("intent-*.json"))) != len(entries):
            raise ValueError("knowledge_call_indeterminate")
        for entry in entries:
            intent = json.loads((self.directory / f"intent-{entry['sequence']:06d}.json").read_bytes())
            if canonical_digest(intent) != entry["intent_hash"]:
                raise ValueError("knowledge_audit_integrity_mismatch")
        return entries

    def call(self, name: str, arguments: dict):
        with self._lock:
            entries = self.entries()
            number = len(entries) + 1
            if number > 100:
                raise ValueError("knowledge_call_limit")
            intent = {"session_ref": self.reference, "sequence": number, "tool": name,
                      "arguments": deepcopy(arguments), "recorded_at": self.clock()}
            if len(canonicalize(intent)) > 65536:
                raise ValueError("knowledge_arguments_too_large")
            if not publish_bytes(self.directory / f"intent-{number:06d}.json", canonicalize(intent)):
                raise ValueError("knowledge_call_conflict")
            try:
                if self._closed:
                    raise KnowledgeChoiceError("knowledge_session_closed", "", "tentativa já encerrada")
                response = self._invoke(name, arguments)
            except (KnowledgeChoiceError, VaultError, ContextError, MemoryStoreError) as error:
                response = {"error": {"code": error.code, "path": getattr(error, "path", ""),
                                      "message": "Busca/seleção recusada pelo host."}}
            entry = {**intent, "intent_hash": canonical_digest(intent), "response": response,
                     "previous_hash": entries[-1]["entry_hash"] if entries else None}
            entry["entry_hash"] = canonical_digest(entry)
            if not publish_bytes(self.directory / f"call-{number:06d}.json", canonicalize(entry)):
                raise ValueError("knowledge_call_conflict")
            return deepcopy(response)

    def _invoke(self, name, arguments):
        allowed = {"requirement_id", "query", "limit"} if name == "search_family_knowledge" else {"selections", "rationale"}
        required = {"requirement_id", "query"} if name == "search_family_knowledge" else {"selections"}
        if name not in TOOLS or not isinstance(arguments, dict) or set(arguments) - allowed or required - set(arguments):
            raise KnowledgeChoiceError("invalid_tool_arguments", "", "argumentos desconhecidos/incompletos")
        if name == "search_family_knowledge":
            query = arguments["query"]
            if not isinstance(query, str) or not query.strip() or len(query) > 512 or "\0" in query:
                raise KnowledgeChoiceError("invalid_search_query", "/query", "query inválida")
            if not isinstance(arguments["requirement_id"], str):
                raise KnowledgeChoiceError("unknown_requirement", "/requirement_id", "requisito inválido")
            candidates = self.choice.search(arguments["requirement_id"], query, arguments.get("limit", 20))
            return {"candidates": [candidate.public() for candidate in candidates]}
        rationale = arguments.get("rationale")
        if rationale is not None and (not isinstance(rationale, str) or len(rationale) > 1024):
            raise KnowledgeChoiceError("invalid_rationale", "/rationale", "máximo 1024 caracteres")
        if self.selection is not None:
            raise KnowledgeChoiceError("selection_already_closed", "", "uma entrega final por tentativa")
        selection = self.choice.choose(arguments["selections"])
        context = materialize_chosen_context(self.backend, selection, **self.config["materialize"])
        self.selection, self.context = selection, context
        return {"selection": selection, "manifest": context.manifest,
                "bundle": json.loads(context.bundle), "selection_id": selection["selection_id"]}

    def visible_catalog(self):
        snapshot = deepcopy(self.backend.catalog)
        reasons = [requirement_reasons(snapshot, r, self.backend.policy) for r in self.config["requirements"]]
        for entry in snapshot["source_entries"]:
            source = entry["source_metadata"]
            from .simulated_executor import parse_utc
            source_ref = {"source_id": source["source_id"], "revision": source["revision"], "content_digest": source["content_digest"]}
            assertions = [a for a in (*snapshot["status_assertions"], *self.config.get("knowledge_status_assertions", []))
                          if a["source_ref"] == source_ref and parse_utc(a["effective_at"]) <= parse_utc(self.config["eligibility"]["as_of"])]
            latest = max((parse_utc(a["effective_at"]) for a in assertions), default=None)
            revoked = any(a["status"] != "active" and parse_utc(a["effective_at"]) == latest for a in assertions)
            entry["units"] = [u for u in entry["units"] if scope_allows(entry, source, u, self.scope)
                              and not revoked
                              and any(not rs[(source["source_id"], source["revision"], u["unit_id"])] for rs in reasons)]
        snapshot["source_entries"] = [e for e in snapshot["source_entries"] if e["units"]]
        return snapshot

    def preflight(self):
        """Confere bytes elegíveis sem escolher memória nem entregar conteúdo ao agente."""
        index = catalog_units(self.visible_catalog())
        from .selection import _source_matches_ref
        for requirement in self.config["requirements"]:
            if not requirement["required"]:
                continue
            reasons = requirement_reasons(self.backend.catalog, requirement, self.backend.policy)
            available = [resolved[1] for key, resolved in index.items() if not reasons[key]]
            if not available or (requirement["coverage_rule"] == "all_source_refs" and any(
                    not any(_source_matches_ref(source, ref) for source in available) for ref in requirement["source_refs"])):
                raise VaultError("knowledge_unsatisfied", "", "requisito sem unidades visíveis elegíveis")
        contents = self.backend.load_unit_contents(list(index))
        for key, (_, _, unit) in index.items():
            if key not in contents:
                raise VaultError("selected_unit_missing", "", "unidade ausente")
            verify_unit_content(unit, contents[key])

    def seal(self):
        with self._lock:
            entries = self.entries()
            validate_audit(entries, self.reference)
            # Só o resultado durável da tool é fonte de verdade; não relê bytes
            # mutáveis após a execução nem repete consultas no Postgres.
            delivered = [e["response"] for e in entries if e["tool"] == "select_family_knowledge"
                         and "error" not in e["response"]]
            if len(delivered) > 1:
                raise ValueError("knowledge_audit_integrity_mismatch")
            result = delivered[0] if delivered else None
            closed = {"session_ref": self.reference, "audit_hash": canonical_digest(entries),
                      "selection_hash": result["selection"]["selection_hash"] if result else None}
            path = self.directory / "closed.json"
            if not publish_bytes(path, canonicalize(closed)) and json.loads(path.read_bytes()) != closed:
                raise ValueError("knowledge_audit_integrity_mismatch")
            self._closed = True
            return result, entries, closed
