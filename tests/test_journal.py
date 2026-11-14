from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
from uuid import UUID

import pytest

from conftest import load_fixture
from orq_contracts import journal as journal_module
from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.journal import JournalError, MAX_JOURNAL_LINE_BYTES, RunJournal
from orq_contracts.validator import validate_instance


FIRST = load_fixture("valid/journal-event-first.json")
DISPATCH = load_fixture("valid/journal-event.json")
RUN_ID = FIRST["run_id"]
NOW = FIRST["recorded_at"]


def create(root: Path, **kwargs) -> RunJournal:
    return RunJournal.create(root, RUN_ID, clock=lambda: NOW, **kwargs)


def reopen(root: Path, **kwargs) -> RunJournal:
    return RunJournal.open(root, RUN_ID, clock=lambda: NOW, **kwargs)


def append_fixture(journal: RunJournal, fixture: dict, **changes) -> dict:
    event = copy.deepcopy(fixture)
    event.update(changes)
    return journal.append(**{
        **{name: event[name] for name in (
            "event_type", "aggregate_type", "aggregate_id", "payload", "actor",
            "correlation_id", "causation_event_id", "idempotency_key", "occurred_at",
        )},
        "expected_aggregate_sequence": event["aggregate_sequence"],
    })


def append_cancel(journal: RunJournal, sequence: int, **changes) -> dict:
    return append_fixture(journal, FIRST, event_type="cancel_requested", payload={"reason": "teste"},
                          aggregate_sequence=sequence, idempotency_key=None, **changes)


def rehash(event: dict) -> None:
    event["event_hash"] = canonical_digest({k: v for k, v in event.items() if k != "event_hash"})


def journal_path(root: Path) -> Path:
    return root / "runs" / RUN_ID / "journal.jsonl"


def test_chain_sequences_and_6a_fixture_match(tmp_path) -> None:
    with create(tmp_path) as journal:
        assert journal.final_digest is None
        first = append_fixture(journal, FIRST)
        assert first == FIRST
        second = append_fixture(journal, DISPATCH, causation_event_id=first["event_id"])
        third = append_fixture(journal, DISPATCH, event_type="execution_started",
                               aggregate_sequence=2, payload={"observed_at": NOW},
                               causation_event_id=second["event_id"], idempotency_key="started")
        fourth = append_cancel(journal, 2)
        events = (first, second, third, fourth)
        assert [e["global_sequence"] for e in events] == [1, 2, 3, 4]
        assert [e["aggregate_sequence"] for e in events] == [1, 1, 2, 2]
        assert journal.final_digest == fourth["event_hash"]
        assert second["payload"] == DISPATCH["payload"]  # hashes embutidos preservados
        for index, event in enumerate(events):
            assert not validate_instance(event, "journal_event")
            assert event["event_hash"] == canonical_digest({k: v for k, v in event.items() if k != "event_hash"})
            assert event["previous_event_hash"] == (events[index - 1]["event_hash"] if index else None)
    assert journal_path(tmp_path).read_bytes() == b"".join(canonicalize(e) + b"\n" for e in events)
    with reopen(tmp_path) as journal:
        assert journal.events == events
        assert journal.final_digest == fourth["event_hash"]
        assert append_cancel(journal, 3)["global_sequence"] == 5


def test_aggregates_have_independent_sequences_even_with_same_id(tmp_path) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
        for aggregate_type in ("run", "attempt", "run", "attempt"):
            expected = 1 + sum(e["aggregate_type"] == aggregate_type for e in journal.events)
            event = append_cancel(journal, expected, aggregate_type=aggregate_type)
            assert event["aggregate_sequence"] == expected
        assert [e["aggregate_sequence"] for e in journal.events] == [1, 2, 1, 3, 2]


@pytest.mark.parametrize("cause", ["event-2", "event-99"])
def test_append_rejects_causation_that_is_not_previous(tmp_path, cause) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
        before = journal.path.read_bytes()
        with pytest.raises(JournalError) as caught:
            append_cancel(journal, 2, causation_event_id=cause)
        assert caught.value.code == "journal_integrity_error"
        assert journal.path.read_bytes() == before


def test_append_rejects_wrong_expected_sequence_without_writing(tmp_path) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
        with pytest.raises(JournalError, match="journal_integrity_error"):
            append_cancel(journal, 3)
        assert journal.events == (FIRST,)
        assert append_cancel(journal, 2)["global_sequence"] == 2


def test_idempotency_repetition_conflict_reopen_and_defensive_copies(tmp_path) -> None:
    with create(tmp_path) as journal:
        original = append_fixture(journal, FIRST)
        before = journal.path.read_bytes()
        reversed_payload = dict(reversed(list(FIRST["payload"].items())))
        assert append_fixture(journal, FIRST, payload=reversed_payload) == original
        # Repetição ignora a sequência antiga e não usa relógio/gerador novamente.
        journal._clock = lambda: pytest.fail("relógio usado na repetição")
        journal._event_id_factory = lambda _: pytest.fail("ID usado na repetição")
        assert append_fixture(journal, FIRST) == original
        changed = copy.deepcopy(FIRST["payload"])
        changed["objective"] = "outro"
        with pytest.raises(JournalError) as caught:
            append_fixture(journal, FIRST, payload=changed)
        assert caught.value.code == "idempotency_conflict"
        assert journal.path.read_bytes() == before
        original["payload"]["objective"] = "mutação externa"
        snapshot = journal.events
        snapshot[0]["event_hash"]["value"] = "0" * 64
        assert journal.events == (FIRST,)
    with reopen(tmp_path) as journal:
        assert append_fixture(journal, FIRST) == FIRST
        with pytest.raises(JournalError, match="idempotency_conflict"):
            append_fixture(journal, FIRST, payload=changed)
        assert journal.path.read_bytes() == before


@pytest.mark.parametrize("tail", [b'{"event_id":', b'{invalid}', b'\xff', b'{"bad":NaN}', canonicalize(FIRST)])
def test_tail_without_lf_is_preserved_and_audited(tmp_path, tail) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
    path = journal_path(tmp_path)
    confirmed = path.read_bytes()
    with path.open("ab") as handle:
        handle.write(tail)
    with reopen(tmp_path) as journal:
        event = journal.events[1]
        assert event["event_type"] == "journal_tail_discarded"
        assert event["global_sequence"] == event["aggregate_sequence"] == 2
        assert event["previous_event_hash"] == FIRST["event_hash"]
        assert event["payload"] == {
            "discarded_bytes": len(tail),
            "discarded_digest": {"algorithm": "sha256", "value": hashlib.sha256(tail).hexdigest()},
        }
        assert list((journal.directory / "discarded").glob("*.tail"))[0].read_bytes() == tail
        assert journal.path.read_bytes().startswith(confirmed)
        assert append_cancel(journal, 3)["global_sequence"] == 3
    final = path.read_bytes()
    with reopen(tmp_path) as journal:
        assert len(journal.events) == 3
        assert journal.path.read_bytes() == final  # recuperação não duplica auditoria


def test_tail_cannot_be_the_only_line(tmp_path) -> None:
    with create(tmp_path):
        pass
    journal_path(tmp_path).write_bytes(b'{"partial"')
    with pytest.raises(JournalError, match="journal_integrity_error.*RunIntent"):
        reopen(tmp_path)
    assert journal_path(tmp_path).read_bytes() == b'{"partial"'
    assert not (journal_path(tmp_path).parent / "discarded").exists()
    assert not (journal_path(tmp_path).parent / "run.lock").exists()


def test_interrupted_tail_recovery_records_pending_discard_first(tmp_path, monkeypatch) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
    path = journal_path(tmp_path)
    with path.open("ab") as handle:
        handle.write(b'{"partial"')
    original_append = RunJournal.append
    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):
            raise OSError("crash antes da auditoria")
        patch.setattr(RunJournal, "append", interrupted)
        with pytest.raises(OSError, match="crash"):
            reopen(tmp_path)
    lock = path.parent / "run.lock"
    assert lock.exists()
    with pytest.raises(JournalError, match="run_locked"):
        reopen(tmp_path)
    lock.unlink()  # simula apenas o procedimento manual documentado
    assert RunJournal.append is original_append
    with reopen(tmp_path) as journal:
        assert [e["event_type"] for e in journal.events] == ["run_intent_recorded", "journal_tail_discarded"]


@pytest.mark.parametrize("corruption", [
    "payload", "hash", "previous_hash", "global_sequence", "aggregate_sequence",
    "non_jcs", "schema", "invalid_json", "invalid_utf8", "nan", "duplicate_json_key",
    "array", "number", "self_cause", "future_cause", "duplicate_id", "event_id", "run_id", "run_aggregate_id",
])
@pytest.mark.parametrize("last_line", [False, True])
def test_corruption_is_not_repaired(tmp_path, corruption, last_line) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
        append_cancel(journal, 2)
        append_cancel(journal, 3)
        events = list(journal.events)
    index = 2 if last_line else 1
    event = events[index]
    if corruption == "payload":
        event["payload"]["reason"] = "alterada"
    elif corruption == "hash":
        event["event_hash"]["value"] = "0" * 64
    elif corruption == "previous_hash":
        event["previous_event_hash"]["value"] = "0" * 64
    elif corruption in {"global_sequence", "aggregate_sequence"}:
        event[corruption] += 1
    elif corruption == "schema":
        event["payload"] = {"campo_desconhecido": True}
    elif corruption == "self_cause":
        event["causation_event_id"] = event["event_id"]
    elif corruption == "future_cause":
        event["causation_event_id"] = "event-99"
    elif corruption == "duplicate_id":
        event["event_id"] = "event-1"
    elif corruption == "event_id":
        event["event_id"] = "custom-3" if last_line else "custom-2"
    elif corruption == "run_id":
        event["run_id"] = "another-run"
    elif corruption == "run_aggregate_id":
        event["aggregate_id"] = "another-run"
    if corruption not in {"payload", "hash"}:
        rehash(event)
    lines = [canonicalize(e) + b"\n" for e in events]
    if corruption == "non_jcs":
        lines[index] = json.dumps(event).encode() + b"\n"
    elif corruption == "invalid_json":
        lines[index] = b"{invalid}\n"
    elif corruption == "invalid_utf8":
        lines[index] = b"\xff\n"
    elif corruption == "nan":
        lines[index] = b'{"bad":NaN}\n'
    elif corruption == "duplicate_json_key":
        lines[index] = b'{"event_id":"duplicate",' + lines[index][1:]
    elif corruption == "array":
        lines[index] = b"[]\n"
    elif corruption == "number":
        lines[index] = b"1\n"
    path = journal_path(tmp_path)
    path.write_bytes(b"".join(lines))
    before = path.read_bytes()
    with pytest.raises(JournalError) as caught:
        reopen(tmp_path)
    assert caught.value.code == "journal_integrity_error"
    assert path.read_bytes() == before
    assert not (path.parent / "discarded").exists()
    assert not (path.parent / "run.lock").exists()


def first_with_line_size(size: int) -> dict:
    event = copy.deepcopy(FIRST)
    tags = event["payload"]["experiment_metadata"]["tags"] = []
    remaining = size - len(canonicalize(event) + b"\n") + 1
    index = 0
    while remaining > 4099:
        tags.append("x" * 4092 + f"{index:04d}")
        remaining -= 4099
        index += 1
    assert remaining >= 3
    tags.append("y" * (remaining - 3))
    event["payload"]["record_hash"] = canonical_digest({k: v for k, v in event["payload"].items() if k != "record_hash"})
    rehash(event)
    assert len(canonicalize(event) + b"\n") == size
    return event


def test_line_limit_includes_lf_and_rejects_before_write(tmp_path) -> None:
    with create(tmp_path) as journal:
        oversized = first_with_line_size(MAX_JOURNAL_LINE_BYTES + 1)
        with pytest.raises(JournalError) as caught:
            append_fixture(journal, oversized)
        assert caught.value.code == "journal_line_too_large"
        assert journal.path.read_bytes() == b""
        assert not journal.events
        exact = first_with_line_size(MAX_JOURNAL_LINE_BYTES)
        assert not validate_instance(exact, "journal_event")
        append_fixture(journal, exact)
        assert journal.path.stat().st_size == MAX_JOURNAL_LINE_BYTES
    with reopen(tmp_path) as journal:
        assert journal.events == (exact,)


def test_run_exists_and_existing_lock_is_never_removed(tmp_path) -> None:
    with create(tmp_path) as journal:
        lock = journal.directory / "run.lock"
        token = lock.read_bytes()
        metadata = json.loads(token)
        assert metadata["pid"] == os.getpid()
        assert metadata["host"] == socket.gethostname()
        assert metadata["created_at"].endswith("Z")
        assert UUID(metadata["uuid"])
        for operation, code in ((create, "run_exists"), (reopen, "run_locked")):
            with pytest.raises(JournalError) as caught:
                operation(tmp_path)
            assert caught.value.code == code
            assert lock.read_bytes() == token
    assert not lock.exists()
    with pytest.raises(JournalError, match="run_exists"):
        create(tmp_path)
    lock.write_bytes(b"orfao")
    with pytest.raises(JournalError, match="run_locked"):
        reopen(tmp_path)
    assert lock.read_bytes() == b"orfao"


def test_same_inputs_produce_identical_bytes_and_final_digest(tmp_path) -> None:
    results = []
    for name in ("one", "two"):
        with create(tmp_path / name) as journal:
            append_fixture(journal, FIRST)
            append_fixture(journal, DISPATCH, causation_event_id="event-1")
            append_cancel(journal, 2)
            results.append((journal.path.read_bytes(), journal.final_digest))
    assert results[0] == results[1]


def test_injected_ids_must_follow_contract_and_clock_is_used(tmp_path) -> None:
    with create(tmp_path, event_id_factory=lambda n: f"custom-{n}") as journal:
        with pytest.raises(JournalError, match="journal_integrity_error"):
            append_fixture(journal, FIRST)
        assert journal.path.read_bytes() == b""
        journal._event_id_factory = lambda n: f"event-{n}"
        event = append_fixture(journal, FIRST)
        assert event["event_id"] == "event-1"
        assert event["recorded_at"] == NOW
    with reopen(tmp_path) as journal:
        assert append_cancel(journal, 2, causation_event_id="event-1")["event_id"] == "event-2"


def test_task_and_evaluation_sequences_are_independent(tmp_path) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
        for task in ("T1", "T2", "T1"):
            sequence = 2 if task == "T1" and len(journal.events) == 3 else 1
            append_fixture(journal, FIRST, aggregate_type="task", aggregate_id=task,
                           aggregate_sequence=sequence, event_type="state_transition",
                           idempotency_key=None, payload={
                               "from": "pending", "to": "ready", "reason": "fixture",
                               "evidence_event_ids": [],
                           })
        report = load_fixture("valid/evaluation-report-workflow.json")
        append_fixture(journal, FIRST, aggregate_type="evaluation", aggregate_id=report["evaluation_id"],
                       event_type="evaluation_report_recorded", payload=report, idempotency_key=None)
    with reopen(tmp_path) as journal:
        assert [e["aggregate_sequence"] for e in journal.events] == [1, 1, 1, 2, 1]


def test_duplicate_idempotency_key_in_file_is_corruption(tmp_path) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
        second = append_fixture(journal, FIRST, idempotency_key=None, aggregate_sequence=2)
    second["idempotency_key"] = FIRST["idempotency_key"]
    rehash(second)
    path = journal_path(tmp_path)
    path.write_bytes(canonicalize(FIRST) + b"\n" + canonicalize(second) + b"\n")
    before = path.read_bytes()
    with pytest.raises(JournalError, match="journal_integrity_error"):
        reopen(tmp_path)
    assert path.read_bytes() == before


def test_partial_write_retains_lock_and_blocks_further_appends(tmp_path) -> None:
    journal = create(tmp_path)
    append_fixture(journal, FIRST)
    actual = journal._file
    class PartialFile:
        def write(self, line):
            return actual.write(line[:20])
        def close(self):
            actual.close()
    journal._file = PartialFile()
    with pytest.raises(OSError, match="parcial"):
        append_cancel(journal, 2)
    assert journal.events == (FIRST,)
    with pytest.raises(JournalError, match="journal_integrity_error"):
        append_cancel(journal, 2)
    journal.close()
    assert (journal.directory / "run.lock").exists()
    partial = journal.path.read_bytes().splitlines(keepends=True)[-1]
    assert len(partial) == 20
    (journal.directory / "run.lock").unlink()  # procedimento manual após inspeção
    with reopen(tmp_path) as recovered:
        assert recovered.events[0] == FIRST
        assert recovered.events[1]["event_type"] == "journal_tail_discarded"
        assert recovered.events[1]["payload"]["discarded_bytes"] == 20
        assert next((recovered.directory / "discarded").glob("*.tail")).read_bytes() == partial


@pytest.mark.parametrize("defect", ["payload", "envelope", "semantic_outputs"])
def test_append_uses_6a_envelope_payload_and_semantic_validation(tmp_path, defect) -> None:
    event = copy.deepcopy(FIRST)
    if defect == "payload":
        event["payload"] = {"invalid": True}
    elif defect == "envelope":
        event["actor"]["unknown"] = "invalid"
    else:
        report = load_fixture("valid/evaluation-report-workflow.json")
        report["criteria_snapshot"]["criteria"][0]["pass_rule"]["output_ids"] = ["different"]
        event.update(event_type="evaluation_report_recorded", aggregate_type="evaluation",
                     aggregate_id=report["evaluation_id"], payload=report)
    with create(tmp_path) as journal:
        with pytest.raises(JournalError, match="journal_integrity_error"):
            append_fixture(journal, event)
        assert journal.path.read_bytes() == b""


def test_failed_fsync_does_not_confirm_event_or_allow_reuse(tmp_path, monkeypatch) -> None:
    journal = create(tmp_path)
    append_fixture(journal, FIRST)
    def fail(_):
        raise OSError("fsync falhou")
    with monkeypatch.context() as patch:
        patch.setattr(journal_module.os, "fsync", fail)
        with pytest.raises(OSError, match="fsync falhou"):
            append_fixture(journal, DISPATCH, causation_event_id=FIRST["event_id"])
    assert journal.events == (FIRST,)
    assert journal.final_digest == FIRST["event_hash"]
    with pytest.raises(JournalError, match="journal_integrity_error"):
        append_fixture(journal, DISPATCH, causation_event_id=FIRST["event_id"])
    journal.close()
    assert (journal.directory / "run.lock").exists()
    (journal.directory / "run.lock").unlink()  # procedimento manual após inspeção
    with reopen(tmp_path) as recovered:
        # A linha completa pode reaparecer; o 6C deve reconciliar sua intenção.
        assert recovered.events[0] == FIRST
        assert recovered.events[1]["event_type"] == "dispatch_requested"
        assert len(recovered.events) == 2
        assert append_fixture(recovered, DISPATCH, causation_event_id=FIRST["event_id"]) == recovered.events[1]
        assert len(recovered.events) == 2


def test_append_is_one_write_then_flush_then_fsync(tmp_path, monkeypatch) -> None:
    with create(tmp_path) as journal:
        actual = journal._file
        calls = []
        class ObservedFile:
            def write(self, line):
                calls.append(("write", line))
                return actual.write(line)
            def flush(self):
                calls.append(("flush", None))
                return actual.flush()
            def fileno(self):
                return actual.fileno()
            def close(self):
                actual.close()
        journal._file = ObservedFile()
        actual_fsync = journal_module.os.fsync
        def synced(descriptor):
            assert journal.events == ()
            calls.append(("fsync", None))
            actual_fsync(descriptor)
        with monkeypatch.context() as patch:
            patch.setattr(journal_module.os, "fsync", synced)
            event = append_fixture(journal, FIRST)
        assert [c[0] for c in calls] == ["write", "flush", "fsync"]
        assert calls[0][1] == canonicalize(event) + b"\n"


@pytest.mark.parametrize("run_id", ["../escape", "/absolute", "C:ads", "CON", "run.", "run/child"])
def test_run_path_is_confined(tmp_path, run_id) -> None:
    with pytest.raises(JournalError, match="invalid_run_id"):
        RunJournal.create(tmp_path, run_id)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", [
    {"event_type": "cancel_requested"}, {"aggregate_type": "attempt"}, {"aggregate_id": "another-run"},
])
def test_idempotency_checks_operation_identity_before_and_after_reopen(tmp_path, change) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
        before = journal.path.read_bytes()
        with pytest.raises(JournalError, match="idempotency_conflict"):
            append_fixture(journal, FIRST, **change)
        assert journal.path.read_bytes() == before
    with reopen(tmp_path) as journal:
        with pytest.raises(JournalError, match="idempotency_conflict"):
            append_fixture(journal, FIRST, **change)
        assert journal.path.read_bytes() == before


def test_run_aggregate_id_must_match_run_id(tmp_path) -> None:
    with create(tmp_path) as journal:
        with pytest.raises(JournalError, match="journal_integrity_error"):
            append_fixture(journal, FIRST, aggregate_id="another-run")
        assert journal.path.read_bytes() == b""


def test_previous_writer_cannot_remove_replacement_lock(tmp_path) -> None:
    previous = create(tmp_path)
    lock = previous.directory / "run.lock"
    old_token = lock.read_bytes()
    lock.unlink()  # simula remoção manual indevida enquanto o dono ainda existe
    with reopen(tmp_path) as current:
        new_token = lock.read_bytes()
        assert new_token != old_token
        with pytest.raises(JournalError, match="run_lock_lost"):
            append_fixture(previous, FIRST)
        previous.close()
        assert lock.read_bytes() == new_token
        with pytest.raises(JournalError, match="run_locked"):
            reopen(tmp_path)
        assert append_fixture(current, FIRST) == FIRST
    assert not lock.exists()


@pytest.mark.parametrize("operation", [create, reopen])
def test_case_colliding_run_id_is_rejected_explicitly(tmp_path, operation) -> None:
    with create(tmp_path):
        pass
    with pytest.raises(JournalError, match="run_id_case_collision"):
        RunJournal.create(tmp_path, RUN_ID.upper()) if operation is create else RunJournal.open(tmp_path, RUN_ID.upper())
    assert not (journal_path(tmp_path).parent / "run.lock").exists()


def test_missing_run_and_incomplete_creation_have_typed_errors(tmp_path) -> None:
    with pytest.raises(JournalError, match="run_not_found"):
        reopen(tmp_path)
    directory = journal_path(tmp_path).parent
    directory.mkdir(parents=True)
    with pytest.raises(JournalError, match="run_incomplete"):
        reopen(tmp_path)
    assert not (directory / "run.lock").exists()


@pytest.mark.parametrize("operation", [create, reopen])
@pytest.mark.parametrize("failure", ["file", "directory"])
def test_failed_lock_acquisition_leaves_no_orphan(tmp_path, monkeypatch, operation, failure) -> None:
    if operation is reopen:
        with create(tmp_path):
            pass
    original = journal_module.fsync_directory
    def fail_directory(path):
        if path == journal_path(tmp_path).parent:
            raise OSError("lock directory fsync falhou")
        original(path)
    with monkeypatch.context() as patch:
        if failure == "file":
            patch.setattr(journal_module.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("lock fsync falhou")))
        else:
            patch.setattr(journal_module, "fsync_directory", fail_directory)
        with pytest.raises(OSError, match="fsync falhou"):
            operation(tmp_path)
    assert not (journal_path(tmp_path).parent / "run.lock").exists()
    with operation(tmp_path):
        pass


def test_failed_journal_creation_can_be_retried(tmp_path, monkeypatch) -> None:
    original = journal_module.os.fsync
    count = 0
    def fail_journal(descriptor):
        nonlocal count
        # Identifica pelo tamanho: o lock contém token; o journal ainda está vazio.
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode) and os.fstat(descriptor).st_size == 0:
            count += 1
            raise OSError("journal fsync falhou")
        original(descriptor)
    with monkeypatch.context() as patch:
        patch.setattr(journal_module.os, "fsync", fail_journal)
        with pytest.raises(OSError, match="journal fsync falhou"):
            create(tmp_path)
    assert count == 1
    assert not journal_path(tmp_path).parent.exists()
    with create(tmp_path):
        pass


def test_failed_journal_directory_sync_rolls_back_creation(tmp_path, monkeypatch) -> None:
    original = journal_module.fsync_directory
    def fail(path):
        if (path / "journal.jsonl").exists():
            raise OSError("journal directory fsync falhou")
        original(path)
    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "fsync_directory", fail)
        with pytest.raises(OSError, match="journal directory fsync falhou"):
            create(tmp_path)
    assert not journal_path(tmp_path).parent.exists()
    with create(tmp_path):
        pass


def test_preserved_tail_cannot_create_first_event(tmp_path) -> None:
    with create(tmp_path) as journal:
        discarded = journal.directory / "discarded"
        discarded.mkdir()
        tail = b"partial"
        path = discarded / ("0" * 20 + "-" + hashlib.sha256(tail).hexdigest() + ".tail")
        path.write_bytes(tail)
    with pytest.raises(JournalError, match="journal_integrity_error.*RunIntent"):
        reopen(tmp_path)
    assert journal_path(tmp_path).read_bytes() == b""
    assert path.read_bytes() == tail
    assert not (journal_path(tmp_path).parent / "run.lock").exists()


def test_open_syncs_verified_journal_before_returning(tmp_path, monkeypatch) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
    calls = []
    original_verify = RunJournal._verify
    original_sync = journal_module.sync_file
    def verify(journal):
        result = original_verify(journal)
        calls.append("verify")
        return result
    def sync(path):
        assert calls == ["verify"]
        calls.append("sync")
        original_sync(path)
    with monkeypatch.context() as patch:
        patch.setattr(RunJournal, "_verify", verify)
        patch.setattr(journal_module, "sync_file", sync)
        with reopen(tmp_path) as journal:
            assert calls == ["verify", "sync"]
            assert journal.events == (FIRST,)


def test_failed_open_sync_releases_lock_and_refuses_state(tmp_path, monkeypatch) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
    with monkeypatch.context() as patch:
        patch.setattr(journal_module, "sync_file", lambda _: (_ for _ in ()).throw(OSError("open fsync falhou")))
        with pytest.raises(OSError, match="open fsync falhou"):
            reopen(tmp_path)
    assert not (journal_path(tmp_path).parent / "run.lock").exists()
    with reopen(tmp_path) as journal:
        assert journal.events == (FIRST,)


def test_discard_pending_file_is_reported_without_truncation(tmp_path) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
    path = journal_path(tmp_path)
    with path.open("ab") as handle:
        handle.write(b"partial")
    discarded = path.parent / "discarded"
    discarded.mkdir()
    pending = discarded / ".pending-crash"
    pending.write_bytes(b"evidence")
    before = path.read_bytes()
    with pytest.raises(JournalError, match="storage_pending_files.*pending-crash"):
        reopen(tmp_path)
    assert path.read_bytes() == before
    assert pending.read_bytes() == b"evidence"
    assert not (path.parent / "run.lock").exists()


@pytest.mark.parametrize("point", ["preserved", "partial_audit"])
def test_tail_recovery_survives_other_crash_points(tmp_path, monkeypatch, point) -> None:
    with create(tmp_path) as journal:
        append_fixture(journal, FIRST)
    path = journal_path(tmp_path)
    tail = b'{"original-partial"'
    with path.open("ab") as handle:
        handle.write(tail)
    original_publish = journal_module.publish_bytes
    original_append = RunJournal.append
    with monkeypatch.context() as patch:
        def crash_publish(target, content):
            original_publish(target, content)
            raise OSError("crash depois de preservar")
        def partial_audit(journal, **kwargs):
            actual = journal._file
            class PartialFile:
                def write(self, line):
                    return actual.write(line[:20])
                def close(self):
                    actual.close()
            journal._file = PartialFile()
            return original_append(journal, **kwargs)
        if point == "preserved":
            patch.setattr(journal_module, "publish_bytes", crash_publish)
        else:
            patch.setattr(RunJournal, "append", partial_audit)
        with pytest.raises(OSError):
            reopen(tmp_path)
    lock = path.parent / "run.lock"
    assert lock.exists()
    assert tail in [p.read_bytes() for p in (path.parent / "discarded").glob("*.tail")]
    if point == "preserved":
        assert path.read_bytes().endswith(tail)
    else:
        assert len(path.read_bytes().splitlines(keepends=True)[-1]) == 20
    lock.unlink()  # após inspeção manual do crash
    with reopen(tmp_path) as journal:
        audits = [e for e in journal.events if e["event_type"] == "journal_tail_discarded"]
        assert len(audits) == (1 if point == "preserved" else 2)
        assert len({e["idempotency_key"] for e in audits}) == len(audits)
        expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path.parent / "discarded").glob("*.tail")}
        assert {e["idempotency_key"].split("/")[-1]: e["payload"]["discarded_digest"]["value"] for e in audits} == expected
        before = journal.path.read_bytes()
    with reopen(tmp_path) as journal:
        assert journal.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "junction"])
@pytest.mark.parametrize("component", ["run", "artifacts", "discarded"])
def test_storage_rejects_directory_links_escaping_root(tmp_path, kind, component) -> None:
    from orq_contracts.artifact_store import ArtifactStore, ArtifactStoreError
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "run":
        link = journal_path(root).parent
        link.parent.mkdir(parents=True)
    else:
        with create(root) as journal:
            append_fixture(journal, FIRST)
        link = journal_path(root).parent / component
    if kind == "junction":
        if os.name != "nt":
            pytest.skip("junction é específica do Windows")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    else:
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                pytest.skip("symlink exige privilégio; junction tem cobertura separada")
            raise
    if component == "artifacts":
        with reopen(root) as journal:
            with pytest.raises(ArtifactStoreError, match="storage_path_escape"):
                ArtifactStore(journal)
    else:
        with pytest.raises(JournalError, match="storage_path_escape"):
            reopen(root)
        if component == "run":
            with pytest.raises(JournalError, match="storage_path_escape"):
                create(root)
    assert list(outside.iterdir()) == []
    assert not (journal_path(root).parent / "run.lock").exists()
