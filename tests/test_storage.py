from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from orq_contracts import _storage, journal as journal_module
from orq_contracts.journal import RunJournal


def test_ensure_directory_syncs_each_new_parent(tmp_path, monkeypatch) -> None:
    calls = []
    original = _storage.fsync_directory
    def sync(path):
        calls.append(path)
        original(path)
    with monkeypatch.context() as patch:
        patch.setattr(_storage, "fsync_directory", sync)
        _storage.ensure_directory(tmp_path / "first" / "second")
    assert calls == [tmp_path, tmp_path / "first"]


def test_ensure_directory_propagates_sync_failure(tmp_path, monkeypatch) -> None:
    def fail(path):
        raise OSError("directory fsync falhou")
    monkeypatch.setattr(_storage, "fsync_directory", fail)
    with pytest.raises(OSError, match="directory fsync falhou"):
        _storage.ensure_directory(tmp_path / "new")


def test_journal_lock_creation_open_and_close_sync_directories(tmp_path, monkeypatch) -> None:
    calls = []
    original = journal_module.fsync_directory
    directory = tmp_path / "runs" / "run-test"
    def sync(path):
        calls.append((path, (directory / "run.lock").exists(), (directory / "journal.jsonl").exists()))
        original(path)
    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "fsync_directory", sync)
        with RunJournal.create(tmp_path, "run-test"):
            assert calls == [
                (directory.parent, False, False),
                (directory, True, False),  # publicação do lock
                (directory, True, True),   # criação do journal
            ]
        assert calls[-1] == (directory, False, True)
        calls.clear()
        with RunJournal.open(tmp_path, "run-test"):
            assert calls[0] == (directory, True, True)
        assert calls[-1] == (directory, False, True)


def test_close_propagates_directory_sync_failure(tmp_path, monkeypatch) -> None:
    journal = RunJournal.create(tmp_path, "run-test")
    def fail(path):
        raise OSError("close directory fsync falhou")
    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "fsync_directory", fail)
        with pytest.raises(OSError, match="close directory fsync falhou"):
            journal.close()
    assert not (journal.directory / "run.lock").exists()
    journal.close()


def test_open_syncs_journal_file_then_its_directory(tmp_path, monkeypatch) -> None:
    with RunJournal.create(tmp_path, "run-test") as journal:
        path = journal.path
    calls = []
    original_fsync = _storage.os.fsync
    original_sync_directory = _storage.fsync_directory
    original_sync_file = journal_module.sync_file
    def fsync(descriptor):
        calls.append("file")
        original_fsync(descriptor)
    def directory(parent):
        assert parent == path.parent
        calls.append("directory")
        original_sync_directory(parent)
    def observed_sync_file(target):
        assert target == path
        with monkeypatch.context() as patch:
            patch.setattr(_storage.os, "fsync", fsync)
            patch.setattr(_storage, "fsync_directory", directory)
            original_sync_file(target)
        # POSIX fsync_directory também chama os.fsync no descriptor do diretório.
        assert calls == (["file", "directory"] if os.name == "nt" else ["file", "directory", "file"])
    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "sync_file", observed_sync_file)
        with RunJournal.open(tmp_path, "run-test"):
            pass


def test_open_propagates_verified_journal_directory_sync_failure(tmp_path, monkeypatch) -> None:
    with RunJournal.create(tmp_path, "run-test") as journal:
        path = journal.path
    def fail(_):
        raise OSError("verified journal directory fsync falhou")
    with monkeypatch.context() as patch:
        patch.setattr(_storage, "fsync_directory", fail)
        with pytest.raises(OSError, match="verified journal directory fsync falhou"):
            RunJournal.open(tmp_path, "run-test")
    assert not (path.parent / "run.lock").exists()


@pytest.mark.parametrize("race", [False, True])
def test_existing_publication_is_synced_before_consumer_reads(tmp_path, monkeypatch, race) -> None:
    path = tmp_path / "target"
    if not race:
        path.write_bytes(b"existing")
    calls = []
    original_fsync = _storage.os.fsync
    original_sync_directory = _storage.fsync_directory
    original_rename = type(path).rename
    original_link = _storage.os.link
    def fsync(descriptor):
        calls.append(("file", os.fstat(descriptor).st_size))
        original_fsync(descriptor)
    def sync_directory(parent):
        calls.append(("directory", parent))
        original_sync_directory(parent)
    def raced_rename(source, target):
        target.write_bytes(b"existing")
        return original_rename(source, target)
    def raced_link(source, target):
        target.write_bytes(b"existing")
        return original_link(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(_storage.os, "fsync", fsync)
        patch.setattr(_storage, "fsync_directory", sync_directory)
        if race:
            patch.setattr(type(path), "rename", raced_rename)
            patch.setattr(_storage.os, "link", raced_link)
        assert _storage.publish_bytes(path, b"new") is False
    assert path.read_bytes() == b"existing"
    assert ("file", len(b"existing")) in calls
    assert calls.index(("file", len(b"existing"))) < calls.index(("directory", tmp_path))
    assert not list(tmp_path.glob(".pending-*"))


@pytest.mark.parametrize("failure", ["file", "directory"])
def test_existing_publication_propagates_sync_failure(tmp_path, monkeypatch, failure) -> None:
    path = tmp_path / "target"
    path.write_bytes(b"existing")
    def fail(_):
        raise OSError("existing fsync falhou")
    if failure == "file":
        monkeypatch.setattr(_storage.os, "fsync", fail)
    else:
        monkeypatch.setattr(_storage, "fsync_directory", fail)
    with pytest.raises(OSError, match="existing fsync falhou"):
        _storage.publish_bytes(path, b"new")
    assert path.read_bytes() == b"existing"


def test_posix_link_never_replaces_racing_destination(tmp_path, monkeypatch) -> None:
    """Exercita o caminho POSIX também no Windows, com hard link real."""
    path = tmp_path / "target"
    calls = []
    def link(source, target):
        assert source.parent == target.parent
        target.write_bytes(b"winner")
        calls.append("link")
        os.link(source, target)  # EEXIST atômico; preserva winner
    monkeypatch.setattr(_storage, "os", SimpleNamespace(name="posix", fdopen=os.fdopen, fsync=os.fsync, link=link))
    monkeypatch.setattr(_storage, "fsync_directory", lambda parent: calls.append("directory"))
    assert _storage.publish_bytes(path, b"loser") is False
    assert path.read_bytes() == b"winner"
    assert calls == ["link", "directory"]
    assert not list(tmp_path.glob(".pending-*"))


def test_posix_link_unlinks_temporary_before_directory_sync(tmp_path, monkeypatch) -> None:
    path = tmp_path / "target"
    calls = []
    def link(source, target):
        assert source.read_bytes() == b"new"
        os.link(source, target)
        calls.append("link")
    def sync(parent):
        assert path.read_bytes() == b"new"
        assert not list(parent.glob(".pending-*"))
        calls.append("directory")
    monkeypatch.setattr(_storage, "os", SimpleNamespace(name="posix", fdopen=os.fdopen, fsync=os.fsync, link=link))
    monkeypatch.setattr(_storage, "fsync_directory", sync)
    assert _storage.publish_bytes(path, b"new") is True
    assert calls == ["link", "directory"]


def test_new_publication_propagates_directory_sync_failure(tmp_path, monkeypatch) -> None:
    path = tmp_path / "target"
    def fail(_):
        raise OSError("publish directory fsync falhou")
    with monkeypatch.context() as patch:
        patch.setattr(_storage, "fsync_directory", fail)
        with pytest.raises(OSError, match="publish directory fsync falhou"):
            _storage.publish_bytes(path, b"new")
    assert path.read_bytes() == b"new"
    assert not list(tmp_path.glob(".pending-*"))
    # Repetição recupera a durabilidade antes de permitir o uso do destino.
    assert _storage.publish_bytes(path, b"new") is False
