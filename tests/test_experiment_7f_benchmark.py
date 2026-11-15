"""Validação do pacote do benchmark 7F (experiments/7f): corpus, tarefas, gabaritos, manifesto e lotes.

Nada aqui executa CLIs de assinatura nem acessa rede; o pacote é só dado e documentação.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from orq_contracts.canonical import canonical_digest
from orq_contracts.evaluator import ContentContractRegistry
from orq_contracts.knowledge_backend import EligibilityPolicy, requirement_reasons, scope_allows
from orq_contracts.memory_store.config import MemoryScope
from orq_contracts.validator import validate_instance, validate_task, validate_workflow
from orq_contracts.vault import build_catalog_snapshot, ingest_markdown_source

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "experiments" / "7f"
VAULT = PACKAGE / "vault"
sys.path.insert(0, str(PACKAGE / "tools"))

import f7_build  # noqa: E402
import f7_checks  # noqa: E402
import f7_corpus  # noqa: E402

ROLES = {"software_architect", "backend_engineer", "tester", "security_reviewer"}
FAMILIES = {"family_claude", "family_codex", "family_gemini", "family_kiro"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return read_json(PACKAGE / "manifest.json")


@pytest.fixture(scope="module")
def definition() -> dict[str, Any]:
    return read_json(VAULT / "catalog-definition.json")


@pytest.fixture(scope="module")
def snapshot() -> dict[str, Any]:
    return read_json(VAULT / "catalog-snapshot.json")


@pytest.fixture(scope="module")
def tasks() -> list[dict[str, Any]]:
    return [read_json(path) for path in sorted((PACKAGE / "tasks").glob("*.json"))]


def entries_by_id(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["source_metadata"]["source_id"]: entry for entry in snapshot["source_entries"]}


# ------------------------------------------------------------------- corpus e catálogo


def test_vault_sources_pass_the_vault_pipeline(definition: dict[str, Any]) -> None:
    assert len(definition["sources"]) == len(f7_corpus.SOURCES)
    for metadata in definition["sources"]:
        ingested = ingest_markdown_source(VAULT, metadata)
        assert len(ingested.units) == 1, metadata["source_id"]
        assert not ingested.units[0]["oversized"]
        assert ingested.metadata["audience"] == f7_corpus.BY_ID[metadata["source_id"]].audience


def test_catalog_snapshot_is_reproducible_and_valid(
    definition: dict[str, Any], snapshot: dict[str, Any], manifest: dict[str, Any]
) -> None:
    assert validate_instance(snapshot, "knowledge_catalog") == []
    assert snapshot["exclusions"] == []
    assert build_catalog_snapshot(VAULT, definition) == snapshot
    body = {key: value for key, value in snapshot.items() if key != "catalog_hash"}
    assert canonical_digest(body) == snapshot["catalog_hash"]
    assert manifest["catalog"]["catalog_hash"] == snapshot["catalog_hash"]
    assert manifest["catalog"]["source_count"] == len(snapshot["source_entries"]) == len(f7_corpus.SOURCES)


def test_corpus_audience_groups_and_synthetic_marking() -> None:
    synthetic_note = f7_corpus.NOTE_SYNTHETIC
    for source in f7_corpus.SOURCES:
        audience = source.audience
        assert validate_instance(audience, "audience_metadata") == []
        if source.group == "common":
            assert audience["area_scope"] == "common"
        elif source.group == "role":
            assert audience["target_families"] == [] and len(audience["target_roles"]) == 1
        elif source.group == "family":
            assert len(audience["target_families"]) == 1 and audience["target_roles"] == []
        elif source.group == "family_role":
            assert audience["target_families"] and audience["target_roles"]
        else:
            assert source.group == "synthetic" and source.synthetic
            assert len(audience["target_families"]) == 1 and audience["target_roles"] == []
        text = (VAULT / source.path).read_text(encoding="utf-8")
        assert (source.group == "synthetic") == source.synthetic
        assert (synthetic_note in text) == source.synthetic
        assert ("sintetico" in source.topics) == source.synthetic
        assert all(family in FAMILIES for family in audience["target_families"])
        assert set(audience["target_roles"]) <= ROLES | {"code_reviewer"}
    groups = {source.group for source in f7_corpus.SOURCES}
    assert groups == {"common", "role", "family", "family_role", "synthetic"}


def test_every_family_has_real_and_synthetic_units() -> None:
    for family in FAMILIES:
        real = [s for s in f7_corpus.SOURCES if not s.synthetic and family in s.families]
        fake = [s for s in f7_corpus.SOURCES if s.synthetic and family in s.families]
        assert real, family
        assert fake, family


SOURCE_DOCS = {
    # fatos de cada unidade real só valem se aparecem nos documentos/código ligados a ela (nunca no benchmark)
    "common-jcs-digest": ["src/orq_contracts/canonical.py", "schemas/v0.1/common.schema.json"],
    "common-identifiers": ["schemas/v0.1/common.schema.json"],
    "common-content-contract": ["src/orq_contracts/evaluator.py", "schemas/v0.1/common.schema.json"],
    "common-evaluator-methods": ["src/orq_contracts/evaluator.py"],
    "common-subscription-policy": ["docs/phase-7/subscription-adapters.md"],
    "common-cost-honesty": ["docs/phase-7/subscription-adapters.md"],
    "common-audience-rules": ["docs/phase-7/family-memory.md"],
    "common-rls-scope": ["docs/phase-7/family-memory.md", "docs/phase-7/README.md"],
    "common-output-protocol": ["docs/phase-7/subscription-adapters.md"],
    "common-quota-cooldown": ["docs/phase-7/quota-aware-routing.md"],
    "common-context-budget": ["docs/phase-7/family-memory.md"],
    "common-host-process-rules": ["docs/phase-7/subscription-adapters.md", "docs/phase-6/executor-choice.md"],
    "role-software-architect": ["docs/phase-7/subscription-adapters.md", "docs/phase-7/experiment.md"],
    "role-backend-engineer": ["docs/phase-7/subscription-adapters.md"],
    "role-tester": ["docs/phase-7/subscription-adapters.md", "AGENTS.md"],
    "role-security-reviewer": ["docs/phase-7/subscription-adapters.md"],
    "family-claude-invocation": ["docs/phase-7/subscription-adapters.md"],
    "family-codex-invocation": ["docs/phase-7/subscription-adapters.md", "docs/phase-6/executor-choice.md"],
    "family-codex-readonly": ["docs/phase-7/subscription-adapters.md"],
    "family-codex-windows-tests": ["docs/phase-7/subscription-adapters.md", "docs/phase-6/executor-choice.md"],
    "family-gemini-invocation": ["docs/phase-7/subscription-adapters.md", "docs/phase-6/executor-choice.md"],
    "family-gemini-readonly": ["docs/phase-7/subscription-adapters.md"],
    "family-gemini-permissions": ["docs/phase-7/subscription-adapters.md", "docs/phase-6/executor-choice.md"],
    "family-gemini-noise": ["docs/phase-7/subscription-adapters.md"],
    "family-kiro-invocation": ["docs/phase-7/subscription-adapters.md", "docs/phase-6/executor-choice.md"],
    "family-kiro-trust-tools": ["docs/phase-7/subscription-adapters.md"],
    "family-kiro-output": ["docs/phase-7/subscription-adapters.md", "docs/phase-6/executor-choice.md"],
    "family-claude-quota": ["docs/phase-7/quota-aware-routing.md"],
    "family-codex-quota": ["docs/phase-7/quota-aware-routing.md"],
    "family-gemini-quota": ["docs/phase-7/quota-aware-routing.md"],
    "family-kiro-quota": ["docs/phase-7/quota-aware-routing.md"],
    "family-claude-security": ["docs/phase-7/subscription-adapters.md"],
    "family-gemini-security": ["docs/phase-7/subscription-adapters.md"],
    "family-kiro-security": ["docs/phase-7/subscription-adapters.md"],
}
OPTION = re.compile(r"(?<![\w-])(--[A-Za-z][A-Za-z-]*|-[A-Za-z])(?![\w-])")
CODE_SPAN = re.compile(r"`([^`\n]+)`")


def test_real_units_flags_and_code_terms_appear_in_their_own_sources() -> None:
    """Escopo: flags e termos de código de cada unidade real aparecem nas fontes ligadas a ela.

    Não prova que o resumo está semanticamente correto; só impede flag ou termo sem origem no repositório.
    """

    real = {source.source_id for source in f7_corpus.SOURCES if not source.synthetic}
    assert set(SOURCE_DOCS) == real
    for source in f7_corpus.SOURCES:
        if source.synthetic:
            continue
        files = SOURCE_DOCS[source.source_id]
        if source.source_id != "role-software-architect":
            assert not any(name.endswith("experiment.md") for name in files)
        documentation = "\n".join((ROOT / name).read_text(encoding="utf-8") for name in files)
        text = source.markdown().split("\n---\n", 1)[1]
        for flag in set(OPTION.findall(text)):
            assert flag in documentation, f"{source.source_id}: {flag} sem origem em {files}"
        if source.group.startswith("family"):
            for span in set(CODE_SPAN.findall(text)):
                term = re.split(r"[=( <]", span, maxsplit=1)[0]
                if len(term) >= 3 and re.fullmatch(r"[A-Za-z_.:/~\\-]+", term):
                    assert term in documentation, f"{source.source_id}: `{span}` sem origem em {files}"


# ------------------------------------------------------------------- tarefas


def test_ten_tasks_cover_the_four_roles_and_four_families(tasks: list[dict[str, Any]]) -> None:
    assert len(tasks) == 10
    assert len({task["task_id"] for task in tasks}) == 10
    roles = [task["target"]["role_id"] for task in tasks]
    families = [task["target"]["family_id"] for task in tasks]
    assert set(roles) == ROLES and set(families) == FAMILIES
    assert all(roles.count(role) >= 2 for role in ROLES)
    assert all(families.count(family) >= 2 for family in FAMILIES)


def test_tasks_validate_against_project_schemas(tasks: list[dict[str, Any]]) -> None:
    for wrapper in tasks:
        task = wrapper["task"]
        assert wrapper["schema_version"] == "7f-task-0.1"
        assert validate_task(task) == [], wrapper["task_id"]
        assert task["task_id"] == wrapper["task_id"]
        assert task["agent_role"]["id"] == wrapper["target"]["role_id"]
        assert task["constraints"]["max_attempts"] == 1
        assert {item["method"] for item in task["evaluation_criteria"]} <= {
            "output_binding_present",
            "digest_matches",
            "content_contract_valid",
        }
        assert task["knowledge_requirements"], wrapper["task_id"]


def test_each_task_is_a_valid_single_task_workflow(tasks: list[dict[str, Any]]) -> None:
    for wrapper in tasks:
        task = wrapper["task"]
        workflow = {
            "schema_version": "0.1",
            "workflow_id": f"workflow-{task['task_id']}",
            "version": "1",
            "project_id": f7_build.PROJECT_ID,
            "objective": task["objective"],
            "requirements": [{"requirement_id": "WREQ1", "description": "Resposta correta", "required": True}],
            "external_inputs": [],
            "tasks": [task],
            "requirement_coverage": [
                {"workflow_requirement_id": "WREQ1", "task_id": task["task_id"], "task_requirement_id": "REQ1"}
            ],
            "outputs": [
                {
                    "output_id": "answer",
                    "artifact_type": "structured_answer",
                    "source_task_id": task["task_id"],
                    "source_output_id": "answer",
                }
            ],
            "evaluation_criteria": [
                {
                    "criterion_id": f"WCRIT{index}",
                    "method": method,
                    "pass_rule": {"output_ids": ["answer"]},
                    "required": True,
                    "verifier_ref": None,
                    "workflow_requirement_ids": ["WREQ1"],
                    "output_ids": ["answer"],
                }
                for index, method in enumerate(
                    ("output_binding_present", "digest_matches", "content_contract_valid"), start=1
                )
            ],
            "execution_policy_ref": f7_build.versioned_ref("policy-7f", {"policy": "7f"}),
            "constraints": {
                "max_concurrency": 1,
                "max_run_duration_ms": 600000,
                "max_attempts": 1,
                "attempt_timeout_ms": 300000,
                "max_evaluations_per_attempt": 1,
                "evaluation_timeout_ms": 20000,
                "reconciliation_timeout_ms": 15000,
            },
        }
        assert validate_workflow(workflow) == [], wrapper["task_id"]


def test_output_contracts_are_closed_objects_with_matching_digests(tasks: list[dict[str, Any]]) -> None:
    for wrapper in tasks:
        schema = wrapper["output_contract"]
        Draft202012Validator.check_schema(schema)
        reference = wrapper["task"]["expected_output"][0]["content_contract_ref"]
        assert canonical_digest(schema) == reference["content_digest"]
        registry = ContentContractRegistry()
        registry.register(reference, schema)
        validator = registry.validator(reference)
        assert validator is not None
        assert validator.is_valid(wrapper["reference_answers"]["pass"]), wrapper["task_id"]
        assert validator.is_valid(wrapper["reference_answers"]["contaminated"]), wrapper["task_id"]
        # só estrutura: objeto fechado, campos obrigatórios e sem `const`. Os enums listam todas as opções
        # (inclusive a correta): este teste NÃO alega ausência de vazamento do gabarito.
        assert "const" not in json.dumps(schema)
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False


def test_deterministic_checks_pass_reference_and_fail_contaminated(tasks: list[dict[str, Any]]) -> None:
    for wrapper in tasks:
        checks = wrapper["acceptance"]["deterministic_checks"]
        ids = [check["check_id"] for check in checks]
        assert len(ids) == len(set(ids))
        assert {check["kind"] for check in checks} <= f7_checks.KINDS
        assert any(check["kind"] == "no_forbidden_text" for check in checks)
        passed = f7_checks.evaluate_checks(wrapper["reference_answers"]["pass"], checks)
        assert all(item["passed"] for item in passed), (wrapper["task_id"], passed)
        failed = f7_checks.evaluate_checks(wrapper["reference_answers"]["contaminated"], checks)
        assert not all(item["passed"] for item in failed), wrapper["task_id"]


def test_checks_module_semantics() -> None:
    answer = {"flag": "--x", "n": 1, "ok": False, "tools": ["A", "B"], "nested": {"text": "use --bad aqui"}}
    run = f7_checks.run_check
    assert run(answer, {"kind": "equals", "pointer": "/flag", "value": "--x"})[0]
    assert not run(answer, {"kind": "equals", "pointer": "/n", "value": True})[0]
    assert not run(answer, {"kind": "equals", "pointer": "/ok", "value": 0})[0]
    assert not run(answer, {"kind": "equals", "pointer": "/ausente", "value": 1})[0]
    assert run(answer, {"kind": "set_equals", "pointer": "/tools", "values": ["B", "A"]})[0]
    assert not run(answer, {"kind": "set_equals", "pointer": "/tools", "values": ["A"]})[0]
    assert not run({"tools": ["A", "A"]}, {"kind": "set_equals", "pointer": "/tools", "values": ["A"]})[0]
    assert run(answer, {"kind": "contains_all", "pointer": "/tools", "values": ["A"]})[0]
    assert not run(answer, {"kind": "contains_all", "pointer": "/tools", "values": ["C"]})[0]
    assert run(answer, {"kind": "matches", "pointer": "/flag", "pattern": "--[a-z]"})[0]
    assert not run(answer, {"kind": "matches", "pointer": "/flag", "pattern": "--"})[0]
    assert not run(answer, {"kind": "no_forbidden_text", "values": ["--bad"]})[0]
    assert run(answer, {"kind": "no_forbidden_text", "values": ["--worse"]})[0]
    with pytest.raises(ValueError):
        run(answer, {"kind": "llm_judge", "pointer": "/flag"})


# ------------------------------------------------------------------- gabaritos e distratores


def scope_of(wrapper: dict[str, Any]) -> MemoryScope:
    return MemoryScope(
        f7_build.CATALOG_ID,
        f7_build.PROJECT_ID,
        wrapper["target"]["family_id"],
        wrapper["target"]["role_id"],
    )


def test_gold_points_to_existing_units_with_correct_audience(
    tasks: list[dict[str, Any]], snapshot: dict[str, Any]
) -> None:
    entries = entries_by_id(snapshot)
    for wrapper in tasks:
        scope = scope_of(wrapper)
        requirement_ids = {item["knowledge_requirement_id"] for item in wrapper["task"]["knowledge_requirements"]}
        gold = wrapper["knowledge"]["gold"]
        assert {item["requirement_id"] for item in gold} == requirement_ids, wrapper["task_id"]
        assert len({(item["requirement_id"], item["source_id"]) for item in gold}) == len(gold)
        for item in gold:
            entry = entries[item["source_id"]]
            source, unit = entry["source_metadata"], entry["units"][0]
            assert item["revision"] == source["revision"]
            assert item["unit_id"] == unit["unit_id"]
            assert item["content_digest"] == unit["content_digest"]
            # visível sob o escopo da família/papel da tarefa, pelo espelho do RLS e pela regra de audiência
            assert scope_allows(entry, source, unit, scope), (wrapper["task_id"], item["source_id"])
            assert f7_build.visible(f7_corpus.BY_ID[item["source_id"]], scope.family_id, scope.role_id)
            # e o requisito casa por tópico com a unidade
            topics = next(
                kr["applicability_filters"]["topics"]
                for kr in wrapper["task"]["knowledge_requirements"]
                if kr["knowledge_requirement_id"] == item["requirement_id"]
            )
            assert set(topics) & set(unit["effective_topics"]), (wrapper["task_id"], item["source_id"])
            assert not f7_corpus.BY_ID[item["source_id"]].synthetic


def test_distractors_cover_other_families_and_are_hidden_by_rls(
    tasks: list[dict[str, Any]], snapshot: dict[str, Any]
) -> None:
    entries = entries_by_id(snapshot)
    policy = EligibilityPolicy(project_id=f7_build.PROJECT_ID, as_of="2026-11-15T00:00:00Z")
    for wrapper in tasks:
        scope = scope_of(wrapper)
        gold_ids = {item["source_id"] for item in wrapper["knowledge"]["gold"]}
        distractors = wrapper["knowledge"]["distractors"]
        assert len(distractors) >= 3, wrapper["task_id"]
        assert len({item["source_id"] for item in distractors}) == len(distractors)
        assert all(item["reason"] for item in distractors)
        kinds = {item["kind"] for item in distractors}
        assert "other_family_synthetic" in kinds, wrapper["task_id"]
        for item in distractors:
            source = f7_corpus.BY_ID[item["source_id"]]
            entry = entries[item["source_id"]]
            unit = entry["units"][0]
            assert item["source_id"] not in gold_ids
            assert item["unit_id"] == unit["unit_id"] and item["content_digest"] == unit["content_digest"]
            assert item["kind"] == f7_build.distractor_kind(source, scope.family_id)
            # regime particionado: nunca visível; regime compartilhado: visível a qualquer família
            assert not scope_allows(entry, entry["source_metadata"], unit, scope), item["source_id"]
            assert not f7_build.visible(source, scope.family_id, scope.role_id)
            # distrator realista: passa nos filtros do host para pelo menos um requisito da tarefa
            eligible_for = [
                kr["knowledge_requirement_id"]
                for kr in wrapper["task"]["knowledge_requirements"]
                if not requirement_reasons(snapshot, kr, policy)[
                    (item["source_id"], item["revision"], item["unit_id"])
                ]
            ]
            assert eligible_for, (wrapper["task_id"], item["source_id"])
        # os requisitos da tarefa também liberam todo o gabarito
        for item in wrapper["knowledge"]["gold"]:
            kr = next(
                kr for kr in wrapper["task"]["knowledge_requirements"]
                if kr["knowledge_requirement_id"] == item["requirement_id"]
            )
            key = (item["source_id"], item["revision"], item["unit_id"])
            assert requirement_reasons(snapshot, kr, policy)[key] == []


# (tópico, {família: (fonte, trecho que só ela traz)}): regras diferentes para o mesmo fim operacional
CONTRASTIVE_PAIRS = [
    ("somente-leitura", {
        "family_codex": ("family-codex-readonly", "-s read-only"),
        "family_gemini": ("family-gemini-readonly", "--mode plan"),
        "family_kiro": ("family-kiro-trust-tools", "--trust-tools=fs_read"),
    }),
    ("modelo", {
        "family_codex": ("family-codex-invocation", "-m <native_model_id>"),
        "family_gemini": ("family-gemini-invocation", "--model <native_model_id>"),
    }),
    ("diretorio-de-trabalho", {
        "family_codex": ("family-codex-invocation", "-C <workspace_dir>"),
        "family_gemini": ("family-gemini-invocation", "cwd=workspace_dir"),
        "family_kiro": ("family-kiro-invocation", "wsl.exe --cd <workspace_dir>"),
    }),
    ("saida-estruturada", {
        "family_claude": ("family-claude-invocation", "--output-format json"),
        "family_codex": ("family-codex-invocation", "--json -o"),
        "family_gemini": ("family-gemini-invocation", "--output-format text"),
    }),
    ("cota", {
        "family_codex": ("family-codex-quota", "try again at"),
        "family_gemini": ("family-gemini-quota", "RESOURCE_EXHAUSTED"),
        "family_kiro": ("family-kiro-quota", "monthly limit"),
    }),
]


def test_explicit_contrastive_pairs_use_different_text_per_family() -> None:
    """Cada par declarado: a unidade existe, tem o tópico, é da família e traz um trecho ausente das demais."""

    for topic, members in CONTRASTIVE_PAIRS:
        assert len(members) >= 2
        for family, (source_id, literal) in members.items():
            source = f7_corpus.BY_ID[source_id]
            assert not source.synthetic and topic in source.topics and family in source.families
            assert literal in source.markdown(), (topic, source_id)
            for other_family, (other_id, _) in members.items():
                if other_family != family:
                    assert literal not in f7_corpus.BY_ID[other_id].markdown(), (topic, source_id, other_id)


def answer_tokens(wrapper: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for check in wrapper["acceptance"]["deterministic_checks"]:
        if check["kind"] == "equals" and isinstance(check["value"], str):
            tokens.add(check["value"])
        elif check["kind"] in {"set_equals", "contains_all"}:
            tokens |= {value for value in check["values"] if isinstance(value, str)}
    return {token for token in tokens if len(token) >= 2}


def contains_token(text: str, token: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", text) is not None


def classes_of(wrapper: dict[str, Any]) -> dict[str, set[str]]:
    knowledge = wrapper["knowledge"]
    return {
        "gold": {item["source_id"] for item in knowledge["gold"]},
        "alternatives": {item["source_id"] for item in knowledge["acceptable_alternatives"]},
        "distractors": {item["source_id"] for item in knowledge["distractors"]},
        "irrelevant": {item["source_id"] for item in knowledge["irrelevant"]},
    }


def test_every_eligible_unit_falls_into_exactly_one_class(
    tasks: list[dict[str, Any]], snapshot: dict[str, Any]
) -> None:
    """Alega só isto: as 4 classes são disjuntas e juntas cobrem todas as unidades elegíveis (filtros do host,
    regime shared_global) aos requisitos da tarefa, com motivo declarado nas classes não-gold."""

    policy = EligibilityPolicy(project_id=f7_build.PROJECT_ID, as_of="2026-11-15T00:00:00Z")
    for wrapper in tasks:
        classes = classes_of(wrapper)
        total = sum(len(item) for item in classes.values())
        union = set().union(*classes.values())
        assert total == len(union), wrapper["task_id"]
        eligible = {
            key[0]
            for requirement in wrapper["task"]["knowledge_requirements"]
            for key, reasons in requirement_reasons(snapshot, requirement, policy).items()
            if not reasons
        }
        assert union == eligible, (wrapper["task_id"], sorted(union ^ eligible))
        knowledge = wrapper["knowledge"]
        for name in ("acceptable_alternatives", "distractors", "irrelevant"):
            assert all(item["reason"].strip() for item in knowledge[name]), (wrapper["task_id"], name)
        for item in [*knowledge["gold"], *knowledge["acceptable_alternatives"], *knowledge["irrelevant"],
                     *knowledge["distractors"]]:
            entry = entries_by_id(snapshot)[item["source_id"]]
            assert item["unit_id"] == entry["units"][0]["unit_id"]
            assert item["content_digest"] == entry["units"][0]["content_digest"]
        # unidades sintéticas elegíveis são sempre distratoras (regras fictícias contraditórias)
        for source_id in union:
            if f7_corpus.BY_ID[source_id].synthetic:
                assert source_id in classes["distractors"], (wrapper["task_id"], source_id)


def test_no_distractor_is_declared_as_an_answer_source(tasks: list[dict[str, Any]]) -> None:
    """Alega só isto: `answer_sources` (declaradas à mão por checagem, inclusive booleanos e números) cobre
    todas as checagens de resposta, cada uma tem fonte no gold, e nenhuma fonte listada é distratora ou irrelevante.
    Paráfrases não são detectadas automaticamente; a declaração é a premissa."""

    for wrapper in tasks:
        classes = classes_of(wrapper)
        sources = wrapper["knowledge"]["answer_sources"]
        answer_checks = {
            check["check_id"]
            for check in wrapper["acceptance"]["deterministic_checks"]
            if check["kind"] != "no_forbidden_text"
        }
        assert set(sources) == answer_checks, wrapper["task_id"]
        for check_id, listed in sources.items():
            assert listed and len(set(listed)) == len(listed), (wrapper["task_id"], check_id)
            assert set(listed) & classes["gold"], (wrapper["task_id"], check_id)
            assert set(listed) <= classes["gold"] | classes["alternatives"], (wrapper["task_id"], check_id)
            assert not set(listed) & (classes["distractors"] | classes["irrelevant"])


def test_literal_answer_tokens_do_not_appear_in_distractors_or_irrelevant(tasks: list[dict[str, Any]]) -> None:
    """Heurística literal (não cobre booleanos, números nem paráfrases): termos-resposta em texto, que existem
    em gold ou alternativas, não aparecem como token inteiro em distratores nem em irrelevantes."""

    for wrapper in tasks:
        classes = classes_of(wrapper)
        reference = "\n".join(
            f7_corpus.BY_ID[source_id].markdown() for source_id in classes["gold"] | classes["alternatives"]
        )
        answers = {token for token in answer_tokens(wrapper) if contains_token(reference, token)}
        assert answers, wrapper["task_id"]
        for source_id in classes["distractors"] | classes["irrelevant"]:
            text = f7_corpus.BY_ID[source_id].markdown()
            leaked = sorted(token for token in answers if contains_token(text, token))
            assert not leaked, (wrapper["task_id"], source_id, leaked)


def mutations(wrapper: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Respostas de referência com um único erro cada: valor trocado, elemento extra, elemento faltando."""

    reference = wrapper["reference_answers"]["pass"]
    result = []
    for check in wrapper["acceptance"]["deterministic_checks"]:
        if check["kind"] == "no_forbidden_text":
            continue
        field = check["pointer"].lstrip("/")
        variants = []
        value = reference[field]
        if isinstance(value, bool):
            variants.append(not value)
        elif isinstance(value, int):
            variants.append(value + 1)
        elif isinstance(value, str):
            variants.append(value + "x")
        else:
            variants.append([*value, "--trust-all-tools"])
            variants.append([*value, "extra-item"])
            variants.append(value[:-1])
            variants.append([*value, value[0]])
        for index, variant in enumerate(variants):
            result.append((f"{check['check_id']}#{index}", {**reference, field: variant}))
    return result


def test_each_isolated_error_is_rejected_by_its_check(tasks: list[dict[str, Any]]) -> None:
    for wrapper in tasks:
        checks = wrapper["acceptance"]["deterministic_checks"]
        assert f7_checks.all_passed(wrapper["reference_answers"]["pass"], checks)
        for label, answer in mutations(wrapper):
            results = {item["check_id"]: item["passed"] for item in f7_checks.evaluate_checks(answer, checks)}
            check_id = label.split("#")[0]
            assert not results[check_id], (wrapper["task_id"], label)
            assert not f7_checks.all_passed(answer, checks), (wrapper["task_id"], label)


def test_most_tasks_have_real_other_family_distractors(tasks: list[dict[str, Any]]) -> None:
    with_real = [
        task["task_id"]
        for task in tasks
        if any(item["kind"] == "other_family_real" for item in task["knowledge"]["distractors"])
    ]
    assert len(with_real) >= 8, with_real


# ------------------------------------------------------------------- manifesto


def test_manifest_digests_match_files(manifest: dict[str, Any]) -> None:
    listed = {item["path"]: item for item in manifest["files"]}
    on_disk = {
        path.relative_to(PACKAGE).as_posix()
        for folder in ("vault", "tasks")
        for path in (PACKAGE / folder).rglob("*")
        if path.is_file()
    } | {f"tools/{path.name}" for path in (PACKAGE / "tools").glob("*.py")}
    assert {"tools/f7_checks.py", "tools/f7_build.py", "tools/f7_tasks.py", "tools/f7_corpus.py"} <= set(listed)
    assert set(listed) == on_disk
    for relative, item in listed.items():
        raw = (PACKAGE / relative).read_bytes()
        assert item["raw_sha256"] == f7_build.sha256_hex(raw), relative
        if relative.endswith(".json"):
            assert item["jcs_digest"] == canonical_digest(json.loads(raw))["value"], relative
        else:
            assert "jcs_digest" not in item
    body = {key: value for key, value in manifest.items() if key != "package_digest"}
    assert manifest["package_digest"] == canonical_digest(body)
    # o digest cobre sementes, plano de lotes e arquivos, não só a lista de arquivos
    for change in (
        lambda m: m["seeds"].__setitem__("batch_plan", 1),
        lambda m: m["batch_plan"]["batches"][0]["runs"].reverse(),
        lambda m: m["files"][0].__setitem__("raw_sha256", "0" * 64),
    ):
        altered = copy.deepcopy(body)
        change(altered)
        assert canonical_digest(altered) != manifest["package_digest"]


def test_manifest_matches_design_and_tasks(manifest: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
    design = manifest["design"]
    assert (design["tasks"], design["repetitions"], design["total_runs"]) == (10, 3, 60)
    assert design["conditions"] == ["shared_global", "family_partitioned_rls"]
    assert [item["task_id"] for item in manifest["tasks"]] == [task["task_id"] for task in tasks]
    for item, task in zip(manifest["tasks"], tasks, strict=True):
        assert item["role_id"] == task["target"]["role_id"] and item["family_id"] == task["target"]["family_id"]
    assert manifest["catalog"]["synthetic_source_ids"] == sorted(
        source.source_id for source in f7_corpus.SOURCES if source.synthetic
    )


def test_package_regenerates_byte_for_byte(tmp_path: Path) -> None:
    f7_build.build_all(tmp_path)
    for path in sorted(p for p in tmp_path.rglob("*") if p.is_file()):
        relative = path.relative_to(tmp_path)
        assert (PACKAGE / relative).read_bytes() == path.read_bytes(), relative.as_posix()


# ------------------------------------------------------------------- plano de lotes


def all_runs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [run for batch in manifest["batch_plan"]["batches"] for run in batch["runs"]]


def test_batch_plan_covers_each_combination_exactly_once(manifest: dict[str, Any]) -> None:
    runs = all_runs(manifest)
    task_ids = [item["task_id"] for item in manifest["tasks"]]
    expected = set(itertools.product(task_ids, (1, 2, 3), ("shared_global", "family_partitioned_rls")))
    assert len(expected) == 60 and len(runs) == 60
    assert {(run["task_id"], run["repetition"], run["condition"]) for run in runs} == expected
    assert len({run["run_key"] for run in runs}) == 60
    assert [run["global_ordinal"] for run in runs] == list(range(1, 61))
    assert len({run["run_seed"] for run in runs}) == 60


def test_batch_plan_is_balanced_and_paired(manifest: dict[str, Any]) -> None:
    plan = manifest["batch_plan"]
    assert len(plan["batches"]) == 6
    first_counts = {"shared_global": 0, "family_partitioned_rls": 0}
    batch_of_task: dict[str, set[int]] = {}
    for batch in plan["batches"]:
        runs = batch["runs"]
        assert len(runs) == batch["run_count"] == 10
        assert [run["position_in_batch"] for run in runs] == list(range(1, 11))
        assert sum(run["condition"] == "shared_global" for run in runs) == 5
        for index in range(0, 10, 2):
            first, second = runs[index], runs[index + 1]
            assert first["pair_id"] == second["pair_id"]
            assert first["condition"] != second["condition"]
            assert first["condition"] == first["pair_first_condition"]
            first_counts[first["condition"]] += 1
        for run in runs:
            batch_of_task.setdefault(run["task_id"], set()).add(batch["batch"])
    assert first_counts == {"shared_global": 15, "family_partitioned_rls": 15}
    # contrabalanceamento da condição inicial por família-alvo e por tarefa (diferença máxima de 1 par)
    family_of = {item["task_id"]: item["family_id"] for item in manifest["tasks"]}
    by_family: dict[str, int] = {}
    by_task: dict[str, int] = {}
    for run in all_runs(manifest):
        if run["position_in_batch"] % 2 == 1:
            step = 1 if run["condition"] == "shared_global" else -1
            family = family_of[run["task_id"]]
            by_family[family] = by_family.get(family, 0) + step
            by_task[run["task_id"]] = by_task.get(run["task_id"], 0) + step
    assert len(by_family) == 4 and len(by_task) == 10
    assert all(abs(value) <= 1 for value in by_family.values()), by_family
    assert all(abs(value) <= 1 for value in by_task.values()), by_task
    # as três repetições de cada tarefa caem em dias distintos
    assert all(len(batches) == 3 for batches in batch_of_task.values())
    assert len({run["pair_id"] for run in all_runs(manifest)}) == 30


def test_batch_plan_is_reproducible_from_the_seed(manifest: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
    task_ids = [task["task_id"] for task in tasks]
    families = {task["task_id"]: task["target"]["family_id"] for task in tasks}
    roles = {task["task_id"]: task["target"]["role_id"] for task in tasks}
    assert f7_build.build_batch_plan(task_ids, families, roles) == manifest["batch_plan"]
    assert manifest["batch_plan"]["seed"] == manifest["seeds"]["batch_plan"] == f7_build.BATCH_SEED
    for run in all_runs(manifest):
        assert run["run_seed"] == int(f7_build.rank_key("run-seed", run["run_key"])[:12], 16)
