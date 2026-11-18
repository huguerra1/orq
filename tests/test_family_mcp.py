"""MCP oficial em memória e stdio: escopo, escolha, orçamento e trilha 7E."""
from copy import deepcopy
import hashlib
import json
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters
import pytest

import _search_vault
from _memory_vault import audience
from test_phase7_scenarios import data_for, runtime, SubscriptionAgent, no_network
from test_runner_scenarios import make_runner, events
from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.family_mcp import AttemptKnowledgeSession
from orq_contracts.mcp_server import create_family_knowledge_server
from orq_contracts.validator import validate_instance


@pytest.fixture
def anyio_backend():
    return "asyncio"


class PassiveAgent(SubscriptionAgent):
    def dispatch(self, intent, bundle, workspace):
        self.dispatches.append(intent["attempt_id"])
        return self.inner.dispatch(intent, bundle, workspace)


@pytest.fixture
def live_session(tmp_path, monkeypatch):
    sources = list(_search_vault.SEARCH_SOURCES) + [
        ("extra-one", None, "# Reconciliação\n\nReconciliação com registros duráveis.\n"),
        ("extra-two", None, "# Reconciliação\n\nReconciliação com orçamento auditável.\n"),
        ("gemini-only", audience(["family_gemini"]), "# Gemini\n\nReconciliação na família Gemini.\n"),
    ]
    monkeypatch.setattr(_search_vault, "SEARCH_SOURCES", sources)
    opened = []
    def create(*, family="codex", budget=2048):
        data, profiles = data_for(tmp_path / ("vault-" + str(len(opened))))
        data["document"]["attempts"] = {}
        data["policy"]["knowledge_max_tokens"] = budget
        if family != "codex":
            role = "code_reviewer" if family == "kiro" else "researcher"
            task = data["workflow"]["tasks"][0]
            task["task_type"] = "review" if family == "kiro" else "documentation"
            task["agent_role"]["id"] = role
            for target in data["catalog"]["targets"]:
                target["supported_role_refs"] = [task["agent_role"]]
            data["catalog"]["catalog_hash"] = canonical_digest({k: v for k, v in data["catalog"].items() if k != "catalog_hash"})
        rt = runtime(profiles)
        rt.executor_factory = PassiveAgent
        runner = make_runner(tmp_path / ("store-" + str(len(opened))), data, family_runtime=rt)
        opened.append(runner)
        runner._prepare()
        runner._transition("task", "T1", "ready", "dependencies_accepted")
        runner._admit("T1")
        session = next(iter(runner.executor.sessions.values()))
        assert session.scope.family_id == "family_" + family
        return runner, session
    yield create
    for runner in opened:
        runner.close()


def response(result):
    return json.loads(result.content[0].text)


def refs(candidates):
    return [{k: c[k] for k in ("source_id", "revision", "unit_id")} for c in candidates]


@pytest.mark.anyio
async def test_Q23_runner_scope_cannot_be_overridden(live_session):
    runner, session = live_session(family="kiro")
    async with Client(create_family_knowledge_server(session)) as client:
        tools = (await client.list_tools()).tools
        assert {t.name for t in tools} == {"search_family_knowledge", "select_family_knowledge"}
        for tool in tools:
            assert not {"family_id", "role_id", "project_id", "catalog_id", "run_id", "task_id", "attempt_number"} & set(tool.input_schema.get("properties", {}))
            assert tool.annotations.read_only_hint is True
        result = await client.call_tool("search_family_knowledge", {
            "requirement_id": "K1", "query": "reconciliação", "family_id": "family_claude"})
        assert result.is_error
        assert response(result)["error"]["code"] == "invalid_tool_arguments"
        found = response(await client.call_tool("search_family_knowledge", {"requirement_id": "K1", "query": "reconciliação"}))["candidates"]
        assert all(c["source_id"] not in {"codex-only", "claude-only", "gemini-only"} for c in found)
        summary = (await client.read_resource("orq://catalog/summary")).contents[0].text
        assert "claude-only" not in summary and "content_locator" not in summary
    assert session.scope.family_id == "family_kiro"
    assert len(session.entries()) == 2
    assert session.entries()[0]["session_ref"] == runner.projection.latest_attempt("T1").intent["knowledge_session_ref"]


@pytest.mark.anyio
async def test_Q24_six_candidates_agent_chooses_two_and_runner_seals(live_session):
    runner, session = live_session()
    async with Client(create_family_knowledge_server(session)) as client:
        candidates = response(await client.call_tool("search_family_knowledge", {
            "requirement_id": "K1", "query": "reconciliação configuração QuotaStateRecord Git", "limit": 6}))["candidates"]
        assert len(candidates) == 6
        selected = response(await client.call_tool("select_family_knowledge", {"selections": {"K1": refs(candidates[:2])}, "rationale": "Duas unidades suficientes."}))
        assert len(selected["manifest"]["items"]) == 2
        assert selected["selection"]["selection_mode"] == "agent_mcp_choice"
        assert not validate_instance(selected["selection"], "knowledge_selection")
        assert selected["manifest"]["bundle_hash"]["value"] == hashlib.sha256(canonicalize(selected["bundle"])).hexdigest()
        for item, content in zip(selected["manifest"]["items"], selected["bundle"]["items"], strict=True):
            assert item["content_hash"]["value"] == hashlib.sha256(content["content"].encode()).hexdigest()
        ref = refs(candidates[:1])[0]
        resource = await client.read_resource("orq://knowledge/" + "/".join(ref[k] for k in ("source_id", "revision", "unit_id")))
        assert json.loads(resource.contents[0].text)["content"]
    assert runner.run().state == "succeeded"
    sealed = events(runner, "attempt_context_sealed")[0]["payload"]
    assert sealed["manifest"] == selected["manifest"]
    assert sealed["audit_hash"] == canonical_digest(session.entries())
    assert events(runner, "dispatch_requested")[0]["global_sequence"] < events(runner, "attempt_context_sealed")[0]["global_sequence"]


@pytest.mark.anyio
@pytest.mark.parametrize("budget,choose_empty,expected", [(2048, True, "mandatory_requirement_uncovered"), (1, False, "context_budget_exceeded")])
async def test_Q25_tool_refuses_missing_coverage_and_over_budget(live_session, budget, choose_empty, expected):
    _, session = live_session(budget=budget)
    async with Client(create_family_knowledge_server(session)) as client:
        candidates = response(await client.call_tool("search_family_knowledge", {"requirement_id": "K1", "query": "reconciliação"}))["candidates"]
        result = await client.call_tool("select_family_knowledge", {"selections": {"K1": [] if choose_empty else refs(candidates)}})
        assert result.is_error and response(result)["error"]["code"] == expected
        assert session.selection is None
    selected, _, _ = session.seal()
    assert selected is None


@pytest.mark.anyio
async def test_Q22_offline_gemini_scope_and_record(live_session):
    runner, session = live_session(family="gemini")
    assert session.backend.backend_id == "offline_markdown_bm25"
    async with Client(create_family_knowledge_server(session)) as client:
        candidates = response(await client.call_tool("search_family_knowledge", {"requirement_id": "K1", "query": "reconciliação"}))["candidates"]
        assert "gemini-only" in {c["source_id"] for c in candidates}
        assert not {"codex-only", "claude-only"} & {c["source_id"] for c in candidates}
        result = response(await client.call_tool("select_family_knowledge", {"selections": {"K1": refs(candidates)}}))
        assert result["selection"]["backend"] == "offline_markdown_bm25"
        assert result["selection"]["ranking_version"] == "bm25-v1"
    assert runner.run().state == "succeeded"


def test_audit_detects_tampering_and_interrupted_call(live_session):
    _, session = live_session()
    session.call("search_family_knowledge", {"requirement_id": "K1", "query": "reconciliação"})
    path = session.directory / "call-000001.json"
    original = path.read_bytes()
    changed = json.loads(original)
    changed["arguments"]["query"] = "alterada"
    path.write_bytes(canonicalize(changed))
    with pytest.raises(ValueError, match="knowledge_audit_integrity_mismatch"):
        session.seal()
    path.write_bytes(original)
    (session.directory / "intent-000002.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="knowledge_call_indeterminate"):
        session.seal()


def test_server_restart_restores_search_and_forbids_second_delivery(live_session):
    _, session = live_session()
    result = session.call("search_family_knowledge", {"requirement_id": "K1", "query": "reconciliação"})
    restarted = AttemptKnowledgeSession(session.directory, session.config, session.backend, clock=session.clock)
    chosen = restarted.call("select_family_knowledge", {"selections": {"K1": refs(result["candidates"][:1])}})
    assert "manifest" in chosen
    restarted_again = AttemptKnowledgeSession(session.directory, session.config, session.backend, clock=session.clock)
    result = restarted_again.call("select_family_knowledge", {"selections": {"K1": refs(result["candidates"][:1])}})
    assert result["error"]["code"] == "selection_already_closed"


@pytest.mark.anyio
async def test_scoped_stdio_server_uses_host_config(live_session):
    runner, session = live_session(family="kiro")
    catalog_path = session.directory / "catalog.json"
    catalog_path.write_bytes(canonicalize(runner.knowledge_catalog))
    parameters = StdioServerParameters(**session.stdio_launch(runner.vault_root),
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "ORQ_KNOWLEDGE_BACKEND": "offline_markdown_bm25"})
    async with Client(parameters, read_timeout_seconds=10) as client:
        found = response(await client.call_tool("search_family_knowledge", {"requirement_id": "K1", "query": "reconciliação"}))["candidates"]
        assert found and all(c["source_id"] not in {"codex-only", "claude-only"} for c in found)
    assert len(session.entries()) == 1
