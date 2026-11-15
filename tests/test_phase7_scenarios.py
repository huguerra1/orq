"""7E: runner real, agente simulado cliente MCP oficial e memória offline.

Q01–Q06/Q14 e os casos Postgres são mapeados aos testes existentes na tabela
de cobertura; este arquivo executa os cenários novos de integração, sem rede.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import asyncio
import json
import socket

import pytest
from mcp import Client

from _search_vault import build_search_vault, requirement
from _memory_vault import PROJECT
from conftest import load_fixture
from test_runner_scenarios import inputs, make_runner, events, edges, InjectedCrash
from orq_contracts.adapters.quota import QuotaLimitDetector
from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.executor import seal_record, stable_id, error_record, ExecutorError
from orq_contracts.knowledge_backend import OfflineMarkdownBackend
from orq_contracts.mcp_server import create_family_knowledge_server
from orq_contracts.projections import project_journal, record_ref
from orq_contracts.runner_family import FamilyRuntime
from orq_contracts.runner import reference
from orq_contracts.router import route_and_finalize
from orq_contracts.quota import QuotaStateSnapshot
from orq_contracts.subscription_hub import SubscriptionHub
from orq_contracts.memory_store.config import resolve_memory_config
from orq_contracts.simulated_executor import parse_utc, format_utc
from orq_contracts.validator import validate_instance


LIMIT = "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/plus) or try again at 10:05 PM."


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("rede/CLI real proibida")
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    original = socket.socket.connect
    def local_asyncio_pipe(sock, address):
        # asyncio implementa socketpair por loopback no Windows; só IPC local.
        if isinstance(address, tuple) and address[0] == "127.0.0.1":
            return original(sock, address)
        return forbidden(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_asyncio_pipe)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def data_for(root, *, all_families=False):
    data = inputs()
    root.mkdir(parents=True, exist_ok=True)
    data["knowledge_catalog"] = build_search_vault(root, "cat-7e")
    data["vault_root"] = root
    data["workflow"]["project_id"] = data["request"]["project_id"] = PROJECT
    task = data["workflow"]["tasks"][0]
    task["task_type"] = "analysis" if all_families else "generation"
    task["knowledge_requirements"] = [requirement("K1")]
    task["agent_role"]["id"] = "backend_engineer"
    data["policy"]["routing_policy"] = load_fixture("valid/routing-policy-subscription.json")
    data["document"]["clock"]["start"] = "2026-11-14T23:15:00Z"
    data["document"]["attempts"] = {"T1": {"1": {"execution": "fail_retryable", "reconcile": "found_failed"}}}
    profiles = {}
    targets = []
    for index, (family, cli) in enumerate((("codex", "codex"), ("claude", "claude"), ("kiro", "kiro-cli"), ("gemini", "agy"))):
        profile = load_fixture("valid/subscription-profile.json")
        profile.update(subscription_id="sub-" + family, family_id="family_" + family,
                       quota_pool_id="pool-" + family, cli_executable=cli, detector_version=cli + "-quota-v1")
        profile = seal_record(profile, "subscription_profile")
        profiles[profile["subscription_id"]] = profile
        target = deepcopy(data["catalog"]["targets"][0])
        target.update(target_id="target-" + family, priority=index + 1,
                      supported_task_types=["analysis", "generation", "review", "test", "documentation"],
                      supported_role_refs=[task["agent_role"]],
                      **{key: profile[key] for key in ("family_id", "subscription_id", "quota_pool_id")})
        targets.append(target)
    data["catalog"]["targets"] = targets
    data["catalog"]["catalog_hash"] = canonical_digest({k: v for k, v in data["catalog"].items() if k != "catalog_hash"})
    return data, profiles


class SubscriptionAgent:
    """Executa o simulado 6D e faz chamadas MCP durante dispatch, como agente.

    Falhas de cota passam pelo detector 7C, sem fabricar eventos de runner.
    collect é puro sobre o resultado durável do simulado, inclusive na retomada.
    """
    def __init__(self, runner, *, message=LIMIT, choice="valid"):
        self.runner, self.inner = runner, runner.simulated_executor
        self.message, self.choice = message, choice
        self.sessions = {}
        self.dispatches = []

    def describe(self):
        return self.inner.describe()

    def bind_knowledge_session(self, intent, session):
        self.sessions[intent["attempt_id"]] = session

    def dispatch(self, intent, bundle, workspace):
        assert events(self.runner, "dispatch_requested", "attempt", intent["attempt_id"])
        self.dispatches.append(intent["attempt_id"])
        ack = self.inner.dispatch(intent, bundle, workspace)
        session = self.sessions[intent["attempt_id"]]
        async def agent():
            async with Client(create_family_knowledge_server(session)) as client:
                result = await client.call_tool("search_family_knowledge", {"requirement_id": "K1", "query": "reconciliação"})
                candidates = json.loads(result.content[0].text)["candidates"]
                refs = [{k: c[k] for k in ("source_id", "revision", "unit_id")} for c in candidates]
                response = await client.call_tool("select_family_knowledge", {"selections": {"K1": refs if self.choice == "valid" else []}})
                content = json.loads(response.content[0].text)
                assert ("error" in content) == (self.choice != "valid"), content
        asyncio.run(agent())
        return ack

    def observe(self, *args):
        return self.inner.observe(*args)

    def cancel(self, *args):
        return self.inner.cancel(*args)

    def reconcile(self, *args):
        return self.inner.reconcile(*args)

    def collect(self, intent):
        result = self.inner.collect(intent)
        target = self.runner.family_runtime.target(intent)
        profile = self.runner.family_runtime.profiles[target["subscription_id"]]
        usage = deepcopy(result.usage[0])
        usage["subscription_id"] = profile["subscription_id"]
        usage["cost_lines"] = [{"status": "unavailable", "amount": None, "currency": None,
                                "method": "unavailable", "source_ref": None}]
        # Preserve os campos fechados da linha de custo 6D.
        original_line = result.usage[0]["cost_lines"][0]
        usage["cost_lines"] = [{**original_line, "status": "unavailable", "amount": None, "currency": None}]
        usage["coverage"] = {"reported": [], "estimated": [], "unavailable": ["cost"]}
        usage = seal_record(usage, "usage_record")
        errors = result.errors
        if errors:
            detector = QuotaLimitDetector(profile["cli_executable"], local_timezone=profile["local_timezone"])
            detection = detector.detect(returncode=1, final_response=False,
                                        diagnostics=(self.message,), observed_at=usage["observed_at"])
            error = deepcopy(errors[0])
            if detection.reason == "quota_exhausted":
                error.update(code="quota_limit_reached", category="provider", retryability="retryable", termination_certainty="confirmed",
                    quota_state=detector.state_record(detection, profile=profile, intent=intent, observed_at=usage["observed_at"]))
            elif detection.reason in {"network_unavailable", "authentication_unavailable"}:
                error.update(code="execution_failed", message=detection.reason, category="provider", retryability="retryable")
            errors = (seal_record(error, "error_record"),)
        return replace(result, usage=(usage,), errors=errors)

    def quota_consumption(self, intent):
        result = self.collect(intent)
        usage = result.usage[0]
        target = self.runner.family_runtime.target(intent)
        return seal_record({"schema_version": "0.1", "quota_consumption_id": stable_id("consumption", intent["attempt_id"]),
            "run_id": intent["run_id"], "task_id": intent["task_id"], "attempt_id": intent["attempt_id"],
            **{key: target[key] for key in ("family_id", "subscription_id", "quota_pool_id")},
            "usage_record_ref": {"id": usage["usage_id"], "version": "0.1", "content_digest": canonical_digest(usage)}, "messages_consumed": 1,
            "input_tokens": None, "output_tokens": None, "credit_multiplier": None,
            "quota_outcome": "quota_exhausted_on_call" if any(e["code"] == "quota_limit_reached" for e in result.errors) else "unknown" if result.errors else "accepted",
            "recorded_at": usage["recorded_at"]}, "quota_consumption_record")


def runtime(profiles, **kwargs):
    message = kwargs.pop("message", LIMIT)
    choice = kwargs.pop("choice", "valid")
    return FamilyRuntime(profiles, lambda r: SubscriptionAgent(r, message=message, choice=choice),
                         memory_config=resolve_memory_config({}), **kwargs)


def test_Q07_Q08_Q13_Q26_quota_fallback_and_effective_context(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles)) as r:
        s = r.run()
        assert s.state == "succeeded"
        first, second = [s.attempts[aid] for aid in s.tasks["T1"].attempt_ids]
        assert (first.state, second.state) == ("failed", "completed")
        quota = events(r, "quota_exhausted_recorded")[0]["payload"]
        assert parse_utc(quota["reset_at"]) == parse_utc(quota["blocked_until"]) == parse_utc("2026-11-15T01:05:00Z")
        assert quota["status"] == "quota_exhausted" and quota["subscription_id"] == "sub-codex"
        assert ("running", "retry_wait") in edges(r, "task", "T1")
        decision = events(r, "routing_decision_recorded")[-1]["payload"]
        assert decision["effective_selection"] == {"recommended_target_id": "target-codex",
            "selected_target_id": "target-claude", "used_fallback": True, "rule": "quota_fallback"}
        blocked = next(c for c in decision["candidates"] if c["target_ref"]["id"] == "target-codex")
        assert not blocked["eligible"] and blocked["reasons"][0]["code"] == "quota_exhausted"
        contexts = [e["payload"] for e in events(r, "attempt_context_sealed")]
        one, two = contexts
        assert one["manifest"]["context_manifest_id"] != two["manifest"]["context_manifest_id"]
        assert one["manifest"]["bundle_hash"] != two["manifest"]["bundle_hash"]
        for sealed, family, exclusive, forbidden in [(one, "family_codex", "codex-only", "claude-only"), (two, "family_claude", "claude-only", "codex-only")]:
            assert sealed["session_config"]["scope"]["family_id"] == family
            sources = {i["knowledge_provenance"]["unit_ref"]["source_id"] for i in sealed["manifest"]["items"]}
            assert exclusive in sources and forbidden not in sources
            assert sealed["selection"]["selection_mode"] == "agent_mcp_choice"
            assert sealed["audit_hash"] == canonical_digest(sealed["audit"])
        assert all(u["cost_lines"][0]["status"] == "unavailable" and u["cost_lines"][0]["amount"] is None for u in s.usages.values())
        assert all(e["payload"]["amount"] is None for e in events(r, "budget_reserved") if e["payload"]["scope_type"] == "attempt")
        assert len(s.quota_consumptions) == 2
        assert s.cost_summary["unavailable_count"] == 2 and not s.cost_summary["total_cost_exact"]
        persisted = [json.loads(line) for line in r.journal.path.read_bytes().splitlines()]
        replayed = project_journal(persisted, workflow=r.workflow, reconcile_not_found_authoritative=True)
        assert persisted[-1]["event_hash"] == r.journal.final_digest
        assert replayed.state == "succeeded" and replayed.quota_states == s.quota_states
        assert len(r.executor.dispatches) == len(set(r.executor.dispatches)) == 2


def test_Q10_cooldown_and_family_fallback(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles, message="usage limit")) as r:
        assert r.run().state == "succeeded"
        quota = events(r, "quota_exhausted_recorded")[0]["payload"]
        assert quota["reset_at"] is None and quota["status"] == "cooldown"
        assert quota["reset_source"] == "cooldown_policy"
        assert parse_utc(quota["blocked_until"]) - parse_utc(quota["observed_at"]) == timedelta(minutes=60)
        assert events(r, "routing_decision_recorded")[-1]["payload"]["effective_selection"]["rule"] == "quota_fallback"


def test_Q03_unknown_control_blocks_real_priority(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    for target in data["catalog"]["targets"]:
        target["controls"][0].update(support="unknown", evidence_refs=[])
    data["catalog"]["catalog_hash"] = canonical_digest({k: v for k, v in data["catalog"].items() if k != "catalog_hash"})
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles)) as r:
        assert r.run().state == "failed"
        decision = events(r, "routing_decision_recorded")[0]["payload"]
        assert decision["effective_selection"] is None
        assert all(any(reason["code"] == "control_unknown" for reason in c["reasons"]) for c in decision["candidates"])
        assert not events(r, "dispatch_requested")


def test_Q27_changed_vault_fails_integrity_before_any_attempt(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    (data["vault_root"] / "content" / "codex-only.md").write_text("# Alterado\n", encoding="utf-8")
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles)) as r:
        assert r.run().state == "failed"
        assert not events(r, "dispatch_requested") and not r.projection.attempts
        assert events(r, "error_attached")[0]["payload"]["code"] == "integrity_mismatch"


def test_Q12_all_four_pools_blocked_no_fifth_dispatch(tmp_path):
    data, profiles = data_for(tmp_path / "vault", all_families=True)
    # Tipos genéricos mantêm prioridade do catálogo; quatro recusas confirmadas.
    data["workflow"]["constraints"]["max_attempts"] = data["policy"]["resolved_limits"]["max_attempts"] = 5
    data["workflow"]["tasks"][0]["constraints"]["max_attempts"] = 5
    data["document"]["attempts"] = {"T1": {str(n): {"execution": "fail_retryable", "reconcile": "found_failed"} for n in range(1, 5)}}
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles, message="usage limit")) as r:
        assert r.run().state == "failed"
        assert len(events(r, "dispatch_requested")) == 4
        decision_event = events(r, "routing_decision_recorded")[-1]
        decision = decision_event["payload"]
        assert decision["decision_status"] == "no_eligible_target"
        assert decision["effective_selection"] is None
        assert decision["reason"]["code"] == "all_eligible_families_quota_exhausted"
        assert len(r.projection.quota_states) == 4
        assert all(any(reason["code"] == "quota_exhausted" for reason in c["reasons"]) for c in decision["candidates"])
        assert events(r, "admission_rejected")
        assert not [e for e in events(r, "dispatch_requested") if e["global_sequence"] > decision_event["global_sequence"]]


def add_second_task(data):
    task = deepcopy(data["workflow"]["tasks"][0])
    task.update(task_id="T2", dependencies=["T1"], inputs=[])
    data["workflow"]["tasks"].append(task)
    data["workflow"]["outputs"][0]["source_task_id"] = "T2"
    data["workflow"]["constraints"]["max_run_duration_ms"] = data["policy"]["resolved_limits"]["max_run_duration_ms"] = 86400000


def test_Q09_explicit_reset_restores_codex_before_new_task(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    add_second_task(data)
    def advance(r, event):
        if event["event_type"] == "outputs_accepted" and event["aggregate_id"] == "T1":
            r.clock.advance_to("2026-11-15T01:06:00Z")
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles), on_event=advance) as r:
        assert r.run().state == "succeeded"
        reset = events(r, "quota_reset_observed")[0]
        decision = events(r, "routing_decision_recorded", "task", "T2")[0]
        assert reset["global_sequence"] < decision["global_sequence"]
        assert reset["payload"]["status"] == "available"
        assert decision["payload"]["effective_selection"]["selected_target_id"] == "target-codex"
        assert not events(r, "quota_probe_requested")


@pytest.mark.parametrize("available", [True, False, None])
def test_cooldown_requires_durable_probe_and_reblocks_when_refused(tmp_path, available):
    data, profiles = data_for(tmp_path / "vault")
    add_second_task(data)
    probes = []
    def probe(target, request):
        probes.append(request)
        assert request["probe_id"]
        return {"available": available, "diagnostic": "sondagem simulada"}
    def advance(r, event):
        if event["event_type"] == "outputs_accepted" and event["aggregate_id"] == "T1":
            r.clock.advance_to(format_utc(parse_utc(r.clock.now()) + timedelta(minutes=61)))
    with make_runner(tmp_path / "store", data,
                     family_runtime=runtime(profiles, message="usage limit", probe=probe), on_event=advance) as r:
        assert r.run().state == "succeeded"
        assert len(probes) == 1
        request, observed = events(r, "quota_probe_requested")[0], events(r, "quota_probe_observed")[0]
        assert request["global_sequence"] < observed["global_sequence"]
        chosen = events(r, "routing_decision_recorded", "task", "T2")[0]["payload"]["effective_selection"]["selected_target_id"]
        assert chosen == ("target-codex" if available is True else "target-claude")
        assert bool(events(r, "quota_reset_observed")) == (available is True)
        assert len(events(r, "quota_exhausted_recorded")) == (2 if available is False else 1)


@pytest.mark.parametrize("event_type", ["dispatch_requested", "quota_exhausted_recorded", "quota_consumption_recorded", "attempt_context_sealed"])
def test_crash_recovery_preserves_intention_and_never_redispatches(tmp_path, event_type):
    data, profiles = data_for(tmp_path / "vault")
    fired = []
    def crash(r, event):
        if event["event_type"] == event_type and not fired:
            fired.append(event)
            raise InjectedCrash()
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles), on_event=crash) as r:
        with pytest.raises(InjectedCrash):
            r.run()
    with make_runner(tmp_path / "store", data, resume=True, family_runtime=runtime(profiles)) as r:
        s = r.run()
        assert s.state == "succeeded"
        # A intenção existente só é reconciliada. No corte antes de dispatch,
        # not_found autoritativo permite outra tentativa, nunca reutilizar a 1.
        assert "attempt-T1-1" not in r.executor.dispatches
        assert len(events(r, "dispatch_requested")) == 2
        assert len({e["idempotency_key"] for e in events(r, "dispatch_requested")}) == 2


@pytest.mark.parametrize("message,code", [("network error: DNS unavailable", "network_unavailable"), ("Not logged in", "authentication_unavailable")])
def test_non_quota_unavailability_has_own_reason_without_reset(tmp_path, message, code):
    data, profiles = data_for(tmp_path / "vault")
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles, message=message)) as r:
        assert r.run().state == "succeeded"
        assert not events(r, "quota_exhausted_recorded") and not events(r, "quota_reset_observed")
        decision = events(r, "routing_decision_recorded")[-1]["payload"]
        blocked = next(c for c in decision["candidates"] if c["target_ref"]["id"] == "target-codex")
        assert any(reason["code"] == code for reason in blocked["reasons"])
        assert "reset_at" not in blocked


def test_Q25_invalid_choice_cannot_complete_attempt(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    data["document"]["attempts"] = {}
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles, choice="empty")) as r:
        assert r.run().state == "failed"
        assert all(a.state == "failed" for a in r.projection.attempts.values())
        assert not any(e["payload"]["manifest"] for e in events(r, "attempt_context_sealed"))


def test_Q02_invalid_profile_never_reaches_executor(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    profiles["sub-codex"]["oauth_token"] = "proibido"
    called = []
    rt = runtime(profiles)
    rt.executor_factory = lambda r: called.append(r)
    with pytest.raises(ExecutorError, match="subscription_policy_violation"):
        make_runner(tmp_path / "store", data, family_runtime=rt)
    assert not called


def test_Q04_agy_collect_error_finishes_attempt_failed(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    data["document"]["attempts"] = {}
    class EmptyAgent(SubscriptionAgent):
        def collect(self, intent):
            result = super().collect(intent)
            error = error_record(run_id=intent["run_id"], scope_type="attempt", scope_ref=intent["attempt_id"],
                phase="collect", category="provider", code="permission_or_empty_output", message="permission_or_empty_output",
                timestamp=result.usage[0]["recorded_at"], retryability="unknown", termination_certainty="confirmed")
            return replace(result, produced_files=(), errors=(error,))
    rt = runtime(profiles)
    rt.executor_factory = EmptyAgent
    with make_runner(tmp_path / "store", data, family_runtime=rt) as r:
        assert r.run().state == "failed"
        assert r.projection.latest_attempt("T1").state == "failed"
        assert not any(a.state == "completed" for a in r.projection.attempts.values())


def routing_data(tmp_path, category):
    data, _ = data_for(tmp_path / "vault")
    task = deepcopy(data["workflow"]["tasks"][0])
    task["task_type"] = category
    for target in data["catalog"]["targets"]:
        target["supported_task_types"].append(category) if category not in target["supported_task_types"] else None
    data["catalog"]["catalog_hash"] = canonical_digest({k: v for k, v in data["catalog"].items() if k != "catalog_hash"})
    request = load_fixture("routing-request.json")
    request.update(task_snapshot=task, catalog_ref=reference(data["catalog"], "catalog_id", "catalog_hash"),
                   routing_policy_ref=reference(data["policy"]["routing_policy"], "routing_policy_id"))
    return request, data["catalog"], data["policy"]["routing_policy"]


@pytest.mark.parametrize("category,family", [("architecture", "claude"), ("specification", "claude"),
    ("implementation", "codex"), ("testing", "codex"), ("review", "kiro"), ("documentation", "gemini")])
def test_owner_rules_take_precedence_over_catalog_priority(tmp_path, category, family):
    request, catalog, policy = routing_data(tmp_path, category)
    decision = route_and_finalize(request, catalog, policy,
        preflight_checks=[{"control": "quota", "status": "passed", "observed_at": request["created_at"], "evidence_refs": []}])
    assert decision["effective_selection"]["selected_target_id"] == "target-" + family


def test_review_cannot_use_implementation_family(tmp_path):
    request, catalog, policy = routing_data(tmp_path, "review")
    decision = route_and_finalize(request, catalog, policy, implementation_families=frozenset({"family_kiro"}),
        preflight_checks=[{"control": "quota", "status": "passed", "observed_at": request["created_at"], "evidence_refs": []}])
    assert decision["effective_selection"]["selected_target_id"] == "target-gemini"
    assert any(reason["code"] == "review_family_conflict" for c in decision["candidates"] for reason in c["reasons"])


def test_fixed_router_quota_fallback_uses_another_family(tmp_path):
    request, catalog, policy = routing_data(tmp_path, "implementation")
    policy.update(mode="fixed", fixed_target_id="target-codex", fallback_target_id="target-claude")
    request["routing_policy_ref"] = reference(policy, "routing_policy_id")
    quota = load_fixture("valid/quota-state-record.json")
    quota["quota_pool_id"] = "pool-codex"
    quota = seal_record(quota, "quota_state_record")
    decision = route_and_finalize(request, catalog, policy, quota_snapshot=QuotaStateSnapshot({"pool-codex": quota}),
        preflight_checks=[{"control": "quota", "status": "passed", "observed_at": request["created_at"], "evidence_refs": []}])
    assert decision["effective_selection"]["selected_target_id"] == "target-claude"
    assert decision["effective_selection"]["rule"] == "quota_fallback"


def test_rules_never_falls_back_to_another_pool_of_same_family(tmp_path):
    request, catalog, policy = routing_data(tmp_path, "implementation")
    first = catalog["targets"][0]
    alternate = deepcopy(first)
    alternate.update(target_id="target-codex-alternate", quota_pool_id="pool-codex-alternate", priority=99)
    catalog["targets"] = [first, alternate]
    catalog["catalog_hash"] = canonical_digest({k: v for k, v in catalog.items() if k != "catalog_hash"})
    request["catalog_ref"] = reference(catalog, "catalog_id", "catalog_hash")
    quota = load_fixture("valid/quota-state-record.json")
    quota["quota_pool_id"] = "pool-codex"
    quota = seal_record(quota, "quota_state_record")
    decision = route_and_finalize(request, catalog, policy, quota_snapshot=QuotaStateSnapshot({"pool-codex": quota}),
        preflight_checks=[{"control": "quota", "status": "passed", "observed_at": request["created_at"], "evidence_refs": []}])
    assert decision["decision_status"] == "no_eligible_target" and decision["effective_selection"] is None


def test_subscription_hub_integrates_runner_with_target_adapters(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    rt = runtime(profiles)
    def factory(r):
        agent = SubscriptionAgent(r)
        return SubscriptionHub(r, {t["target_id"]: agent for t in r.catalog["targets"]})
    rt.executor_factory = factory
    with make_runner(tmp_path / "store", data, family_runtime=rt) as r:
        assert r.run().state == "succeeded"
        assert r.projection.manifest["executor_snapshot"]["executor_ref"]["id"] == "executor-subscription-hub"
        assert len(r.projection.manifest["executor_snapshot"]["target_executor_refs"]) == 1


@pytest.mark.parametrize("cut", ["quota_probe_requested", "quota_probe_observed"])
def test_probe_crash_never_repeats_external_intent(tmp_path, cut):
    data, profiles = data_for(tmp_path / "vault")
    add_second_task(data)
    calls, fired = [], []
    def probe(target, request):
        calls.append(request["probe_id"])
        return {"available": True}
    def hook(r, event):
        if event["event_type"] == "outputs_accepted" and event["aggregate_id"] == "T1":
            r.clock.advance_to(format_utc(parse_utc(r.clock.now()) + timedelta(minutes=61)))
        if event["event_type"] == cut and not fired:
            fired.append(event)
            raise InjectedCrash()
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles, message="usage limit", probe=probe), on_event=hook) as r:
        with pytest.raises(InjectedCrash):
            r.run()
    with make_runner(tmp_path / "store", data, resume=True, family_runtime=runtime(profiles, message="usage limit", probe=probe)) as r:
        assert r.run().state == "succeeded"
        assert len(calls) == (1 if cut == "quota_probe_observed" else 0)
        assert len(events(r, "quota_probe_requested")) == 1


def test_family_preflight_honors_run_revocation_before_dispatch(tmp_path):
    data, profiles = data_for(tmp_path / "vault")
    data["document"]["attempts"] = {}
    source = next(e["source_metadata"] for e in data["knowledge_catalog"]["source_entries"] if e["source_metadata"]["source_id"] == "codex-only")
    assertion = deepcopy(next(a for a in data["knowledge_catalog"]["status_assertions"] if a["source_ref"]["source_id"] == "codex-only"))
    assertion.update(assertion_id="revoked-before-dispatch", status="revoked", effective_at="2026-11-14T20:00:00Z")
    assertion["assertion_digest"] = canonical_digest({k: v for k, v in assertion.items() if k != "assertion_digest"})
    data["policy"]["knowledge_status_assertions"] = [assertion]
    data["workflow"]["tasks"][0]["knowledge_requirements"][0]["source_refs"] = [
        {"id": source["source_id"], "version": source["revision"], "content_digest": source["content_digest"]}]
    with make_runner(tmp_path / "store", data, family_runtime=runtime(profiles)) as r:
        assert r.run().state == "failed"
        assert not events(r, "dispatch_requested")
        assert events(r, "admission_rejected")[0]["payload"]["reason"] == "knowledge_unsatisfied"
