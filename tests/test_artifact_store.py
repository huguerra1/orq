from __future__ import annotations

import hashlib

import pytest

from orq_contracts import _storage
from orq_contracts.artifact_store import ArtifactStore, ArtifactStoreError, MAX_ARTIFACT_BYTES
from orq_contracts.journal import RunJournal


@pytest.fixture
def store(tmp_path):
    with RunJournal.create(tmp_path, "run-artifact-test") as journal:
        yield ArtifactStore(journal)


def test_artifact_duplicate_is_verified_without_replacing(store) -> None:
    content = b'{"report":"ok"}\n'
    locator = store.put(content)
    digest = hashlib.sha256(content).hexdigest()
    assert locator == "orq-store:sha256/" + digest
    path = store.directory / digest
    before = path.stat()
    assert store.put(content) == locator
    after = path.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert store.read(locator) == content
    assert list(store.directory.iterdir()) == [path]


@pytest.mark.parametrize("operation", ["read", "put"])
def test_corrupted_artifact_is_detected_and_never_overwritten(store, operation) -> None:
    content = b"original"
    locator = store.put(content)
    path = store.directory / locator.split("/")[-1]
    path.write_bytes(b"corrompido")
    with pytest.raises(ArtifactStoreError) as caught:
        store.read(locator) if operation == "read" else store.put(content)
    assert caught.value.code == "artifact_digest_mismatch"
    assert path.read_bytes() == b"corrompido"


def test_artifact_limit_and_empty_content(store) -> None:
    with pytest.raises(ArtifactStoreError) as caught:
        store.put(b"x" * (MAX_ARTIFACT_BYTES + 1))
    assert caught.value.code == "artifact_too_large"
    assert list(store.directory.iterdir()) == []
    content = b"x" * MAX_ARTIFACT_BYTES
    assert store.read(store.put(content)) == content
    assert store.read(store.put(b"")) == b""


@pytest.mark.parametrize("locator", [
    "orq-store:sha256/../secret", "file:///secret", "orq-store:sha256/" + "A" * 64,
    "orq-store:sha256/" + "0" * 63, "orq-store:sha256/" + "0" * 64 + "/child",
])
def test_locator_is_confined(store, locator) -> None:
    with pytest.raises(ArtifactStoreError, match="invalid_artifact_locator"):
        store.read(locator)


def test_artifact_is_synced_before_same_directory_publication(store, monkeypatch) -> None:
    calls = []
    original_fsync = _storage.os.fsync
    original_rename = type(store.directory).rename
    original_link = _storage.os.link
    original_sync_directory = _storage.fsync_directory
    def fsync(descriptor):
        calls.append("fsync")
        original_fsync(descriptor)
    def rename(source, target):
        assert source.parent == target.parent == store.directory
        assert calls[0] == "fsync"
        assert source.read_bytes() == b"content"
        calls.append("rename")
        return original_rename(source, target)
    def link(source, target):
        assert source.parent == target.parent == store.directory
        assert source.read_bytes() == b"content"
        assert calls == ["fsync"]
        calls.append("link")
        return original_link(source, target)
    def sync_directory(path):
        assert path == store.directory
        assert (path / hashlib.sha256(b"content").hexdigest()).read_bytes() == b"content"
        assert not list(path.glob(".pending-*"))
        calls.append("directory")
        original_sync_directory(path)
    with monkeypatch.context() as patch:
        patch.setattr(_storage.os, "fsync", fsync)
        patch.setattr(type(store.directory), "rename", rename)
        patch.setattr(_storage.os, "link", link)
        patch.setattr(_storage, "fsync_directory", sync_directory)
        store.put(b"content")
    assert calls[:3] == ["fsync", "rename" if _storage.os.name == "nt" else "link", "directory"]


def test_artifact_failed_fsync_leaves_no_final_or_temporary(store, monkeypatch) -> None:
    def fail(_):
        raise OSError("fsync falhou")
    with monkeypatch.context() as patch:
        patch.setattr(_storage.os, "fsync", fail)
        with pytest.raises(OSError, match="fsync falhou"):
            store.put(b"content")
    assert list(store.directory.iterdir()) == []


def test_artifact_requires_open_journal_and_current_lock(tmp_path) -> None:
    with pytest.raises(ArtifactStoreError, match="run_lock_required"):
        ArtifactStore(tmp_path)
    with RunJournal.create(tmp_path, "run-test") as journal:
        store = ArtifactStore(journal)
        locator = store.put(b"ok")
        lock = journal.directory / "run.lock"
        token = lock.read_bytes()
        lock.write_bytes(b"outro dono")
        for operation in (lambda: ArtifactStore(journal), lambda: store.put(b"x"), lambda: store.read(locator)):
            with pytest.raises(ArtifactStoreError, match="run_lock_required"):
                operation()
        lock.write_bytes(token)
    for operation in (lambda: ArtifactStore(journal), lambda: store.put(b"x"), lambda: store.read(locator)):
        with pytest.raises(ArtifactStoreError, match="run_lock_required"):
            operation()


def test_missing_and_oversized_artifacts_have_typed_errors(store) -> None:
    with pytest.raises(ArtifactStoreError, match="invalid_artifact_content"):
        store.put("invalid")
    with pytest.raises(ArtifactStoreError, match="artifact_not_found"):
        store.read("orq-store:sha256/" + "0" * 64)
    locator = store.put(b"small")
    path = store.directory / locator.split("/")[-1]
    path.write_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(ArtifactStoreError, match="artifact_too_large"):
        store.read(locator)


def test_artifact_pending_files_are_reported_and_preserved(store) -> None:
    pending = store.directory / ".pending-crashed"
    pending.write_bytes(b"unconfirmed")
    for operation in (lambda: ArtifactStore(store.journal), lambda: store.put(b"x")):
        with pytest.raises(ArtifactStoreError, match="storage_pending_files.*pending-crashed"):
            operation()
    assert pending.read_bytes() == b"unconfirmed"
