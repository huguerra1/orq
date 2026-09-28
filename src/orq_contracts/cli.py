"""CLI mínima para validar e calcular digests dos contratos."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .canonical import CanonicalizationError, canonical_digest
from .validator import validate_task, validate_workflow


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orq-contracts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="valida um contrato")
    validate.add_argument("kind", choices=("task", "workflow"))
    validate.add_argument("path", type=Path)

    digest = subparsers.add_parser("digest", help="calcula SHA-256 sobre JCS")
    digest.add_argument("path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        document = _read_json(arguments.path)
        if arguments.command == "digest":
            print(json.dumps(canonical_digest(document), sort_keys=True))
            return 0

        issues = validate_task(document) if arguments.kind == "task" else validate_workflow(document)
        result = {
            "valid": not issues,
            "errors": [issue.as_dict() for issue in issues],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not issues else 1
    except (OSError, json.JSONDecodeError, CanonicalizationError) as error:
        print(json.dumps({"valid": False, "error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
