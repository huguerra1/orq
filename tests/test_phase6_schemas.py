from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from orq_contracts.canonical import canonical_digest, canonicalize
from orq_contracts.planner import FixedTemplatePlanner, plan_and_validate
from orq_contracts.router import route_and_finalize
from orq_contracts.schemas import load_schemas, validator_for
from orq_contracts.vault import build_catalog_snapshot
from orq_contracts.validator import validate_instance
from test_context import materialize
from test_selection import select
from test_vault import versioned_ref


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "v0.1"
NAMES = (
    "attempt-header", "dispatch-intent", "error-record", "evaluation-report",
    "journal-event", "run-intent", "run-manifest", "usage-record",
)
CONTROLS = (
    "context_integrity", "knowledge_status", "attempts_remaining",
    "run_time_remaining", "budget", "inputs_accepted",
)
VOCABULARY = {
    "run": (
        "run_intent_recorded", "planning_recorded", "run_manifest_closed", "state_transition",
        "cancel_requested", "deadline_exceeded", "budget_reserved", "budget_settled",
        "budget_released", "usage_attached", "error_attached", "journal_tail_discarded", "run_closed",
        "artifact_ingested",
    ),
    "task": (
        "state_transition", "knowledge_selection_recorded", "routing_decision_recorded",
        "context_manifest_recorded", "admission_rejected", "outputs_accepted", "error_attached",
    ),
    "attempt": (
        "dispatch_requested", "dispatch_acknowledged", "execution_started", "deadline_exceeded",
        "cancel_requested", "termination_confirmed", "reconciliation_requested",
        "reconciliation_observed", "result_collected", "artifact_ingested", "output_bound",
        "usage_attached", "error_attached", "state_transition", "late_observation",
    ),
    "evaluation": (
        "evaluation_requested", "evaluation_report_recorded", "late_observation",
        "artifact_ingested", "error_attached",
    ),
}
EVENT_CASES = [(aggregate, event) for aggregate, events in VOCABULARY.items() for event in events]
FORBIDDEN_EVENT_CASES = [
    (aggregate, event)
    for aggregate, allowed in VOCABULARY.items()
    for event in sorted({event for events in VOCABULARY.values() for event in events} - set(allowed))
]
GOLDEN_HASHES = {
    "attempt-header": "3ac77f0d99551b17f2b667b55b8b2edfe70b06dd1fc25447d1425284c1636888",
    "dispatch-intent": "4812be3b3cf4d88fdd573ec9d40d8de7968326f4166b2b9bdb484b732596f2f0",
    "error-record": "5db231aa971cbf80d8419885983881578d266679b59a20cf2b468e2fe53f11f9",
    "evaluation-report": "ad0cd87286f74f520e314dcd2c1ff662acb54c3c69d2852cfa55848e4decc7ca",
    "evaluation-report-workflow": "59dcea5f0e6c8717370e752735d639e5c223d6ba1ac1a322292b48821f7dac79",
    "journal-event": "07794daaf5330e09ed01c962aae32dc86621c709a50d1c8481925e28b496735b",
    "journal-event-first": "716bbc514f39fda354aaa2adc30bd7c1fb61ec9cb702f8d4437458b73fa5e4fd",
    "run-intent": "11148c4446bc476f79ea355bbf03b69ed1dc441680f79d693c7137f20326c198",
    "run-manifest": "583aa8928a9c400a85aa126126ee247be62d1caf098293039ed91849a3e966cd",
    "run-manifest-cancelled-before-ready": "97d6f7b785ce8ffd42f2bbc579666404b0a9ff22f08fdfc9c713eafc786524a9",
    "run-manifest-planning-failed": "e7bef5fdb126d5ba241fc28cc8ce4d58ec11f990e33ee86aa997ff7119fe3a1d",
    "usage-record": "2c887aaf6e8f5e1111eef48326dea70d16b6539a830b764d3f41d7ba44bc8170",
    "usage-record-estimated": "2745d67ddacaccb8527df5c6969306386c054d6533381372b3e8738b1b054966",
    "usage-record-reported": "3ca61baaaa01aa2eae91f7f83eb9c20db57262c4dc24298df7c62295b764d911",
}


def load(name: str, directory: str = "valid") -> dict:
    return json.loads((FIXTURES / directory / f"{name}.json").read_text(encoding="utf-8"))


def errors_for(name: str, value: dict) -> list:
    """Include precise branch errors beneath oneOf/contains failures."""
    def leaves(errors):
        for error in errors:
            yield error
            yield from leaves(error.context)

    return list(leaves(validator_for(name.replace("-", "_")).iter_errors(value)))


def assert_error(name: str, value: dict, path: tuple, keyword: str) -> None:
    errors = errors_for(name, value)
    assert any(tuple(error.absolute_path) == path and error.validator == keyword for error in errors), [
        (list(error.absolute_path), error.validator, error.message) for error in errors
    ]


@pytest.mark.parametrize("name", NAMES)
def test_phase6_meta_schemas_are_draft_2020_12_and_closed(name) -> None:
    schema = load_schemas()[name.replace("-", "_")]
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("filename", GOLDEN_HASHES)
def test_valid_phase6_fixtures(filename) -> None:
    name = next(name for name in NAMES if filename == name or filename.startswith(name + "-"))
    validator_for(name.replace("-", "_")).validate(load(filename))


@pytest.mark.parametrize(("filename", "name", "path", "keyword"), [
    ("run-intent-unknown-field", "run-intent", ("requested_by",), "additionalProperties"),
    ("run-manifest-ready-null-limits", "run-manifest", ("resolved_limits",), "not"),
    ("dispatch-intent-unpassed-check", "dispatch-intent", ("admission_checks", 0, "status"), "const"),
    ("attempt-header-first-with-previous", "attempt-header", ("previous_attempt_id",), "type"),
    ("evaluation-report-context", "evaluation-report", ("context_manifest_ref",), "type"),
    ("usage-record-unavailable-zero", "usage-record", ("cost_lines", 0, "amount"), "type"),
    ("error-record-invalid-certainty", "error-record", ("termination_certainty",), "enum"),
    ("journal-event-wrong-payload", "journal-event", ("payload",), "required"),
])
def test_invalid_fixtures_fail_for_the_documented_reason(filename, name, path, keyword) -> None:
    record = load(filename, "invalid")
    assert_error(name, record, path, keyword)
    field = "event_hash" if name == "journal-event" else "record_hash"
    assert record[field] == canonical_digest({key: value for key, value in record.items() if key != field})
    if name == "attempt-header":
        assert record["attempt_id"] == f"attempt-{record['task_id']}-{record['attempt_number']}"


@pytest.mark.parametrize("name", NAMES)
def test_every_required_top_level_field_is_required_and_extra_fields_are_rejected(name) -> None:
    record = load(name)
    for field in validator_for(name.replace("-", "_")).schema["required"]:
        incomplete = copy.deepcopy(record)
        del incomplete[field]
        assert_error(name, incomplete, (), "required")
    record["unexpected"] = True
    assert_error(name, record, (), "additionalProperties")


@pytest.mark.parametrize("control", CONTROLS)
def test_admission_requires_each_control_once(control) -> None:
    intent = load("dispatch-intent")
    intent["admission_checks"] = [check for check in intent["admission_checks"] if check["control"] != control]
    assert_error("dispatch-intent", intent, ("admission_checks",), "contains")
    intent = load("dispatch-intent")
    intent["admission_checks"].append(next(check for check in intent["admission_checks"] if check["control"] == control))
    assert_error("dispatch-intent", intent, ("admission_checks",), "maxContains")


@pytest.mark.parametrize("field", [
    "planning_record_ref", "workflow_snapshot_ref", "execution_policy_snapshot_ref", "resolved_limits",
    "budget_snapshot", "routing_catalog_ref", "routing_policy_ref", "knowledge_catalog_snapshot_ref",
    "executor_snapshot", "evaluator_snapshot", "scheduling_policy",
])
def test_ready_manifest_cannot_have_unresolved_fields(field) -> None:
    manifest = load("run-manifest")
    manifest[field] = None
    assert_error("run-manifest", manifest, (field,), "type" if field.endswith("_ref") else "not")
    for filename in ["run-manifest-planning-failed", "run-manifest-cancelled-before-ready"]:
        manifest = load(filename)
        assert manifest[field] is None
        validator_for("run_manifest").validate(manifest)


@pytest.mark.parametrize("field", [
    "max_attempts", "attempt_timeout_ms", "max_run_duration_ms", "max_evaluations_per_attempt",
    "evaluation_timeout_ms", "reconciliation_timeout_ms",
])
def test_resolved_limits_are_positive_integers(field) -> None:
    manifest = load("run-manifest")
    manifest["resolved_limits"][field] = 0
    assert_error("run-manifest", manifest, ("resolved_limits", field), "minimum")


def test_retry_policy_and_budget_are_explicit_and_closed() -> None:
    manifest = load("run-manifest")
    manifest["resolved_limits"]["max_concurrency"] = 2
    assert_error("run-manifest", manifest, ("resolved_limits", "max_concurrency"), "const")
    manifest = load("run-manifest")
    del manifest["resolved_limits"]["retry_policy"]["on_confirmed_timeout"]
    assert_error("run-manifest", manifest, ("resolved_limits", "retry_policy"), "required")
    manifest = load("run-manifest")
    manifest["resolved_limits"]["retry_policy"]["on_evaluation_fail"] = "true"
    assert_error("run-manifest", manifest, ("resolved_limits", "retry_policy", "on_evaluation_fail"), "type")
    manifest = load("run-manifest")
    manifest["budget_snapshot"]["max_cost"] = None
    assert_error("run-manifest", manifest, ("budget_snapshot", "max_cost"), "type")
    manifest["budget_snapshot"]["cap_mandatory"] = False
    validator_for("run_manifest").validate(manifest)


def test_knowledge_assertions_reuse_catalog_contract() -> None:
    manifest = load("run-manifest")
    assertion = {
        "assertion_id": "status-revoked", "source_ref": {
            "source_id": "source-demo", "revision": "1", "content_digest": canonical_digest("source"),
        }, "status": "revoked", "effective_at": manifest["closed_at"],
        "issuer_ref": "owner", "evidence_ref": "review-1",
    }
    assertion["assertion_digest"] = canonical_digest(assertion)
    manifest["knowledge_status_assertions"] = [assertion]
    validator_for("run_manifest").validate(manifest)
    manifest["knowledge_status_assertions"][0]["status"] = "unknown"
    assert_error("run-manifest", manifest, ("knowledge_status_assertions", 0, "status"), "enum")


@pytest.mark.parametrize("amount", [0, -1, "-0.01", "1e2", "NaN", "01.20", ".2", ""])
def test_money_requires_nonnegative_decimal_text(amount) -> None:
    usage = load("usage-record-reported")
    usage["cost_lines"][0]["amount"] = amount
    assert_error("usage-record", usage, ("cost_lines", 0, "amount"), "type" if isinstance(amount, int) else "pattern")


@pytest.mark.parametrize("status", ["reported", "estimated"])
def test_known_cost_cannot_be_null(status) -> None:
    usage = load("usage-record")
    usage["cost_lines"][0]["status"] = status
    assert_error("usage-record", usage, ("cost_lines", 0, "amount"), "type")


def test_measurements_preserve_unknown_quantity() -> None:
    usage = load("usage-record")
    usage["measurements"] = [{
        "metric_id": "input_tokens", "quantity": None, "unit": "tokens",
        "status": "unavailable", "source_ref": None,
    }]
    validator_for("usage_record").validate(usage)
    usage["measurements"][0]["quantity"] = "0"
    assert_error("usage-record", usage, ("measurements", 0, "quantity"), "type")


def test_evaluation_is_local_and_keeps_unknown_methods_without_approving_them() -> None:
    report = load("evaluation-report")
    report["patch_application_refs"] = [versioned_ref("patch")]
    assert_error("evaluation-report", report, ("patch_application_refs",), "maxItems")
    report = load("evaluation-report")
    report["usage_record_ids"] = ["usage-invented-zero"]
    assert_error("evaluation-report", report, ("usage_record_ids",), "maxItems")
    report = load("evaluation-report")
    report["criteria_snapshot"]["criteria"][0]["pass_rule"]["unexpected"] = True
    assert_error("evaluation-report", report, ("criteria_snapshot", "criteria", 0, "pass_rule"), "additionalProperties")
    report = load("evaluation-report")
    report["criteria_snapshot"]["criteria"][0]["method"] = "future_verifier"
    report["criterion_results"][0].update(method="future_verifier", status="not_run")
    validator_for("evaluation_report").validate(report)
    report["criterion_results"][0]["status"] = "pass"
    assert_error("evaluation-report", report, ("criterion_results", 0, "status"), "const")


def test_pass_requires_completed_execution_and_passing_required_results() -> None:
    report = load("evaluation-report")
    report["verdict"] = "pass"
    assert_error("evaluation-report", report, ("criterion_results", 1, "status"), "const")
    report["criterion_results"][1]["status"] = "pass"
    validator_for("evaluation_report").validate(report)
    report["execution_status"] = "indeterminate"
    assert_error("evaluation-report", report, ("execution_status",), "const")


@pytest.fixture(scope="module")
def payloads() -> dict:
    """All payloads use explicit examples; earlier contracts use their existing APIs."""
    workflow = load("workflow-minimal")
    request = json.loads((FIXTURES / "planning-request.json").read_text(encoding="utf-8"))
    planning = plan_and_validate(
        FixedTemplatePlanner([workflow], versioned_ref("planner")), request,
        completed_at="2026-09-29T13:00:01Z",
    ).record
    request = json.loads((FIXTURES / "routing-request.json").read_text(encoding="utf-8"))
    catalog = json.loads((FIXTURES / "routing-target-catalog.json").read_text(encoding="utf-8"))
    policy = json.loads((FIXTURES / "routing-policy-rules.json").read_text(encoding="utf-8"))
    routing = route_and_finalize(request, catalog, policy, preflight_checks=[])
    vault = FIXTURES / "vault"
    catalog = build_catalog_snapshot(vault, json.loads((vault / "catalog-definition.json").read_text(encoding="utf-8")))
    request = json.loads((vault / "selection-request.json").read_text(encoding="utf-8"))
    selection = select(vault, catalog, request.pop("requirements"), **request)
    context = materialize(vault, catalog, selection).manifest
    report, intent = load("evaluation-report"), load("dispatch-intent")
    stamp = intent["created_at"]
    artifact = report["subject_bindings"][0]["artifact"]
    summary = {
        "state": "failed", "tasks": [{"task_id": "T3", "state": "failed"}], "output_bindings": [],
        "usage_record_ids": ["usage-T3-2"], "error_record_ids": [],
        "cost_summary": {"currency": "USD", "reported": "0", "estimated": "0", "unavailable_count": 1, "total_cost_exact": False},
        "limitations": [], "unknowns": ["cost"],
    }
    return {
        "run_intent_recorded": load("run-intent"), "planning_recorded": planning,
        "run_manifest_closed": load("run-manifest"), "cancel_requested": {"reason": "run_cancelled"},
        "deadline_exceeded": {"deadline_at": stamp, "observed_at": stamp},
        "budget_reserved": {"reservation_id": "reservation-attempt-T3-2", "scope_type": "attempt", "scope_ref": "attempt-T3-2", "amount": "0.30", "status": "estimated", "currency": "USD", "method": "fixture_ceiling"},
        "budget_settled": {"reservation_id": "reservation-attempt-T3-2", "usage_ids": ["usage-T3-2"]},
        "budget_released": {"reservation_id": "reservation-attempt-T3-2", "reason": "admission_aborted"},
        "usage_attached": load("usage-record"), "error_attached": load("error-record"),
        "journal_tail_discarded": {"discarded_bytes": 8, "discarded_digest": canonical_digest("tail")},
        "run_closed": {"as_of_global_sequence": 57, "event_hash": load("journal-event")["previous_event_hash"], "run_summary": summary, "summary_hash": canonical_digest(summary)},
        "knowledge_selection_recorded": selection, "routing_decision_recorded": routing,
        "context_manifest_recorded": context, "admission_rejected": {"reason": "knowledge_revoked", "error_record_ids": []},
        "outputs_accepted": {"task_id": "T3", "evaluation_id": report["evaluation_id"], "output_bindings": report["subject_bindings"]},
        "dispatch_requested": {"attempt_header": load("attempt-header"), "dispatch_intent": intent},
        "dispatch_acknowledged": {"external_call_id": None}, "execution_started": {"observed_at": stamp},
        "termination_confirmed": {"cause": "finished", "observed_at": stamp},
        "reconciliation_requested": {"dispatch_idempotency_key": intent["dispatch_idempotency_key"], "external_call_id": None, "deadline_at": stamp},
        "reconciliation_observed": {"outcome": "found_stopped", "external_call_id": None, "evidence_refs": []},
        "result_collected": {"requested_model_id": None, "resolved_model_id": "sim-model-1", "provider_context_coverage": "platform_only"},
        "artifact_ingested": {
            "schema_version": "0.1", **artifact, "artifact_type": "report", "media_type": "application/json",
            "content_contract_ref": versioned_ref("report"), "size_bytes": 8388608,
            "locator": "orq-store:sha256/" + artifact["content_digest"]["value"], "created_at": stamp,
            "producer": {"kind": "attempt_output", "run_id": intent["run_id"], "task_id": "T3", "attempt_id": "attempt-T3-2"},
            "derived_from": [], "data_classification": "internal",
        },
        "output_bound": report["subject_bindings"][0],
        "late_observation": {"observation_type": "termination", "observed_at": stamp, "evidence_refs": [artifact]},
        "evaluation_requested": {
            **{key: report[key] for key in ["evaluation_id", "evaluation_number", "scope", "subject_bindings", "criteria_snapshot", "evaluator_snapshot"]},
            "deadline_at": stamp, "budget_reservation_ids": [],
        },
        "evaluation_report_recorded": report,
    }


def event_with(aggregate: str, event_type: str, payloads: dict) -> dict:
    event = load("journal-event")
    payload = {"from": "running", "to": "failed", "reason": "execution_failed", "evidence_event_ids": ["event-57"]} if event_type == "state_transition" else payloads[event_type]
    event.update(aggregate_type=aggregate, event_type=event_type, payload=copy.deepcopy(payload))
    if event_type == "artifact_ingested":
        if aggregate == "run":
            event["payload"]["producer"] = {
                "kind": "external_input", "run_id": event["run_id"], "input_id": "spec",
                "source_ref": versioned_ref("external-spec"),
            }
        elif aggregate == "evaluation":
            event["payload"]["producer"] = {
                "kind": "evaluation_output", "evaluation_id": "evaluation-T3-1-1", "purpose": "diagnostic",
            }
    if event_type == "error_attached":
        event["payload"].update(scope_type=aggregate, scope_ref={
            "run": event["run_id"], "task": "T3", "attempt": "attempt-T3-2", "evaluation": "evaluation-T3-1-1",
        }[aggregate])
    return event


@pytest.mark.parametrize(("aggregate", "event_type"), EVENT_CASES)
def test_each_event_has_a_valid_closed_payload(aggregate, event_type, payloads) -> None:
    event = event_with(aggregate, event_type, payloads)
    validator_for("journal_event").validate(event)
    for field in list(event["payload"]):
        if field in {"metadata", "provider_extension"}:  # explicitly optional baseline fields
            continue
        incomplete = copy.deepcopy(event)
        del incomplete["payload"][field]
        assert_error("journal-event", incomplete, ("payload",), "required")
    event["payload"]["unexpected"] = "rejected"
    assert_error("journal-event", event, ("payload",), "additionalProperties")


def test_journal_vocabulary_matches_the_accepted_contract() -> None:
    schema = validator_for("journal_event").schema
    assert set(schema["properties"]["aggregate_type"]["enum"]) == set(VOCABULARY)
    assert set(schema["properties"]["event_type"]["enum"]) == {
        event for events in VOCABULARY.values() for event in events
    }
    contract = (ROOT / "docs/phase-6/run-contracts.md").read_text(encoding="utf-8")
    for aggregate, events in VOCABULARY.items():
        if aggregate == "attempt":
            section = contract.split("### Eventos da tentativa\n", 1)[1].split("### Projeção AttemptState", 1)[0]
            documented = re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE)
        else:
            row = re.search(rf"^\| {aggregate} \| (.+) \|$", contract, re.MULTILINE)
            assert row is not None
            documented = re.findall(r"`([^`]+)`", row[1])
        assert set(documented) == set(events)
        rules = [
            rule["then"]["properties"]["event_type"]["enum"]
            for rule in schema["allOf"]
            if rule.get("if", {}).get("properties", {}).get("aggregate_type", {}).get("const") == aggregate
            and "event_type" in rule.get("then", {}).get("properties", {})
        ]
        assert len(rules) == 1 and set(rules[0]) == set(events)


@pytest.mark.parametrize(("aggregate", "event_type"), FORBIDDEN_EVENT_CASES)
def test_event_cannot_be_attached_to_the_wrong_aggregate(aggregate, event_type, payloads) -> None:
    assert_error("journal-event", event_with(aggregate, event_type, payloads), ("event_type",), "enum")


@pytest.mark.parametrize(("aggregate", "foreign_state"), [("run", "queued"), ("task", "created"), ("attempt", "pending")])
@pytest.mark.parametrize("field", ["from", "to"])
def test_transition_states_belong_to_their_aggregate(aggregate, foreign_state, field, payloads) -> None:
    event = event_with(aggregate, "state_transition", payloads)
    event["payload"][field] = foreign_state
    assert_error("journal-event", event, ("payload", field), "enum")
    event["payload"][field] = None  # creation is implicit in the initial record, never a transition
    assert_error("journal-event", event, ("payload", field), "enum")


@pytest.mark.parametrize("scope", ["attempt", "evaluation"])
@pytest.mark.parametrize(("status", "amount"), [("estimated", "0.30"), ("unavailable", None)])
def test_reservations_preserve_unknown_amounts(scope, status, amount, payloads) -> None:
    event = event_with("run", "budget_reserved", payloads)
    event["payload"].update(scope_type=scope, scope_ref=f"{scope}-T3-2", status=status, amount=amount)
    validator_for("journal_event").validate(event)
    event["payload"]["amount"] = "0" if status == "unavailable" else None
    assert_error("journal-event", event, ("payload", "amount"), "type")
    event["payload"]["status"] = "reported"
    assert_error("journal-event", event, ("payload", "status"), "enum")


@pytest.mark.parametrize("state", [
    "pending", "ready", "running", "evaluating", "waiting_evaluation", "retry_wait", "reconciling",
    "succeeded", "failed", "blocked", "cancelled", "indeterminate",
])
def test_closed_run_contains_only_terminal_tasks(state, payloads) -> None:
    event = event_with("run", "run_closed", payloads)
    event["payload"]["run_summary"]["tasks"][0]["state"] = state
    if state in {"succeeded", "failed", "blocked", "cancelled", "indeterminate"}:
        validator_for("journal_event").validate(event)
    else:
        assert_error("journal-event", event, ("payload", "run_summary", "tasks", 0, "state"), "enum")


@pytest.mark.parametrize("manifest_status", ["planning-failed", "cancelled-before-ready"])
def test_closed_run_without_resolved_budget_has_no_invented_currency(manifest_status, payloads) -> None:
    assert load(f"run-manifest-{manifest_status}")["budget_snapshot"] is None
    event = event_with("run", "run_closed", payloads)
    summary = event["payload"]["run_summary"]
    summary.update(state="failed" if manifest_status == "planning-failed" else "cancelled", tasks=[], usage_record_ids=[])
    summary["cost_summary"]["currency"] = None
    validator_for("journal_event").validate(event)
    summary["cost_summary"]["currency"] = "invalid"
    assert_error("journal-event", event, ("payload", "run_summary", "cost_summary", "currency"), "pattern")


@pytest.mark.parametrize("method", ["output_binding_present", "digest_matches", "content_contract_valid"])
def test_workflow_criteria_cannot_have_conflicting_output_ids(method, payloads) -> None:
    report = load("evaluation-report-workflow")
    criterion = report["criteria_snapshot"]["criteria"][0]
    criterion.update(method=method, pass_rule={"output_ids": criterion["output_ids"].copy()})
    assert validate_instance(report, "evaluation_report") == []
    event = event_with("evaluation", "evaluation_requested", payloads)
    event["payload"].update(scope=report["scope"], criteria_snapshot=report["criteria_snapshot"])
    assert validate_instance(event, "journal_event") == []
    criterion["pass_rule"] = {"output_ids": ["different-output"]}
    for value, name, prefix in [(report, "evaluation_report", ""), (event, "journal_event", "/payload")]:
        issues = validate_instance(value, name)
        assert [(issue.code, issue.path) for issue in issues] == [
            ("semantic.criterion_output_ids_mismatch", prefix + "/criteria_snapshot/criteria/0/pass_rule/output_ids"),
        ]
    event["event_type"] = "evaluation_report_recorded"
    event["payload"] = report
    assert validate_instance(event, "journal_event")[0].code == "semantic.criterion_output_ids_mismatch"
    local = load("evaluation-report")
    local["criteria_snapshot"]["criteria"][0].update(method=method, pass_rule={})
    assert_error("evaluation-report", local, ("criteria_snapshot", "criteria", 0, "pass_rule"), "required")


def test_local_criterion_check_runs_only_after_structural_validation() -> None:
    report = load("evaluation-report-workflow")
    report["criteria_snapshot"]["criteria"][0]["pass_rule"] = None
    assert all(issue.code.startswith("schema.") for issue in validate_instance(report, "evaluation_report"))
    assert validate_instance(load("evaluation-report"), "evaluation_report") == []
    report = load("evaluation-report-workflow")
    criterion = report["criteria_snapshot"]["criteria"][0]
    criterion.update(method="future_verifier", pass_rule={"custom": "rule"})
    report["criterion_results"][0].update(method="future_verifier", status="not_run")
    assert validate_instance(report, "evaluation_report") == []


@pytest.mark.parametrize("execution_status", ["failed", "timed_out", "cancelled", "indeterminate"])
@pytest.mark.parametrize("verdict", ["pass", "fail", "inconclusive", "not_run"])
def test_incomplete_evaluation_cannot_deliver_a_conclusive_verdict(execution_status, verdict) -> None:
    report = load("evaluation-report")
    report.update(execution_status=execution_status, verdict=verdict)
    if verdict in {"inconclusive", "not_run"}:
        validator_for("evaluation_report").validate(report)
    else:
        assert_error("evaluation-report", report, ("verdict",), "enum")


@pytest.mark.parametrize(("name", "path"), [
    ("error-record", ("message",)),
    ("evaluation-report", ("criterion_results", 1, "observations", 0)),
    ("run-intent", ("objective",)),
    ("evaluation-report", ("limitations", 0)),
    ("evaluation-report", ("unknowns", 0)),
])
def test_free_text_is_bounded(name, path) -> None:
    record = load(name)
    node = record
    for key in path[:-1]:
        node = node[key]
    if isinstance(node, list) and not node:
        node.append("")
    node[path[-1]] = "x" * 4096
    validator_for(name.replace("-", "_")).validate(record)
    node[path[-1]] += "x"
    assert_error(name, record, path, "maxLength")


@pytest.mark.parametrize("event_type", ["cancel_requested", "budget_released", "state_transition"])
def test_journal_free_text_reasons_are_bounded(event_type, payloads) -> None:
    event = event_with("run", event_type, payloads)
    event["payload"]["reason"] = "x" * 4096
    validator_for("journal_event").validate(event)
    event["payload"]["reason"] += "x"
    assert_error("journal-event", event, ("payload", "reason"), "maxLength")


def test_error_codes_and_admission_reasons_match_the_documented_enums(payloads) -> None:
    contract = (ROOT / "docs/phase-6/run-contracts.md").read_text(encoding="utf-8")
    codes = set(re.findall(r"`([^`]+)`", contract.split("Códigos iniciais do runner: ", 1)[1].split(".\n", 1)[0]))
    reasons = set(re.findall(r"`([^`]+)`", contract.split("Motivos de rejeição de admissão: ", 1)[1].split(".\n", 1)[0]))
    assert set(validator_for("error_record").schema["properties"]["code"]["enum"]) == codes
    assert set(validator_for("journal_event").schema["$defs"]["admission_rejected"]["properties"]["reason"]["enum"]) == reasons
    assert {"journal_line_too_large", "artifact_too_large", "run_locked"} <= codes
    error = load("error-record")
    for code in codes:
        error["code"] = code
        validator_for("error_record").validate(error)
    error["code"] = "invented_code"
    assert_error("error-record", error, ("code",), "enum")
    event = event_with("task", "admission_rejected", payloads)
    for reason in reasons:
        event["payload"]["reason"] = reason
        validator_for("journal_event").validate(event)
    event["payload"]["reason"] = "invented_reason"
    assert_error("journal-event", event, ("payload", "reason"), "enum")


def test_global_evaluation_intent_preserves_workflow_criteria_and_single_evaluation(payloads) -> None:
    event = event_with("evaluation", "evaluation_requested", payloads)
    report = load("evaluation-report-workflow")
    event["payload"].update({key: report[key] for key in ["scope", "criteria_snapshot", "evaluation_id"]})
    validator_for("journal_event").validate(event)
    event["payload"]["evaluation_number"] = 2
    assert_error("journal-event", event, ("payload", "evaluation_number"), "const")
    report["evaluation_number"] = 2
    assert_error("evaluation-report", report, ("evaluation_number",), "const")


def test_journal_chain_shape_and_external_idempotency() -> None:
    first = load("journal-event-first")
    first["previous_event_hash"] = canonical_digest("previous")
    assert_error("journal-event", first, ("previous_event_hash",), "type")
    later = load("journal-event")
    later["previous_event_hash"] = None
    assert_error("journal-event", later, ("previous_event_hash",), "type")
    for actor in ["executor", "evaluator", "human"]:
        observation = load("journal-event-first")
        observation["actor"]["kind"] = actor
        observation["idempotency_key"] = None
        assert_error("journal-event", observation, ("idempotency_key",), "type")


@pytest.mark.parametrize("outcome", ["found_running", "found_finished", "found_failed", "found_stopped", "not_found", "unknown"])
def test_reconciliation_outcomes_are_preserved(outcome, payloads) -> None:
    event = event_with("attempt", "reconciliation_observed", payloads)
    event["payload"]["outcome"] = outcome
    validator_for("journal_event").validate(event)


def test_result_collection_preserves_null_and_rejects_inferred_or_extra_fields(payloads) -> None:
    event = event_with("attempt", "result_collected", payloads)
    event["payload"]["resolved_model_id"] = None
    validator_for("journal_event").validate(event)
    event["payload"]["requested_model_id"] = ""
    assert_error("journal-event", event, ("payload", "requested_model_id"), "minLength")


def test_artifact_declared_limit_and_logical_locator(payloads) -> None:
    event = event_with("attempt", "artifact_ingested", payloads)
    event["payload"]["size_bytes"] = 8388609
    assert_error("journal-event", event, ("payload", "size_bytes"), "maximum")
    event = event_with("attempt", "artifact_ingested", payloads)
    event["payload"]["locator"] = "../../secret"
    assert_error("journal-event", event, ("payload", "locator"), "pattern")


@pytest.mark.parametrize("filename", GOLDEN_HASHES)
def test_example_hashes_are_pinned_and_ignore_only_the_outer_hash_field(filename) -> None:
    record = load(filename)
    field = "event_hash" if filename.startswith("journal-event") else "record_hash"
    unsigned = {key: value for key, value in record.items() if key != field}
    expected = {"algorithm": "sha256", "value": GOLDEN_HASHES[filename]}
    assert record[field] == canonical_digest(unsigned) == expected
    assert hashlib.sha256(canonicalize(unsigned)).hexdigest() == expected["value"]
    def reverse_keys(value):
        if isinstance(value, dict):
            return {key: reverse_keys(child) for key, child in reversed(list(value.items()))}
        if isinstance(value, list):
            return [reverse_keys(child) for child in value]
        return value

    reversed_keys = reverse_keys(unsigned)
    assert list(reversed_keys) == list(reversed(unsigned))
    assert list(reversed_keys) != list(unsigned)
    assert canonical_digest(reversed_keys) == expected
    assert canonical_digest(record) != expected  # including the hash would be a different contract


def test_nested_example_hashes_and_dispatch_bindings_are_consistent() -> None:
    manifest = load("run-manifest")
    assert manifest["run_intent_ref"]["content_digest"] == load("run-intent")["record_hash"]
    assert manifest["objective_snapshot"]["objective_hash"] == canonical_digest(manifest["objective_snapshot"]["objective"])
    for filename in ["evaluation-report", "evaluation-report-workflow"]:
        report = load(filename)
        assert report["criteria_snapshot"]["criteria_hash"] == canonical_digest(report["criteria_snapshot"]["criteria"])
        assert report["evaluator_snapshot"] == manifest["evaluator_snapshot"]
    header, intent = load("attempt-header"), load("dispatch-intent")
    assert load("evaluation-report")["completed_at"] < header["created_at"]
    assert header["created_at"] <= load("usage-record")["observed_at"] <= load("evaluation-report-workflow")["started_at"]
    assert header["created_at"] <= load("error-record")["recorded_at"]
    assert manifest["executor_snapshot"]["executor_ref"] == intent["executor_ref"]
    for estimate in manifest["budget_snapshot"]["cost_estimates"]:
        assert estimate["source_ref"] == manifest["executor_snapshot"]["script_ref"]
        expected = intent["target_ref"] if estimate["scope"] == "attempt" else manifest["evaluator_snapshot"]["evaluator_ref"]
        assert estimate["subject_ref"] == expected
    workflow_artifact = load("evaluation-report-workflow")["subject_bindings"][0]["artifact"]
    assert workflow_artifact == {"artifact_id": "artifact-T3-2-code_patch", "content_digest": canonical_digest("P2")}
    assert workflow_artifact != load("evaluation-report")["subject_bindings"][0]["artifact"]
    for key in set(header) & set(intent) - {"record_hash"}:
        assert header[key] == intent[key]
    assert load("journal-event")["payload"] == {"attempt_header": header, "dispatch_intent": intent}
    assert load("journal-event-first")["payload"] == load("run-intent")


def test_event_hash_covers_the_previous_link_and_embedded_record_hashes() -> None:
    event = load("journal-event")
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    for path in [("previous_event_hash", "value"), ("payload", "dispatch_intent", "record_hash", "value")]:
        tampered = copy.deepcopy(unsigned)
        node = tampered
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = "0" * 64
        assert canonical_digest(tampered) != event["event_hash"]
    line = canonicalize(event) + b"\n"
    assert line.count(b"\n") == 1 and len(line) <= 1048576
