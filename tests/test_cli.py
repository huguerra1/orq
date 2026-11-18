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
    manifest = json.loads((context / "manifest.json").read_text(encoding="utf-8"))
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
    assert output.read_text(encoding="utf-8") == "preservar"
    assert "saída já existe" in json.loads(capsys.readouterr().out)["error"]


def test_plan_and_route_cli_commands(tmp_path, capsys) -> None:
    record_out = tmp_path / "planning-record.json"
    workflow_out = tmp_path / "workflow.json"
    decision_out = tmp_path / "routing-decision.json"
    fixtures = ROOT / "fixtures" / "v0.1"

    assert main(
        [
            "plan",
            "--request",
            str(fixtures / "planning-request.json"),
            "--template",
            str(fixtures / "valid" / "workflow-minimal.json"),
            "--output-record",
            str(record_out),
            "--output-workflow",
            str(workflow_out),
            "--completed-at",
            "2026-09-29T13:00:01Z",
        ]
    ) == 0
    plan_summary = json.loads(capsys.readouterr().out)
    assert plan_summary["status"] == "accepted"
    assert json.loads(record_out.read_text(encoding="utf-8"))["status"] == "accepted"
    assert json.loads(workflow_out.read_text(encoding="utf-8"))["workflow_id"] == "workflow-demo"

    assert main(
        [
            "route",
            "--request",
            str(fixtures / "routing-request.json"),
            "--catalog",
            str(fixtures / "routing-target-catalog.json"),
            "--policy",
            str(fixtures / "routing-policy-rules.json"),
            "--output",
            str(decision_out),
        ]
    ) == 0
    route_summary = json.loads(capsys.readouterr().out)
    assert route_summary["decision_status"] == "selected"
    assert route_summary["effective_selection"]["selected_target_id"] == "target-quality"
    assert json.loads(decision_out.read_text(encoding="utf-8"))["effective_selection"]["selected_target_id"] == "target-quality"


def test_adapters_probe_versions_cli(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "orq_contracts.cli.probe_cli_versions",
        lambda include_wsl=False: {
            "schema_version": "0.1",
            "all_controls_status": "unknown",
            "include_wsl": include_wsl,
            "adapters": [],
        },
    )
    assert main(["adapters", "probe-versions"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["all_controls_status"] == "unknown" and out["include_wsl"] is False
