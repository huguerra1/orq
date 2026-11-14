import json

from conftest import ROOT, load_fixture
from orq_contracts.canonical import canonicalize
from orq_contracts.cli import main


def arguments(root):
    fixtures = ROOT / "fixtures/v0.1"
    return ["run", "simulate", "--store-root", str(root), "--workflow", str(fixtures / "valid/workflow-reference.json"),
            "--request", str(fixtures / "run-request.json"), "--catalog", str(fixtures / "run-catalog.json"),
            "--policy", str(fixtures / "run-policy.json"), "--script", str(fixtures / "valid/simulation-script-reference.json"),
            "--knowledge-catalog", str(fixtures / "vault/catalog-snapshot.json"), "--vault-root", str(fixtures / "vault")]


def test_cli_simulate_succeeded_and_exclusive(tmp_path, capsys):
    args = arguments(tmp_path)
    assert main(args) == 0
    raw = capsys.readouterr().out
    result = json.loads(raw)
    assert result["state"] == "succeeded"
    assert raw == canonicalize(result).decode() + "\n"
    journal = tmp_path / "runs/run-reference/journal.jsonl"
    before = journal.read_bytes()
    assert result["journal_digest"] == json.loads(before.splitlines()[-1])["event_hash"]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "run_exists"
    assert journal.read_bytes() == before


def test_cli_returns_nonzero_for_failed_run(tmp_path, capsys):
    request = load_fixture("run-request.json")
    request["objective"] = "Outro objetivo"
    path = tmp_path / "request.json"
    path.write_bytes(canonicalize(request))
    args = arguments(tmp_path)
    args[args.index("--request")+1] = str(path)
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "failed"
