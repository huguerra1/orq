"""Checagens determinísticas do experimento 7F sobre a resposta JSON de uma tarefa.

Stdlib apenas. Cada checagem é um objeto `{check_id, kind, ...}`; o resultado é
sempre reprodutível (sem LLM). Tipos:

- `equals`: valor em `pointer` igual a `value` (tipo JSON estrito: `true` não é `1`).
- `set_equals`: lista em `pointer` com os mesmos elementos de `values`, sem repetição.
- `contains_all`: lista em `pointer` contém todos os `values`.
- `matches`: texto em `pointer` casa por completo com a regex `pattern`.
- `no_forbidden_text`: nenhum dos `values` aparece como valor textual exato da resposta
  nem como substring de algum valor textual (marcadores de contaminação por distratores).
"""

from __future__ import annotations

import re
from typing import Any

KINDS = {"equals", "set_equals", "contains_all", "matches", "no_forbidden_text"}
_MISSING = object()


def resolve_pointer(document: Any, pointer: str) -> Any:
    """Resolve um JSON Pointer (RFC 6901) simples; devolve `_MISSING` se ausente."""

    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError(f"pointer inválido: {pointer!r}")
    current = document
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def strict_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(strict_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for child in value.values() for text in _strings(child)]
    if isinstance(value, list):
        return [text for child in value for text in _strings(child)]
    return []


def run_check(answer: Any, check: dict[str, Any]) -> tuple[bool, str]:
    kind = check["kind"]
    if kind not in KINDS:
        raise ValueError(f"tipo de checagem desconhecido: {kind}")
    if kind == "no_forbidden_text":
        texts = _strings(answer)
        hits = sorted({needle for needle in check["values"] if any(needle in text for text in texts)})
        return (not hits, "ocorrências proibidas: " + ", ".join(hits) if hits else "ok")
    actual = resolve_pointer(answer, check["pointer"])
    if actual is _MISSING:
        return False, "campo ausente"
    if kind == "equals":
        return strict_equal(actual, check["value"]), "ok" if strict_equal(actual, check["value"]) else "valor diferente"
    if kind == "matches":
        passed = isinstance(actual, str) and re.fullmatch(check["pattern"], actual) is not None
        return passed, "ok" if passed else "texto não casa"
    if not isinstance(actual, list):
        return False, "esperava lista"
    if kind == "set_equals":
        wanted = check["values"]
        passed = len(actual) == len(set(map(repr, actual))) and sorted(map(repr, actual)) == sorted(map(repr, wanted))
        return passed, "ok" if passed else "conjunto diferente"
    passed = all(any(strict_equal(item, wanted) for item in actual) for wanted in check["values"])
    return passed, "ok" if passed else "faltam elementos"


def evaluate_checks(answer: Any, checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for check in checks:
        passed, detail = run_check(answer, check)
        results.append({"check_id": check["check_id"], "passed": passed, "detail": detail})
    return results


def all_passed(answer: Any, checks: list[dict[str, Any]]) -> bool:
    return all(item["passed"] for item in evaluate_checks(answer, checks))
