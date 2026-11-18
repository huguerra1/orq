"""Supervisor stdlib executado como processo separado (inclusive via python3 no WSL).

Não importa ORQ nem lê o ambiente de autenticação. O receipt só afirma término
local; nenhuma morte de processo comprova liquidação do consumo remoto.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


WINDOWS = os.name == "nt"
PERMISSION_ATTEMPTS = 100
_ALLOWED_EXTRA_ENV = frozenset({"PYTHONPATH"})


def retry_permission(operation):
    """Sharing violations do Windows são transitórias; outras falhas propagam."""
    for attempt in range(PERMISSION_ATTEMPTS):
        try:
            return operation()
        except PermissionError:
            if not WINDOWS or attempt == PERMISSION_ATTEMPTS - 1:
                raise
            time.sleep(.01)


def read_bytes(path: Path, limit: int = -1) -> bytes:
    def read():
        with path.open("rb") as stream:
            return stream.read(limit)
    return retry_permission(read)


def safe_environment(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    names = {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
             "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME"}
    env = {key: value for key, value in os.environ.items() if key.upper() in names}
    if not WINDOWS and env.get("HOME"):
        local_bin = os.path.join(env["HOME"], ".local", "bin")
        if os.path.isdir(local_bin):
            parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
            if local_bin not in parts:
                env["PATH"] = os.pathsep.join([*parts, local_bin])
    if extra_env:
        for key, value in extra_env.items():
            if key not in _ALLOWED_EXTRA_ENV or not isinstance(value, str):
                raise ValueError(f"variável de ambiente não permitida: {key}")
            env[key] = value
    return env


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.FlushFileBuffers.argtypes = [w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x40000000, 7, None, 3, 0x02000000, None)
        if handle == w.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.FlushFileBuffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
    else:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_record(path: Path, body: dict) -> None:
    # Subconjunto JCS fechado: chaves ASCII, strings, inteiros seguros, bool/null.
    data = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    record = {**body, "state_hash": {"algorithm": "sha256", "value": hashlib.sha256(data).hexdigest()}}
    descriptor, name = retry_permission(lambda: tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        retry_permission(lambda: os.replace(temporary, path))
        retry_permission(lambda: sync_directory(path.parent))
    finally:
        retry_permission(lambda: temporary.unlink(missing_ok=True))


class WindowsJob:
    """Job sem janela de fuga: processo nasce suspenso, é associado e retomado."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        self.kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID]

        class Basic(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_int64), ("per_job", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Limits(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())

    def attach_and_resume(self, process):
        import ctypes
        from ctypes import wintypes as w
        if not self.kernel.AssignProcessToJobObject(self.handle, w.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("thread", w.DWORD),
                        ("owner", w.DWORD), ("base_priority", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]

        self.kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        self.kernel.OpenThread.restype = w.HANDLE
        self.kernel.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.Thread32First.argtypes = [w.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.kernel.Thread32Next.argtypes = [w.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.kernel.ResumeThread.argtypes = [w.HANDLE]
        snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        resumed = False
        try:
            present = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if entry.owner == process.pid:
                    thread = self.kernel.OpenThread(2, False, entry.thread)
                    if thread:
                        resumed = self.kernel.ResumeThread(thread) != 0xFFFFFFFF or resumed
                        self.kernel.CloseHandle(thread)
                present = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(snapshot)
        if not resumed:
            raise OSError("não foi possível retomar processo associado ao Job")

    def stop(self):
        import ctypes
        self.kernel.TerminateJobObject(self.handle, 1)
        # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION: quatro LARGE_INTEGER e quatro DWORD.
        counters = (ctypes.c_uint64 * 6)()
        for _ in range(100):
            if self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(counters), ctypes.sizeof(counters), None):
                active = counters[5] & 0xFFFFFFFF
                if active == 0:
                    return True
            time.sleep(.02)
        return False

    def close(self):
        self.kernel.CloseHandle(self.handle)


def group_gone(pid: int, timeout: float = 2.) -> bool:
    end = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if time.monotonic() >= end:
            return False
        time.sleep(.02)


def main(directory: Path):
    config = json.loads(read_bytes(directory / "launch.json"))
    receipt = directory / "receipt.json"
    common = {"fingerprint": config["fingerprint"]}
    write_record(receipt, {**common, "phase": "launching"})
    job = WindowsJob() if WINDOWS else None
    process = None
    recorded = False
    try:
        with (directory / "stdout").open("wb") as stdout, (directory / "stderr").open("wb") as stderr:
            if time.time_ns() // 1000000 >= config["budget_deadline_ms"]:
                write_record(receipt, {**common, "phase": "not_started"})
                return
            try:
                process = subprocess.Popen(config["argv"], cwd=config["cwd"], stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, shell=False,
                                           env=safe_environment(config.get("env")), start_new_session=not WINDOWS,
                                           creationflags=(0x4 | subprocess.CREATE_NO_WINDOW) if WINDOWS else 0)
                if job:
                    job.attach_and_resume(process)
            except OSError:
                if process is None:
                    write_record(receipt, {**common, "phase": "not_started"})
                    return
                raise
            write_record(receipt, {**common, "phase": "running"})
            start = time.monotonic()
            reason = None
            while process.poll() is None:
                if (directory / "cancel").exists():
                    reason = "cancelled"
                elif (time.monotonic() - start >= config["timeout_ms"] / 1000
                      or time.time_ns() // 1000000 >= config["budget_deadline_ms"]):
                    reason = "timeout"
                elif any((directory / name).stat().st_size > 8 * 1024 * 1024 for name in ("stdout", "stderr")):
                    reason = "output_limit"
                if reason:
                    break
                time.sleep(.02)
            confirmed = True
            if job:
                confirmed = job.stop()  # elimina também filhos deixados após a saída do pai
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    confirmed = False
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                confirmed, code = False, None
            if not job:
                confirmed = group_gone(process.pid) and confirmed
            for stream in (stdout, stderr):
                stream.flush()
                os.fsync(stream.fileno())
            write_record(receipt, {**common, "phase": "terminal" if confirmed else "unknown",
                                   "returncode": code, "stop_reason": reason,
                                   "remote_consumption": "unknown"})
            recorded = True
    finally:
        try:
            if job:
                job.close()
        finally:
            try:
                # AssignProcessToJobObject pode falhar antes de o Job possuir o filho.
                if process and process.poll() is None:
                    if job:
                        process.kill()
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                if process is not None and not recorded:
                    write_record(receipt, {**common, "phase": "unknown", "remote_consumption": "unknown"})


if __name__ == "__main__":
    main(Path(sys.argv[1]))
