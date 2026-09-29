"""CLI local para contratos, Vault e materialização de contexto."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Sequence

from .canonical import CanonicalizationError, canonical_digest
from .context import ContextError, materialize_knowledge_context
from .selection import select_knowledge
from .validator import (
    validate_context_manifest,
    validate_instance,
    validate_knowledge_source,
    validate_task,
    validate_workflow,
)
from .vault import VaultError, build_catalog_snapshot


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _json_bytes(document: Any) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_exclusive(path: Path, content: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"saída já existe: {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"diretório de saída ausente: {path.parent}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_context_directory(output: Path, materialization: Any) -> None:
    if output.exists():
        raise FileExistsError(f"diretório de saída já existe: {output}")
    if not output.parent.is_dir():
        raise FileNotFoundError(f"diretório pai ausente: {output.parent}")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        (staging / "items").mkdir()
        (staging / "manifest.json").write_bytes(_json_bytes(materialization.manifest))
        (staging / "bundle.json").write_bytes(materialization.bundle)
        for item_id, content in materialization.item_contents.items():
            (staging / "items" / f"{item_id}.bin").write_bytes(content)
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orq-contracts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="valida um contrato")
    validate.add_argument(
        "kind",
        choices=(
            "task",
            "workflow",
            "knowledge_source",
            "knowledge_catalog",
            "knowledge_selection",
            "context_manifest",
        ),
    )
    validate.add_argument("path", type=Path)

    digest = subparsers.add_parser("digest", help="calcula SHA-256 sobre JCS")
    digest.add_argument("path", type=Path)

    vault = subparsers.add_parser("vault", help="opera a cadeia local de conhecimento")
    vault_commands = vault.add_subparsers(dest="vault_command", required=True)

    catalog = vault_commands.add_parser("catalog", help="constrói um snapshot do catálogo")
    catalog.add_argument("--root", type=Path, required=True)
    catalog.add_argument("--definition", type=Path, required=True)
    catalog.add_argument("--output", type=Path, required=True)

    select = vault_commands.add_parser("select", help="seleciona conhecimento do catálogo")
    select.add_argument("--root", type=Path, required=True)
    select.add_argument("--catalog", type=Path, required=True)
    select.add_argument("--request", type=Path, required=True)
    select.add_argument("--output", type=Path, required=True)

    context = vault_commands.add_parser("context", help="materializa ContextManifest e bundle")
    context.add_argument("--root", type=Path, required=True)
    context.add_argument("--catalog", type=Path, required=True)
    context.add_argument("--selection", type=Path, required=True)
    context.add_argument("--request", type=Path, required=True)
    context.add_argument("--output-dir", type=Path, required=True)
    return parser


def _validate(kind: str, document: Any):
    validators = {
        "task": validate_task,
        "workflow": validate_workflow,
        "knowledge_source": validate_knowledge_source,
        "knowledge_catalog": lambda value: validate_instance(value, "knowledge_catalog"),
        "knowledge_selection": lambda value: validate_instance(value, "knowledge_selection"),
        "context_manifest": validate_context_manifest,
    }
    return validators[kind](document)


def _run_vault(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.vault_command == "catalog":
        result = build_catalog_snapshot(arguments.root, _read_json(arguments.definition))
        _write_exclusive(arguments.output, _json_bytes(result))
        return {"status": "ok", "output": str(arguments.output), "catalog_hash": result["catalog_hash"]}

    catalog = _read_json(arguments.catalog)
    request = _read_json(arguments.request)
    if arguments.vault_command == "select":
        request = dict(request)
        request["allowed_collections"] = set(request.get("allowed_collections", []))
        result = select_knowledge(arguments.root, catalog, **request)
        _write_exclusive(arguments.output, _json_bytes(result))
        return {"status": "ok", "output": str(arguments.output), "selection_hash": result["selection_hash"]}

    selection = _read_json(arguments.selection)
    result = materialize_knowledge_context(arguments.root, catalog, selection, **request)
    _write_context_directory(arguments.output_dir, result)
    return {
        "status": "ok",
        "output_dir": str(arguments.output_dir),
        "bundle_hash": result.manifest["bundle_hash"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "digest":
            document = _read_json(arguments.path)
            print(json.dumps(canonical_digest(document), sort_keys=True))
            return 0

        if arguments.command == "vault":
            print(json.dumps(_run_vault(arguments), ensure_ascii=False, indent=2))
            return 0

        document = _read_json(arguments.path)
        issues = _validate(arguments.kind, document)
        result = {
            "valid": not issues,
            "errors": [issue.as_dict() for issue in issues],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not issues else 1
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        CanonicalizationError,
        VaultError,
        ContextError,
    ) as error:
        print(json.dumps({"valid": False, "error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
