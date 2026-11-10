"""Artefatos imutáveis, endereçados pelo SHA-256 dos bytes reais."""

from __future__ import annotations

import hashlib
import re

from ._storage import StorageError, confined_path, ensure_directory, publish_bytes, reject_pending_files
from .journal import JournalError, RunJournal


MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
LOCATOR_PREFIX = "orq-store:sha256/"


class ArtifactStoreError(StorageError):
    """Falha tipada do armazenamento de artefatos."""


class ArtifactStore:
    """Store que exige um journal aberto e dono do lock em cada operação."""

    def __init__(self, journal: RunJournal) -> None:
        if not isinstance(journal, RunJournal):
            raise ArtifactStoreError("run_lock_required", "ArtifactStore exige RunJournal aberto")
        self.journal = journal
        self._require_writer()
        self.run_directory = journal.directory
        self.directory = confined_path(self.run_directory, "artifacts", "sha256", error_type=ArtifactStoreError)
        ensure_directory(self.directory)
        self._check_directory()

    def _require_writer(self) -> None:
        try:
            self.journal._require_writer()
        except JournalError as error:
            raise ArtifactStoreError("run_lock_required", "journal fechado, indeterminado ou sem posse do lock") from error

    def _check_directory(self) -> None:
        confined_path(self.run_directory, "artifacts", "sha256", error_type=ArtifactStoreError)
        reject_pending_files(self.directory, error_type=ArtifactStoreError)

    def put(self, content: bytes) -> str:
        """Persiste os bytes e retorna seu locator somente após sincronização."""
        self._require_writer()
        self._check_directory()
        if not isinstance(content, bytes):
            raise ArtifactStoreError("invalid_artifact_content", "artefato deve ser bytes")
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ArtifactStoreError("artifact_too_large", "artefato excede 8 MiB")
        digest = hashlib.sha256(content).hexdigest()
        locator = LOCATOR_PREFIX + digest
        path = confined_path(self.run_directory, "artifacts", "sha256", digest, error_type=ArtifactStoreError)
        publish_bytes(path, content)
        # Também confere o destino que já existia; nunca o substitui.
        self.read(locator)
        return locator

    def read(self, locator: str) -> bytes:
        self._require_writer()
        self._check_directory()
        if not isinstance(locator, str) or not re.fullmatch(
            r"orq-store:sha256/[a-f0-9]{64}", locator
        ):
            raise ArtifactStoreError("invalid_artifact_locator", "locator de artefato inválido")
        digest = locator[len(LOCATOR_PREFIX):]
        path = confined_path(self.run_directory, "artifacts", "sha256", digest, error_type=ArtifactStoreError)
        try:
            with path.open("rb") as handle:
                content = handle.read(MAX_ARTIFACT_BYTES + 1)
        except FileNotFoundError as error:
            raise ArtifactStoreError("artifact_not_found", "artefato ausente") from error
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ArtifactStoreError("artifact_too_large", "artefato no disco excede 8 MiB")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ArtifactStoreError("artifact_digest_mismatch", "bytes do artefato divergem do locator")
        return content
