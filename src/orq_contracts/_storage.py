"""Primitivas locais de persistência durável, sem dependências externas."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


class StorageError(Exception):
    """Falha de armazenamento com código estável para os consumidores."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def confined_path(root: Path, *parts: str, error_type: type[StorageError] = StorageError) -> Path:
    candidate = root.joinpath(*parts)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise error_type("storage_path_escape", "caminho escapa da raiz do armazenamento")
    return candidate


def fsync_directory(path: Path) -> None:
    """Sincroniza metadata; falhas nunca são tratadas como sucesso."""
    if os.name == "nt":
        # O CRT não abre diretórios. A API nativa exige GENERIC_WRITE para flush.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel.FlushFileBuffers.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(path), 0x40000000, 7, None, 3, 0x02000000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.FlushFileBuffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
    else:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def ensure_directory(path: Path) -> None:
    if path.is_dir():
        return
    if not path.parent.is_dir():
        ensure_directory(path.parent)
    try:
        path.mkdir()
    except FileExistsError:
        if not path.is_dir():
            raise
    fsync_directory(path.parent)


def reject_pending_files(directory: Path, *, error_type: type[StorageError]) -> None:
    """Reporta lixo de crash; a inspeção/remoção é manual sob o lock do run."""
    pending = sorted(path.name for path in directory.glob(".pending-*"))
    if pending:
        raise error_type("storage_pending_files", f"temporários de escrita exigem inspeção: {pending}")


def sync_file(path: Path) -> None:
    # O CRT Windows exige handle gravável para _commit/FlushFileBuffers.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())
    fsync_directory(path.parent)


def publish_bytes(path: Path, content: bytes) -> bool:
    """Publica sem substituir; exige o escritor exclusivo do run.

    False indica destino existente, que o consumidor deve reler e verificar.
    """
    if path.exists():
        sync_file(path)
        return False
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if os.name == "nt":
                temporary.rename(path)
            else:
                os.link(temporary, path)
                temporary.unlink()
        except FileExistsError:
            sync_file(path)
            return False
        fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)
