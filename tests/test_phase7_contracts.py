"""Gate 7A: contratos; não executa CLIs, Router por cota, RLS ou MCP."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from orq_contracts.canonical import canonical_digest
from orq_contracts.schemas import load_schemas, validator_for
from orq_contracts.validator import (
    DEFAULT_SUBSCRIPTION_COOLDOWN_MS,
    validate_instance,
    validate_quota_consumption_record,
    validate_routing_decision,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures/v0.1"
NEW_SCHEMAS = (
    "agent_family", "audience_metadata", "subscription_profile",
    "quota_state_record", "quota_consumption_record",
)
# AgentFamily e AudienceMetadata são metadados; o digest pertence ao contêiner.
VALID = {
    "agent-family": "agent_family",
    "audience-metadata": "audience_metadata",
    "audience-metadata-common": "audience_metadata",
    "subscription-profile": "subscription_profile",
    "quota-state-record": "quota_state_record",
    "quota-state-record-cooldown": "quota_state_record",
    "quota-state-record-reset": "quota_state_record",
    "quota-consumption-record": "quota_consumption_record",
    "usage-record-subscription": "usage_record",
    "error-record-quota": "error_record",
    "journal-event-quota-exhausted": "journal_event",
    "journal-event-quota-reset": "journal_event",
    "journal-event-quota-consumption": "journal_event",
    "journal-event-subscription-reservation": "journal_event",
    "routing-policy-subscription": "routing_policy",
    "routing-decision-quota-fallback": "routing_decision",
    "routing-decision-quota-exhausted": "routing_decision",
}
PINNED_DIGESTS = {
    "agent-family": "012d9880dbe6bac11aa42b2f81c917dfbfcb111b804c2fedb9ad313204d7791d",
    "audience-metadata": "1c894a3ec69dc07ea9b926fdc79e020ca6e1f9d13802a912eea26bd41c966b77",
    "audience-metadata-common": "b3bed0672910ae1ce24d3b014671c1670771342200a2659517183cfb90155d50",
    "subscription-profile": "865881f397cba466443be52185d951c4c6621f469e548494fdab3bd4567e08a7",
    "quota-state-record": "172ec317c06c5616c081f4b8cdc601d566c4d2843f8a5622a437f01ae538cec5",
    "quota-state-record-cooldown": "736640b3bea9ee0db79094ed110fdf100a444d6130e7dced83ca907e61d8294e",
    "quota-state-record-reset": "6d2016eea3ef27ee82098e3cc56e609ca9f7c0f332da3dbd894bb448f4693c53",
    "quota-consumption-record": "6a468f90f896459f795069c169a58aad1f98c55f080966262840044f4f06f274",
    "usage-record-subscription": "05eec761e21bfb6dbaf6f6f68e9b2d5ddde616cbdb7b96536b6978a9c9429664",
    "error-record-quota": "07f3b75c967d37f136f3afa861d02e38d481df57d8f2970fd4df10a5c28449e1",
    "journal-event-quota-exhausted": "17d459e9587d1ee5314ad2b75498d691610bc2a86f3465608c8bb493ca6e887f",
    "journal-event-quota-reset": "1bedda6fbefced3f2acf335b45ff50b2e3d01726844cdb41e368b84eb610cd1d",
    "journal-event-quota-consumption": "63eadcca642355940cafae00d0fc62819926d0b643681502f5550f55b365b0f1",
    "journal-event-subscription-reservation": "756e6de8ff84ed9cfe16b8b01f064d0d346d1b9650b61705b9541da5d6d9451e",
    "routing-policy-subscription": "f44ec1b9c9959f66cc9d2e5afbff6dca6a671882847729842d5687e739698218",
    "routing-decision-quota-fallback": "c98f032e921814b63952c8431abc71080e3424d0a0b1d5a7e13e0842e3fbd1df",
    "routing-decision-quota-exhausted": "0a6240ba0ca0cf4ead01ae6bf969295f8c44f9fefc321178ab94770f378742d5",
}


def load(name, directory="valid"):
    return json.loads((FIXTURES / directory / f"{name}.json").read_text(encoding="utf-8"))


def sign(record, field="record_hash"):
    record[field] = canonical_digest({key: value for key, value in record.items() if key != field})
    return record


def codes(record, schema):
    return {issue.code for issue in validate_instance(record, schema)}


def test_all_five_new_schemas_are_closed_draft_2020_12():
    # Seis contratos pedidos: áreas são expressas em AudienceMetadata.
    for name in NEW_SCHEMAS:
        schema = load_schemas()[name]
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False


@pytest.mark.parametrize("filename,schema", VALID.items())
def test_valid_fixtures_and_pinned_jcs(filename, schema):
    record = load(filename)
    assert validate_instance(record, schema) == []
    if schema == "routing_decision":
        assert validate_routing_decision(record) == []
    field = "event_hash" if schema == "journal_event" else "record_hash"
    unsigned = {key: value for key, value in record.items() if key != field}
    digest = canonical_digest(unsigned)
    assert digest["value"] == PINNED_DIGESTS[filename]
    if field in record:
        assert record[field] == digest


@pytest.mark.parametrize("schema", NEW_SCHEMAS)
def test_required_fields_and_unknown_fields(schema):
    filename = next(name for name, kind in VALID.items() if kind == schema)
    record = load(filename)
    for field in load_schemas()[schema]["required"]:
        incomplete = copy.deepcopy(record)
        del incomplete[field]
        assert "schema.required" in codes(incomplete, schema)
    record["unexpected"] = True
    assert "schema.additionalProperties" in codes(record, schema)


@pytest.mark.parametrize("filename,schema,code,repair", [
    ("agent-family-composite", "agent_family", "schema.enum", {"family_id": "family_codex"}),
    ("audience-metadata-common-restricted", "audience_metadata", "schema.maxItems", {"area_scope": "family"}),
    ("subscription-profile-credential", "subscription_profile", "subscription_policy_violation", {"oauth_token": None}),
    ("quota-state-record-wrong-reset", "quota_state_record", "schema.const", {"reset_source": "cli_reported"}),
    ("quota-state-record-short-cooldown", "quota_state_record", "semantic.quota_cooldown_mismatch", {"blocked_until": "2026-11-15T00:15:00Z"}),
    ("quota-state-record-no-probe", "quota_state_record", "schema.minItems", {"probe_evidence_refs": [{"artifact_id": "probe", "content_digest": canonical_digest("probe") }]}),
    ("quota-consumption-record-negative", "quota_consumption_record", "schema.minimum", {"messages_consumed": 1}),
    ("usage-record-subscription-zero", "usage_record", "invalid_subscription_cost_zero", {"cost_lines": load("usage-record-subscription")["cost_lines"]}),
    ("error-record-quota-unknown-termination", "error_record", "schema.const", {"termination_certainty": "confirmed"}),
    ("journal-event-quota-wrong-aggregate", "journal_event", "schema.enum", {"aggregate_type": "run"}),
])
def test_invalid_fixtures_have_one_defect_and_valid_hashes(filename, schema, code, repair):
    record = load(filename, "invalid")
    assert code in codes(record, schema)
    field = "event_hash" if schema == "journal_event" else "record_hash"
    if field in record:
        assert record[field] == canonical_digest({k: v for k, v in record.items() if k != field})
    for key, value in repair.items():
        if key == "oauth_token":
            del record[key]
        else:
            record[key] = value
    if field in record:
        sign(record, field)
    assert validate_instance(record, schema) == []


@pytest.mark.parametrize("family", ["claude", "codex", "gemini", "kiro", "simulated"])
@pytest.mark.parametrize("role", ["backend_engineer", "software_architect", "security_reviewer"])
def test_family_axes_are_independent(family, role):
    record = load("agent-family")
    record.update(family_id=f"family_{family}", role_id=role)
    assert validate_instance(record, "agent_family") == []


@pytest.mark.parametrize("families,roles,valid", [
    ([], [], False), (["family_codex"], [], True), ([], ["tester"], True),
    (["family_claude"], ["security_reviewer"], True),
    (["family_claude", "family_claude"], [], False),
    ([], ["tester", "tester"], False), (["codex:tester"], [], False),
])
def test_family_area_requires_at_least_one_axis(families, roles, valid):
    record = load("audience-metadata")
    record.update(target_families=families, target_roles=roles)
    assert (validate_instance(record, "audience_metadata") == []) is valid


@pytest.mark.parametrize("field", ["target_families", "target_roles"])
def test_common_area_cannot_restrict_either_axis(field):
    record = load("audience-metadata-common")
    record[field] = ["family_codex" if field == "target_families" else "tester"]
    assert "schema.maxItems" in codes(record, "audience_metadata")


@pytest.mark.parametrize("field", ["oauth_token", "session_cookie", "api_key", "web_url"])
def test_q02_profile_rejects_credentials_and_web_configuration(field):
    record = load("subscription-profile")
    record[field] = "example-forbidden-value"
    assert "subscription_policy_violation" in codes(sign(record), "subscription_profile")


@pytest.mark.parametrize("family,cli", [("claude", "claude"), ("codex", "codex"), ("gemini", "agy"), ("kiro", "kiro-cli")])
def test_subscription_cli_must_match_the_family(family, cli):
    record = load("subscription-profile")
    record.update(family_id=f"family_{family}", cli_executable=cli)
    assert validate_instance(sign(record), "subscription_profile") == []
    record["cli_executable"] = "codex" if cli != "codex" else "claude"
    assert "subscription_policy_violation" in codes(sign(record), "subscription_profile")


def test_q07_error_and_quota_event_retain_reset_and_confirmed_termination():
    error = load("error-record-quota")
    state = error["quota_state"]
    assert state == load("journal-event-quota-exhausted")["payload"]
    assert state["reset_at"] == state["blocked_until"] == "2026-11-15T01:05:00Z"
    assert error["code"] == "quota_limit_reached"
    assert error["retryability"] == "retryable" and error["termination_certainty"] == "confirmed"
    state["blocked_until"] = "2026-11-15T01:06:00Z"
    sign(state)
    assert "semantic.quota_reset_mismatch" in codes(sign(error), "error_record")


@pytest.mark.parametrize("status,source", [("quota_exhausted", "cooldown_policy"), ("cooldown", "cli_reported"), ("available", "cli_reported")])
def test_reset_source_must_match_state(status, source):
    filename = {"quota_exhausted": "quota-state-record", "cooldown": "quota-state-record-cooldown", "available": "quota-state-record-reset"}[status]
    record = load(filename)
    record["reset_source"] = source
    assert "schema.const" in codes(sign(record), "quota_state_record")


@pytest.mark.parametrize("timestamp", ["2026-11-14T23:15:00Z", "2026-11-14T23:14:59Z"])
def test_quota_block_must_end_in_the_future(timestamp):
    record = load("quota-state-record")
    record.update(reset_at=timestamp, blocked_until=timestamp)
    assert "semantic.quota_block_not_future" in codes(sign(record), "quota_state_record")


def test_q09_q10_default_cooldown_override_and_light_probe():
    record = load("quota-state-record-cooldown")
    assert DEFAULT_SUBSCRIPTION_COOLDOWN_MS == 3_600_000
    assert record["reset_at"] is None and "default_cooldown_ms" not in record
    before = copy.deepcopy(record)
    assert validate_instance(record, "quota_state_record") == []
    assert record == before  # Não insere defaults nos bytes assinados.
    record.update(default_cooldown_ms=1_800_000, blocked_until="2026-11-14T23:45:00Z")
    assert validate_instance(sign(record), "quota_state_record") == []
    reset = load("quota-state-record-reset")
    assert reset["probe_evidence_refs"]
    reset["probe_evidence_refs"] = []
    assert "schema.minItems" in codes(sign(reset), "quota_state_record")
    reset["previous_status"] = "quota_exhausted"
    assert validate_instance(sign(reset), "quota_state_record") == []


@pytest.mark.parametrize("field,value", [("observed_at", "2026-02-30T12:00:00Z"), ("reset_at", "2026-11-15T01:05:00-03:00"), ("raw_message_excerpt", "x" * 513)])
def test_quota_timestamps_and_excerpt_are_bounded(field, value):
    record = load("quota-state-record")
    record[field] = value
    assert validate_instance(sign(record), "quota_state_record")


def test_fractional_timestamps_are_compared_without_truncation():
    record = load("quota-state-record-cooldown")
    record.update(observed_at="2026-11-14T23:15:00.000000000000000000001Z", blocked_until="2026-11-15T00:15:00.000000000000000000002Z")
    assert "semantic.quota_cooldown_mismatch" in codes(sign(record), "quota_state_record")


@pytest.mark.parametrize("filename,schema", [("subscription-profile", "subscription_profile"), ("quota-state-record", "quota_state_record"), ("quota-consumption-record", "quota_consumption_record")])
def test_signed_phase7_records_reject_stale_digests(filename, schema):
    record = load(filename)
    record["record_hash"]["value"] = "0" * 64
    assert "semantic.record_hash_mismatch" in codes(record, schema)


def test_q08_q12_routing_contracts_express_fallback_and_no_eligible_target():
    fallback = load("routing-decision-quota-fallback")
    assert fallback["effective_selection"]["rule"] == "quota_fallback"
    assert fallback["effective_selection"]["used_fallback"] is True
    fallback["effective_selection"]["used_fallback"] = False
    assert "semantic.invalid_quota_fallback" in codes(sign(fallback), "routing_decision")
    none = load("routing-decision-quota-exhausted")
    assert none["reason"]["code"] == "all_eligible_families_quota_exhausted"
    none["candidates"][0]["eligible"] = True
    assert "semantic.invalid_quota_rejection" in codes(sign(none), "routing_decision")


@pytest.mark.parametrize("outcome", ["accepted", "quota_exhausted_on_call", "unknown"])
def test_q13_quota_consumption_and_usage_resolve_without_inventing_cost(outcome):
    record, usage = load("quota-consumption-record"), load("usage-record-subscription")
    record["quota_outcome"] = outcome
    assert validate_quota_consumption_record(sign(record), usage_record=usage) == []
    assert usage["cost_lines"][0]["amount"] is None
    assert record["output_tokens"] is None and record["messages_consumed"] == 1
    record["usage_record_ref"]["content_digest"]["value"] = "0" * 64
    issues = validate_quota_consumption_record(sign(record), usage_record=usage)
    assert "semantic.quota_usage_ref_mismatch" in {issue.code for issue in issues}


@pytest.mark.parametrize("field,value", [("run_id", "other-run"), ("attempt_id", "other-attempt"), ("subscription_id", "other-sub")])
def test_quota_usage_binding_must_match(field, value):
    record = load("quota-consumption-record")
    record[field] = value
    issues = validate_quota_consumption_record(sign(record), usage_record=load("usage-record-subscription"))
    assert "semantic.quota_usage_scope_mismatch" in {issue.code for issue in issues}


@pytest.mark.parametrize("amount", ["0", "0.00"])
@pytest.mark.parametrize("filename,schema", [("usage-record-subscription", "usage_record"), ("journal-event-subscription-reservation", "journal_event")])
def test_q14_subscription_zero_is_rejected_with_specific_code(amount, filename, schema):
    record = load(filename)
    line = record["cost_lines"][0] if schema == "usage_record" else record["payload"]
    line["amount"] = amount
    assert "invalid_subscription_cost_zero" in codes(record, schema)


@pytest.mark.parametrize("status,amount,currency", [("reported", "1.2", "USD"), ("estimated", "0.01", "USD"), ("unavailable", None, "USD")])
def test_subscription_cost_cannot_reuse_cli_estimate_or_currency(status, amount, currency):
    record = load("usage-record-subscription")
    record["cost_lines"][0].update(status=status, amount=amount, currency=currency)
    assert validate_instance(sign(record), "usage_record")


@pytest.mark.parametrize("filename,allowed", [("journal-event-quota-exhausted", "run"), ("journal-event-quota-reset", "run"), ("journal-event-quota-consumption", "attempt")])
def test_quota_events_have_exact_payloads_and_aggregate_types(filename, allowed):
    record = load(filename)
    for aggregate in ["run", "task", "attempt", "evaluation"]:
        record["aggregate_type"] = aggregate
        assert (list(validator_for("journal_event").iter_errors(record)) == []) is (aggregate == allowed)
    record = load(filename)
    record["payload"]["unexpected"] = True
    assert "schema.additionalProperties" in codes(record, "journal_event")


def test_quota_event_payload_is_validated_semantically_and_bound_to_run():
    event = load("journal-event-quota-exhausted")
    event["payload"]["blocked_until"] = "2026-11-15T01:06:00Z"
    sign(event["payload"])
    issues = validate_instance(sign(event, "event_hash"), "journal_event")
    assert any(i.code == "semantic.quota_reset_mismatch" and i.path == "/payload/blocked_until" for i in issues)
    event = load("journal-event-quota-consumption")
    event["payload"]["run_id"] = "other-run"
    sign(event["payload"])
    assert "semantic.quota_run_mismatch" in codes(sign(event, "event_hash"), "journal_event")


def test_rules_priorities_remain_policy_data_and_review_requires_other_family():
    policy = load("routing-policy-subscription")
    rules = policy["subscription_rules"]
    assert rules["review_requires_different_family"] is True
    assert rules["family_order_by_task_type"]["implementation"] == ["family_codex", "family_claude"]
    assert rules["family_order_by_task_type"]["architecture"] == ["family_claude", "family_kiro"]
    rules["family_order_by_task_type"]["implementation"].reverse()
    assert "schema.const" in codes(policy, "routing_policy")


def test_audience_extensions_and_subscription_target_reuse_existing_contracts():
    catalog = json.loads((FIXTURES / "vault/catalog-snapshot.json").read_text(encoding="utf-8"))
    source = catalog["source_entries"][0]["source_metadata"]
    source["audience"] = load("audience-metadata-common")
    catalog["source_entries"][0]["units"][0]["audience"] = load("audience-metadata")
    validator_for("knowledge_catalog").validate(catalog)
    target_catalog = json.loads((FIXTURES / "routing-target-catalog.json").read_text(encoding="utf-8"))
    target = target_catalog["targets"][0]
    target.update(family_id="family_codex", subscription_id="sub-chatgpt-plus", quota_pool_id="pool-chatgpt-plus")
    validator_for("routing_target_catalog").validate(target_catalog)
    del target["quota_pool_id"]
    assert list(validator_for("routing_target_catalog").iter_errors(target_catalog))


def test_availability_unknown_is_not_a_fabricated_quota_event():
    schema = validator_for("agent_family").evolve(schema={"$ref": "common.schema.json#/$defs/quotaAvailability"})
    for status in ["available", "quota_exhausted", "cooldown", "unknown"]:
        schema.validate(status)
    record = load("quota-state-record")
    record["status"] = "unknown"
    assert "schema.enum" in codes(sign(record), "quota_state_record")
