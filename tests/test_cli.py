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


def test_vault_cli_runs_catalog_selection_and_context_chain(tmp_path, capsys) -> None:
    vault = ROOT / "fixtures" / "v0.1" / "vault"
    catalog = tmp_path / "catalog.json"
    selection = tmp_path / "selection.json"
    context = tmp_path / "context"

    assert main(
        [
            "vault",
            "catalog",
            "--root",
            str(vault),
            "--definition",
            str(vault / "catalog-definition.json"),
            "--output",
            str(catalog),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert main(
        [
            "vault",
            "select",
            "--root",
            str(vault),
            "--catalog",
            str(catalog),
            "--request",
            str(vault / "selection-request.json"),
            "--output",
            str(selection),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    assert main(
        [
            "vault",
            "context",
            "--root",
            str(vault),
            "--catalog",
            str(catalog),
            "--selection",
            str(selection),
            "--request",
            str(vault / "context-request.json"),
            "--output-dir",
            str(context),
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
    manifest = json.loads((context / "manifest.json").read_text())
    assert manifest["items"][0]["knowledge_provenance"]["selection_id"] == "selection-run-fixture-T1-1"
    assert (context / "bundle.json").read_bytes()
    assert sorted(path.name for path in (context / "items").iterdir()) == [
        f"{item['item_id']}.bin" for item in manifest["items"]
    ]


def test_vault_cli_does_not_overwrite_output(tmp_path, capsys) -> None:
    vault = ROOT / "fixtures" / "v0.1" / "vault"
    output = tmp_path / "catalog.json"
    output.write_text("preservar")

    result = main(
        [
            "vault",
            "catalog",
            "--root",
            str(vault),
            "--definition",
            str(vault / "catalog-definition.json"),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert output.read_text() == "preservar"
    assert "saída já existe" in json.loads(capsys.readouterr().out)["error"]
