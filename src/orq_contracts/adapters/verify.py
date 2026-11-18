"""Sonda somente leitura de disponibilidade e versão das CLIs oficiais (V01–V12).

Nunca lê diretórios de credenciais nem promove controles `unknown` para `supported`
sem a sessão completa de verificação V01–V12 com ArtifactRefs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Callable

from ._supervisor import safe_environment

ADAPTER_BINARIES: tuple[tuple[str, str], ...] = (
    ("family_claude", "claude"),
    ("family_codex", "codex"),
    ("family_gemini", "agy"),
    ("family_kiro", "kiro-cli"),
)
V01_V12_IDS: tuple[str, ...] = tuple(f"V{index:02d}" for index in range(1, 13))


def probe_cli_versions(
    *,
    include_wsl: bool = False,
    platform: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Consulta `--version` das CLIs com `stdin` fechado e ambiente saneado."""

    host_platform = platform or sys.platform
    env = safe_environment()
    results: list[dict[str, Any]] = []
    for family_id, binary in ADAPTER_BINARIES:
        if binary == "kiro-cli" and host_platform == "win32" and not include_wsl:
            results.append(
                {
                    "family_id": family_id,
                    "cli": binary,
                    "status": "skipped_wsl_disabled",
                    "installed": False,
                    "detected_version": None,
                    "controls_status": "unknown",
                    "pending_checklist": list(V01_V12_IDS),
                }
            )
            continue
        argv = (
            ["wsl.exe", "--", "kiro-cli", "--version"]
            if binary == "kiro-cli" and host_platform == "win32"
            else [binary, "--version"]
        )
        try:
            completed = runner(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                env=env,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            results.append(
                {
                    "family_id": family_id,
                    "cli": binary,
                    "status": "unavailable",
                    "installed": False,
                    "detected_version": None,
                    "error": type(error).__name__,
                    "controls_status": "unknown",
                    "pending_checklist": list(V01_V12_IDS),
                }
            )
            continue
        output = (completed.stdout or completed.stderr or "").strip().splitlines()
        first_line = output[0].strip() if output else None
        ok = completed.returncode == 0 and bool(first_line)
        results.append(
            {
                "family_id": family_id,
                "cli": binary,
                "status": "detected" if ok else "error",
                "installed": ok,
                "detected_version": first_line if ok else None,
                "returncode": completed.returncode,
                "controls_status": "unknown",
                "pending_checklist": list(V01_V12_IDS),
            }
        )
    return {
        "schema_version": "0.1",
        "platform": host_platform,
        "os_name": os.name,
        "all_controls_status": "unknown",
        "adapters": results,
    }
