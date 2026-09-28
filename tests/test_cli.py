from __future__ import annotations

import json
from pathlib import Path

from orq_contracts.cli import main


ROOT = Path(__file__).resolve().parents[1]


def test_validate_cli_reports_valid_document(capsys) -> None:
    path = ROOT / "fixtures" / "v0.1" / "valid" / "workflow-minimal.json"

    assert main(["validate", "workflow", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"valid": True, "errors": []}


def test_validate_cli_returns_one_for_invalid_document(capsys) -> None:
    path = ROOT / "fixtures" / "v0.1" / "invalid" / "task-empty-objective.json"

    assert main(["validate", "task", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is False
    assert result["errors"][0]["path"] == "/objective"


def test_digest_cli_returns_sha256(capsys) -> None:
    path = ROOT / "fixtures" / "v0.1" / "valid" / "task-minimal.json"

    assert main(["digest", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["algorithm"] == "sha256"
    assert len(result["value"]) == 64
