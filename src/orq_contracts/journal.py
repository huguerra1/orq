"""Journal JSONL/JCS append-only, com um escritor exclusivo por run."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
from typing import Any, Callable
from uuid import uuid4

from ._storage import (
    StorageError, confined_path, ensure_directory, fsync_directory, publish_bytes,
    reject_pending_files, sync_file,
)
from .canonical import CanonicalizationError, canonical_digest, canonicalize
from .validator import validate_instance


MAX_JOURNAL_LINE_BYTES = 1024 * 1024


class JournalError(StorageError):
    """Falha tipada do journal; transições e ErrorRecord pertencem ao runner."""


def _confined_path(root: Path, *parts: str) -> Path:
    return confined_path(root, *parts, error_type=JournalError)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_id(sequence: int) -> str:
    return f"event-{sequence}"


def _digest_bytes(content: bytes) -> dict[str, str]:
    return {"algorithm": "sha256", "value": hashlib.sha256(content).hexdigest()}


def _invalid_json_constant(value: str) -> None:
    raise ValueError(f"constante fora de JSON: {value}")


class RunJournal:
    """Use create/open e close, preferencialmente como context manager.

    expected_aggregate_sequence é a próxima sequência, começando em 1.
    As cópias retornadas não permitem modificar os índices internos.
    """

    def __init__(
        self, directory: Path, run_id: str, *, clock: Callable[[], str],
        event_id_factory: Callable[[int], str],
    ) -> None:
        self.directory = directory
        self.run_id = run_id
        self.path = _confined_path(directory, "journal.jsonl")
        self._lock_path = _confined_path(directory, "run.lock")
        self._lock_token = canonicalize({
            "pid": os.getpid(), "host": socket.gethostname(),
            "created_at": _utc_now(), "uuid": str(uuid4()),
        })
        self._clock = clock
        self._event_id_factory = event_id_factory
        self._events: list[dict[str, Any]] = []
        self._aggregates: dict[tuple[str, str], int] = {}
        self._event_ids: set[str] = set()
        self._idempotency: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
        self._file = None
        self._owns_lock = False
        self._write_failed = False

    @staticmethod
    def _directory(store_root: Path, run_id: str) -> Path:
        # ':' é válido no contrato, mas denota ADS no Windows; não vira path.
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,127}", run_id):
            raise JournalError("invalid_run_id", "run_id não é um componente de caminho seguro")
        if run_id.endswith(".") or run_id.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }:
            raise JournalError("invalid_run_id", "run_id reservado pelo filesystem")
        directory = _confined_path(Path(store_root).resolve(), "runs", run_id)
        if directory.parent.is_dir():
            for entry in directory.parent.iterdir():
                if entry.name.casefold() == run_id.casefold() and entry.name != run_id:
                    raise JournalError("run_id_case_collision", f"run_id colide por caixa com {entry.name}")
        return directory

    @classmethod
    def create(
        cls, store_root: Path, run_id: str, *, clock: Callable[[], str] = _utc_now,
        event_id_factory: Callable[[int], str] = _event_id,
    ) -> RunJournal:
        directory = cls._directory(store_root, run_id)
        ensure_directory(directory.parent)
        try:
            directory.mkdir()
        except FileExistsError as error:
            raise JournalError("run_exists", "diretório do run já existe") from error
        journal = cls(directory, run_id, clock=clock, event_id_factory=event_id_factory)
        created_file = False
        try:
            fsync_directory(directory.parent)
            journal._acquire_lock()
            with journal.path.open("xb") as handle:
                created_file = True
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(directory)
            journal._file = journal.path.open("ab", buffering=0)
        except BaseException:
            try:
                journal.close()
            finally:
                # Só desfaz a criação desta chamada; nunca remove conteúdo alheio.
                if created_file:
                    journal.path.unlink()
                if not any(directory.iterdir()):
                    directory.rmdir()
                    fsync_directory(directory.parent)
            raise
        return journal

    @classmethod
    def open(
        cls, store_root: Path, run_id: str, *, clock: Callable[[], str] = _utc_now,
        event_id_factory: Callable[[int], str] = _event_id,
    ) -> RunJournal:
        directory = cls._directory(store_root, run_id)
        if not directory.is_dir():
            raise JournalError("run_not_found", "diretório do run ausente")
        journal = cls(directory, run_id, clock=clock, event_id_factory=event_id_factory)
        try:
            journal._acquire_lock()
            if not journal.path.is_file():
                raise JournalError("run_incomplete", "run sem journal.jsonl; criação incompleta")
            tail = journal._verify()
            sync_file(journal.path)
            fsync_directory(directory.parent)
            journal._file = journal.path.open("ab", buffering=0)
            journal._recover(tail)
        except BaseException:
            journal.close()
            raise
        return journal

    def _acquire_lock(self) -> None:
        try:
            handle = self._lock_path.open("xb")
        except FileExistsError as error:
            raise JournalError("run_locked", "run.lock existente; remoção órfã é manual") from error
        self._owns_lock = True
        identity = os.fstat(handle.fileno())
        try:
            with handle:
                handle.write(self._lock_token)
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(self.directory)
        except BaseException:
            # A aquisição não retornou: limpa inclusive token parcialmente escrito,
            # mas somente se o arquivo ainda for o criado por esta instância.
            handle.close()
            try:
                current = self._lock_path.stat()
            except FileNotFoundError:
                current = None
            self._owns_lock = False
            if current is not None and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                self._lock_path.unlink()
                fsync_directory(self.directory)
            raise

    def __enter__(self) -> RunJournal:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._owns_lock and not self._write_failed:
            same_token = self._has_lock_token()
            self._owns_lock = False
            if same_token:
                self._lock_path.unlink()
                fsync_directory(self.directory)

    def _has_lock_token(self) -> bool:
        try:
            return self._lock_path.read_bytes() == self._lock_token
        except FileNotFoundError:
            return False

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._events))

    @property
    def final_digest(self) -> dict[str, str] | None:
        return copy.deepcopy(self._events[-1]["event_hash"]) if self._events else None

    def _require_writer(self) -> None:
        if self._file is None or self._write_failed:
            raise JournalError("journal_integrity_error", "escritor fechado ou persistência indeterminada")
        if not self._owns_lock or not self._has_lock_token():
            raise JournalError("run_lock_lost", "escritor não possui o token de run.lock")

    def _check_event(self, event: Any) -> None:
        issues = validate_instance(event, "journal_event")
        if issues:
            issue = issues[0]
            raise JournalError("journal_integrity_error", f"{issue.code} {issue.path}: {issue.message}")
        aggregate = (event["aggregate_type"], event["aggregate_id"])
        if (
            event["run_id"] != self.run_id
            or (event["aggregate_type"] == "run" and event["aggregate_id"] != self.run_id)
            or event["global_sequence"] != len(self._events) + 1
            or event["event_id"] != _event_id(event["global_sequence"])
            or event["aggregate_sequence"] != self._aggregates.get(aggregate, 0) + 1
            or event["previous_event_hash"] != self.final_digest
            or event["event_id"] in self._event_ids
            or (event["causation_event_id"] is not None and event["causation_event_id"] not in self._event_ids)
        ):
            raise JournalError("journal_integrity_error", "run, sequência, hash anterior, ID ou causa divergente")
        if event["event_hash"] != canonical_digest({k: v for k, v in event.items() if k != "event_hash"}):
            raise JournalError("journal_integrity_error", "event_hash divergente")
        key = event["idempotency_key"]
        if key is not None and key in self._idempotency:
            raise JournalError("journal_integrity_error", "idempotency_key duplicada no disco")

    def _remember(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        self._aggregates[(event["aggregate_type"], event["aggregate_id"])] = event["aggregate_sequence"]
        self._event_ids.add(event["event_id"])
        if event["idempotency_key"] is not None:
            self._idempotency[event["idempotency_key"]] = (canonical_digest(event["payload"]), event)

    def append(
        self, *, event_type: str, aggregate_type: str, aggregate_id: str,
        expected_aggregate_sequence: int, payload: dict[str, Any],
        actor: dict[str, str] | None = None, correlation_id: str | None = None,
        causation_event_id: str | None = None, idempotency_key: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        self._require_writer()
        payload = copy.deepcopy(payload)
        try:
            payload_digest = canonical_digest(payload)
            if idempotency_key is not None and idempotency_key in self._idempotency:
                previous_digest, previous = self._idempotency[idempotency_key]
                if previous_digest != payload_digest or (
                    previous["event_type"], previous["aggregate_type"], previous["aggregate_id"]
                ) != (event_type, aggregate_type, aggregate_id):
                    raise JournalError("idempotency_conflict", "chave já usada com outro payload, evento ou agregado")
                return copy.deepcopy(previous)
            sequence = len(self._events) + 1
            event = {
                "schema_version": "0.1", "event_id": self._event_id_factory(sequence),
                "run_id": self.run_id, "global_sequence": sequence,
                "aggregate_type": aggregate_type, "aggregate_id": aggregate_id,
                "aggregate_sequence": expected_aggregate_sequence, "event_type": event_type,
                "recorded_at": self._clock(), "occurred_at": occurred_at,
                "actor": copy.deepcopy(actor) if actor is not None else {"kind": "orchestrator", "ref": "orq-runner"},
                "correlation_id": correlation_id if correlation_id is not None else aggregate_id,
                "causation_event_id": causation_event_id, "idempotency_key": idempotency_key,
                "payload": payload, "previous_event_hash": self.final_digest,
            }
            event["event_hash"] = canonical_digest(event)
            line = canonicalize(event) + b"\n"
            if len(line) > MAX_JOURNAL_LINE_BYTES:
                raise JournalError("journal_line_too_large", "linha excede 1 MiB incluindo LF")
            self._check_event(event)
        except (CanonicalizationError, RecursionError) as error:
            raise JournalError("journal_integrity_error", "evento fora do perfil JCS") from error
        try:
            # FileIO sem buffering: uma única escrita, com O_APPEND.
            if self._file.write(line) != len(line):
                raise OSError("escrita parcial do journal")
            self._file.flush()
            os.fsync(self._file.fileno())
        except BaseException:
            self._write_failed = True
            raise
        self._remember(event)
        return copy.deepcopy(event)

    def _verify(self) -> tuple[int, bytes] | None:
        offset = 0
        with self.path.open("rb") as handle:
            line = handle.readline(MAX_JOURNAL_LINE_BYTES + 1)
            while line:
                following = handle.readline(MAX_JOURNAL_LINE_BYTES + 1)
                if len(line) > MAX_JOURNAL_LINE_BYTES:
                    raise JournalError("journal_integrity_error", "linha no disco excede 1 MiB")
                if not line.endswith(b"\n") and not following:
                    if not self._events:
                        raise JournalError("journal_integrity_error", "cauda sem evento anterior; RunIntent não confirmado")
                    return offset, line
                try:
                    event = json.loads(line[:-1].decode("utf-8"), parse_constant=_invalid_json_constant)
                except ValueError as error:
                    raise JournalError("journal_integrity_error", "JSON inválido em linha com LF") from error
                except RecursionError as error:
                    raise JournalError("journal_integrity_error", "JSON excede profundidade suportada") from error
                try:
                    if canonicalize(event) + b"\n" != line:
                        raise JournalError("journal_integrity_error", "bytes fora da forma JCS")
                    self._check_event(event)
                except (CanonicalizationError, RecursionError) as error:
                    raise JournalError("journal_integrity_error", "linha fora do perfil JCS") from error
                self._remember(event)
                offset += len(line)
                line = following
        return None

    def _recover(self, tail: tuple[int, bytes] | None) -> None:
        discarded = _confined_path(self.directory, "discarded")
        reject_pending_files(discarded, error_type=JournalError)
        if not self._events and any(discarded.glob("*.tail")):
            raise JournalError("journal_integrity_error", "cauda preservada sem evento anterior; RunIntent não confirmado")
        try:
            if tail is not None:
                offset, content = tail
                ensure_directory(discarded)
                name = f"{offset:020d}-{_digest_bytes(content)['value']}.tail"
                path = _confined_path(self.directory, "discarded", name)
                publish_bytes(path, content)
                if path.read_bytes() != content:
                    raise JournalError("journal_integrity_error", "arquivo descartado divergente")
                with self.path.open("r+b") as handle:
                    handle.truncate(offset)
                    handle.flush()
                    os.fsync(handle.fileno())
            # Arquivos sem evento indicam crash entre preservar, truncar e auditar.
            if discarded.exists():
                for path in sorted(discarded.glob("*.tail")):
                    match = re.fullmatch(r"[0-9]{20}-([a-f0-9]{64})\.tail", path.name)
                    path = _confined_path(self.directory, "discarded", path.name)
                    sync_file(path)
                    content = path.read_bytes()
                    if not match or not content or _digest_bytes(content)["value"] != match[1]:
                        raise JournalError("journal_integrity_error", "cauda preservada corrompida")
                    key = "orq/journal-tail/" + path.name
                    payload = {"discarded_bytes": len(content), "discarded_digest": _digest_bytes(content)}
                    self.append(
                        event_type="journal_tail_discarded", aggregate_type="run", aggregate_id=self.run_id,
                        expected_aggregate_sequence=self._aggregates.get(("run", self.run_id), 0) + 1,
                        payload=payload, idempotency_key=key,
                    )
        except BaseException:
            # Recuperação interrompida também exige inspeção/remoção manual do lock.
            self._write_failed = True
            raise
