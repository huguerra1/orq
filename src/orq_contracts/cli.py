"""CLI local para contratos, Vault e materialização de contexto."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Sequence

from .canonical import CanonicalizationError, canonical_digest, canonicalize
from ._storage import StorageError
from .planner import PlanningError
from .runner import SequentialRunner
from .simulated_executor import SimulationScript
from .context import ContextError, materialize_knowledge_context
from .memory_store import POSTGRES_BACKEND, MemoryStoreError, build_sync_plan, resolve_memory_config
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

    sync_db = vault_commands.add_parser(
        "sync-db", help="sincroniza um snapshot do Vault com o Postgres de ORQ_MEMORY_DSN"
    )
    sync_db.add_argument("--root", type=Path, required=True)
    sync_db.add_argument("--catalog", type=Path, required=True)

    run = subparsers.add_parser("run", help="executa um workflow local")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    simulate = run_commands.add_parser("simulate", help="executa com o executor simulado, sem rede")
    for flag in ("store-root", "workflow", "request", "catalog", "policy", "script", "knowledge-catalog", "vault-root"):
        simulate.add_argument("--" + flag, type=Path, required=True)
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


def _sync_db(arguments: argparse.Namespace) -> dict[str, Any]:
    config = resolve_memory_config()
    if config.backend != POSTGRES_BACKEND:
        raise MemoryStoreError("memory_dsn_missing", "defina ORQ_MEMORY_DSN para sincronizar com o Postgres")
    plan = build_sync_plan(arguments.root, _read_json(arguments.catalog))
    # Import tardio: o driver só é carregado quando há banco configurado.
    from .memory_store.postgres import apply_schema, connect, sync_catalog

    with connect(config) as connection:
        schema_status = apply_schema(connection)
        result = sync_catalog(connection, plan)
    return {
        "status": "ok",
        "schema": schema_status,
        "sync": result.status,
        "catalog_id": result.catalog_id,
        "sync_digest": result.sync_digest,
        "source_count": result.source_count,
        "unit_count": result.unit_count,
    }


def _run_vault(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.vault_command == "catalog":
        result = build_catalog_snapshot(arguments.root, _read_json(arguments.definition))
        _write_exclusive(arguments.output, _json_bytes(result))
        return {"status": "ok", "output": str(arguments.output), "catalog_hash": result["catalog_hash"]}

    if arguments.vault_command == "sync-db":
        return _sync_db(arguments)

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

        if arguments.command == "run":
            document = _read_json(arguments.script)
            script = SimulationScript(document, expected_digest=canonical_digest(document))
            with SequentialRunner.create(arguments.store_root, workflow=_read_json(arguments.workflow),
                    request=_read_json(arguments.request), catalog=_read_json(arguments.catalog),
                    policy=_read_json(arguments.policy), script=script,
                    knowledge_catalog=_read_json(arguments.knowledge_catalog), vault_root=arguments.vault_root,
                    clock=script.make_clock()) as runner:
                result = runner.run()
                print(canonicalize({"state": result.state, "run_id": result.id,
                                    "journal_digest": runner.journal.final_digest}).decode("utf-8"))
                return 0 if result.state == "succeeded" else 1

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
        MemoryStoreError,
        StorageError,
        PlanningError,
    ) as error:
        result = {"valid": False, "error": str(error)}
        if arguments.command == "run" and hasattr(error, "code"):
            result["code"] = error.code
        print(canonicalize(result).decode("utf-8") if arguments.command == "run" else json.dumps(result))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
