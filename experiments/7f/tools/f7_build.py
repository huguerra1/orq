"""Gerador determinístico do pacote 7F (corpus, catálogo, tarefas e manifesto).

Uso (a partir da raiz do repositório):

    PYTHONPATH=src python experiments/7f/tools/f7_build.py            # reescreve experiments/7f/
    PYTHONPATH=src python experiments/7f/tools/f7_build.py --check    # falha se algo divergir

Não acessa rede, não executa CLIs e não usa aleatoriedade fora das sementes do manifesto.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
PACKAGE_ROOT = TOOLS.parent
REPO_ROOT = PACKAGE_ROOT.parents[1]
for path in (TOOLS, REPO_ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from orq_contracts.canonical import canonical_digest  # noqa: E402
from orq_contracts.knowledge_backend import EligibilityPolicy, requirement_reasons  # noqa: E402
from orq_contracts.vault import build_catalog_snapshot  # noqa: E402

import f7_classes  # noqa: E402
import f7_corpus  # noqa: E402
import f7_tasks  # noqa: E402

PROJECT_ID = "orq-7f"
CATALOG_ID = "knowledge-orq-7f"
CREATED_AT = "2026-11-14T12:00:00Z"
ASSIGNED_AT = "2026-11-14T10:00:00Z"
BATCH_SEED = 20261114
REPETITIONS = (1, 2, 3)
CONDITIONS = ("shared_global", "family_partitioned_rls")
BATCH_COUNT = 6
EXPECTED_TASKS = 10


def dump_json(document: Any) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def versioned_ref(identifier: str, descriptor: dict[str, Any]) -> dict[str, Any]:
    return {"id": identifier, "version": "1", "content_digest": canonical_digest(descriptor)}


def visible(source: f7_corpus.Source, family_id: str, role_id: str) -> bool:
    """Visibilidade por audiência (docs/phase-7/family-memory.md): comum ou interseção dos dois eixos."""

    audience = source.audience
    if audience["area_scope"] == "common":
        return True
    families, roles = audience["target_families"], audience["target_roles"]
    return (not families or family_id in families) and (not roles or role_id in roles)


# --------------------------------------------------------------------------- catálogo


def source_metadata(source: f7_corpus.Source, raw: bytes) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "source_id": source.source_id,
        "revision": "1",
        "title": source.title,
        "knowledge_kind": "project",
        "content_locator": {"root_id": "vault", "path": source.path},
        "media_type": "text/markdown",
        "encoding": "utf-8",
        "language": "pt-BR",
        "content_digest": {"algorithm": "sha256", "value": sha256_hex(raw)},
        "size_bytes": len(raw),
        "project_scope": {"kind": "project", "project_id": PROJECT_ID},
        "authority": {
            "authority_level": "project_authoritative",
            "authority_scopes": ["orq_experiment_7f"],
            "issuer_ref": "catalog-admin",
            "assigned_at": ASSIGNED_AT,
            "evidence_ref": f"catalog-entry:{source.source_id}:1",
        },
        "instruction_scope": "project",
        "applicability": {
            "roles": [],
            "task_types": [],
            "topics": [],
            "paths": [],
            "domains": [],
            "capabilities": [],
        },
        "data_classification": "internal",
        "owner_ref": "orq-experiment-7f",
        "verified_at": ASSIGNED_AT,
        "effective_from": None,
        "expires_at": None,
        "relationships": [],
        "chunking_policy_ref": versioned_ref("heading-sections-v1", {"policy": "heading-sections-v1"}),
        "metadata_provenance": {
            "authority": {"origin": "catalog_rule", "evidence_ref": f"catalog-entry:{source.source_id}:1"},
            "audience": {"origin": "catalog_rule", "evidence_ref": f"catalog-entry:{source.source_id}:1"},
        },
        "audience": source.audience,
    }


def status_assertion(metadata: dict[str, Any]) -> dict[str, Any]:
    assertion = {
        "assertion_id": f"status-{metadata['source_id']}-1",
        "source_ref": {
            "source_id": metadata["source_id"],
            "revision": metadata["revision"],
            "content_digest": metadata["content_digest"],
        },
        "status": "active",
        "effective_at": ASSIGNED_AT,
        "issuer_ref": "catalog-admin",
        "evidence_ref": f"approval:{metadata['source_id']}:1",
    }
    assertion["assertion_digest"] = canonical_digest(assertion)
    return assertion


def build_catalog(vault_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    sources, assertions = [], []
    for source in f7_corpus.SOURCES:
        raw = source.markdown().encode("utf-8")
        metadata = source_metadata(source, raw)
        sources.append(metadata)
        assertions.append(status_assertion(metadata))
    definition = {
        "catalog_id": CATALOG_ID,
        "version": "1",
        "created_at": CREATED_AT,
        "status_cutoff": CREATED_AT,
        "catalog_policy_ref": versioned_ref("catalog-policy-7f", {"policy": "catalog-policy-7f"}),
        "parser_chunker_snapshots": [versioned_ref("markdown-heading-sections", {"chunker": "markdown-heading-sections"})],
        "vocabulary_refs": [],
        "sources": sources,
        "status_assertions": assertions,
    }
    snapshot = build_catalog_snapshot(vault_root, definition)
    if snapshot["exclusions"]:
        raise RuntimeError(f"fontes excluídas do catálogo: {snapshot['exclusions']}")
    return definition, snapshot


def unit_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index = {}
    for entry in snapshot["source_entries"]:
        source = entry["source_metadata"]
        if len(entry["units"]) != 1:
            raise RuntimeError(f"{source['source_id']}: esperava exatamente uma unidade")
        unit = entry["units"][0]
        index[source["source_id"]] = {
            "source_id": source["source_id"],
            "revision": source["revision"],
            "unit_id": unit["unit_id"],
            "content_digest": unit["content_digest"],
        }
    return index


# --------------------------------------------------------------------------- tarefas


def distractor_kind(source: f7_corpus.Source, family_id: str) -> str:
    if source.synthetic:
        return "other_family_synthetic"
    if source.families and family_id not in source.families:
        return "other_family_real"
    return "other_role_real"


def eligible_source_ids(snapshot: dict[str, Any], requirements: list[dict[str, Any]]) -> set[str]:
    """Fontes elegíveis (filtros do host) a pelo menos um requisito no regime shared_global."""

    policy = EligibilityPolicy(project_id=PROJECT_ID, as_of="2026-11-15T00:00:00Z")
    found: set[str] = set()
    for requirement in requirements:
        for key, reasons in requirement_reasons(snapshot, requirement, policy).items():
            if not reasons:
                found.add(key[0])
    return found


def build_task(
    number: int, spec: dict[str, Any], units: dict[str, dict[str, Any]], snapshot: dict[str, Any]
) -> dict[str, Any]:
    task_id = f"exp7f-t{number:02d}-{spec['slug']}"
    properties = spec["properties"]
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://orq.local/experiments/7f/{task_id}-output.schema.json",
        "title": f"Saída da tarefa {task_id}",
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }
    contract_ref = {"id": f"{task_id}-output", "version": "1", "content_digest": canonical_digest(schema)}
    output_binding = {"output_ids": ["answer"]}
    task = {
        "schema_version": "0.1",
        "task_id": task_id,
        "objective": spec["statement"],
        "task_type": "structured_answer",
        "agent_role": versioned_ref(spec["role"], {"agent_role_id": spec["role"], "experiment": "7f"}),
        "requirements": [
            {
                "requirement_id": "REQ1",
                "description": "Responder com o JSON do contrato de saída da tarefa",
                "required": True,
            }
        ],
        "dependencies": [],
        "inputs": [],
        "required_capabilities": [],
        "constraints": {
            "max_attempts": 1,
            "attempt_timeout_ms": 300000,
            "max_context_input_tokens": 4000,
            "max_output_tokens_per_attempt": 500,
        },
        "expected_output": [
            {
                "output_id": "answer",
                "artifact_type": "structured_answer",
                "description": "Resposta JSON curta e fechada",
                "content_contract_ref": contract_ref,
            }
        ],
        "knowledge_requirements": [
            {
                "knowledge_requirement_id": item["id"],
                "subject": item["subject"],
                "required": True,
                "allowed_kinds": ["project"],
                "required_authority_scopes": [],
                "source_refs": [],
                "applicability_filters": {"topics": item["topics"]},
                "freshness": None,
                "coverage_rule": "at_least_one",
                "max_context_tokens": 1500,
            }
            for item in spec["krs"]
        ],
        "evaluation_criteria": [
            {
                "criterion_id": f"CRIT{index}",
                "requirement_ids": ["REQ1"],
                "method": method,
                "pass_rule": dict(output_binding),
                "required": True,
                "verifier_ref": None,
            }
            for index, method in enumerate(
                ("output_binding_present", "digest_matches", "content_contract_valid"), start=1
            )
        ],
    }
    family_id, role_id = spec["family"], spec["role"]
    gold = [
        {"requirement_id": item["id"], **units[source_id]}
        for item in spec["krs"]
        for source_id in item["gold"]
    ]
    classes = f7_classes.CLASSES[spec["slug"]]
    gold_ids = {item["source_id"] for item in gold}
    class_ids = {name: set(classes[name]) for name in ("alternatives", "distractors", "irrelevant")}
    members = [gold_ids, *class_ids.values()]
    if sum(len(item) for item in members) != len(set().union(*members)):
        raise RuntimeError(f"{task_id}: classes não são disjuntas")
    eligible = eligible_source_ids(snapshot, task["knowledge_requirements"])
    if set().union(*members) != eligible:
        raise RuntimeError(
            f"{task_id}: classificação diverge das elegíveis: sem classe {sorted(eligible - set().union(*members))}, "
            f"não elegíveis {sorted(set().union(*members) - eligible)}"
        )

    def entries(name: str) -> list[dict[str, Any]]:
        result = []
        for source_id, reason in classes[name].items():
            entry = {**units[source_id], "reason": reason}
            if name == "distractors":
                entry["kind"] = distractor_kind(f7_corpus.BY_ID[source_id], family_id)
            result.append(entry)
        return result

    distractors = entries("distractors")
    return {
        "schema_version": "7f-task-0.1",
        "task_id": task_id,
        "number": number,
        "title": spec["title"],
        "target": {"family_id": family_id, "role_id": role_id},
        "task": task,
        "output_contract": schema,
        "knowledge": {
            "gold": gold,
            "acceptable_alternatives": entries("alternatives"),
            "distractors": distractors,
            "irrelevant": entries("irrelevant"),
            "answer_sources": classes["answer_sources"],
        },
        "acceptance": {
            "evaluator_criteria": ["CRIT1", "CRIT2", "CRIT3"],
            "deterministic_checks": spec["checks"],
            "pass_rule": "verdict do avaliador == pass E todas as deterministic_checks passam (pass@1, uma tentativa)",
        },
        "reference_answers": {"pass": spec["reference"], "contaminated": spec["contaminated"]},
    }


# --------------------------------------------------------------------------- plano de lotes


def rank_key(*parts: Any) -> str:
    return sha256_hex(":".join(str(part) for part in (BATCH_SEED, *parts)).encode("utf-8"))


def build_batch_plan(task_ids: list[str], families: dict[str, str], roles: dict[str, str]) -> dict[str, Any]:
    """6 lotes x 5 pares (tarefa, repetição), cada par com as duas condições em sequência.

    - Atribuição gulosa determinística (pares em ordem de hash): cada tarefa fica em lotes distintos nas 3
      repetições e o lote com menos pares da mesma família, depois do mesmo papel, recebe o par.
    - Dentro do lote, a ordem dos pares é embaralhada por hash com a semente fixa.
    - A condição que roda primeiro é escolhida por saldo, na prioridade: tarefa, família-alvo e global (empate: paridade).
    """

    pairs_in_order = sorted(
        ((task_id, repetition) for repetition in REPETITIONS for task_id in task_ids),
        key=lambda pair: rank_key("assign", pair[0], pair[1]),
    )
    per_batch = len(pairs_in_order) // BATCH_COUNT
    batches: list[list[tuple[str, int]]] = [[] for _ in range(BATCH_COUNT)]
    for task_id, repetition in pairs_in_order:
        open_batches = [
            index
            for index, members in enumerate(batches)
            if len(members) < per_batch and all(member[0] != task_id for member in members)
        ]
        if not open_batches:
            raise RuntimeError("não há lote livre sem repetir a tarefa")

        def load(index: int) -> tuple[int, int, int, int]:
            members = batches[index]
            return (
                sum(families[member[0]] == families[task_id] for member in members),
                sum(roles[member[0]] == roles[task_id] for member in members),
                len(members),
                index,
            )

        batches[min(open_batches, key=load)].append((task_id, repetition))
    plan = []
    ordinal = 0
    # saldo = pares que começam por shared_global menos pares que começam pela particionada
    balance: dict[str, dict[str, int]] = {"task": {}, "family": {}, "global": {}}
    for batch_index, pairs in enumerate(batches, start=1):
        pairs.sort(key=lambda pair: rank_key("batch", batch_index, pair[0], pair[1]))
        runs = []
        for position, (task_id, repetition) in enumerate(pairs):
            keys = (("task", task_id), ("family", families[task_id]), ("global", "all"))

            def imbalance(sign: int) -> tuple[int, ...]:
                return tuple(abs(balance[kind].get(name, 0) + sign) for kind, name in keys) + (
                    0 if (sign > 0) == ((position + batch_index) % 2 == 0) else 1,
                )

            sign = min((1, -1), key=imbalance)
            for kind, name in keys:
                balance[kind][name] = balance[kind].get(name, 0) + sign
            first = CONDITIONS[0] if sign > 0 else CONDITIONS[1]
            second = CONDITIONS[1] if first == CONDITIONS[0] else CONDITIONS[0]
            for condition in (first, second):
                ordinal += 1
                run_key = f"{task_id}.r{repetition}.{condition}"
                seed = int(rank_key("run-seed", run_key)[:12], 16)
                runs.append(
                    {
                        "run_key": run_key,
                        "task_id": task_id,
                        "repetition": repetition,
                        "condition": condition,
                        "pair_id": f"{task_id}.r{repetition}",
                        "pair_first_condition": first,
                        "position_in_batch": len(runs) + 1,
                        "global_ordinal": ordinal,
                        "run_seed": seed,
                    }
                )
        family_runs: dict[str, int] = {}
        for run in runs:
            family = families[run["task_id"]]
            family_runs[family] = family_runs.get(family, 0) + 1
        plan.append(
            {
                "batch": batch_index,
                "suggested_day_index": batch_index,
                "run_count": len(runs),
                "runs_per_target_family": dict(sorted(family_runs.items())),
                "runs": runs,
            }
        )
    return {
        "seed": BATCH_SEED,
        "method": "sha256(seed:parte) para ordenar; alternância determinística da condição inicial; um lote por dia",
        "batches": plan,
    }


# --------------------------------------------------------------------------- escrita e manifesto


def write_text(path: Path, text: str) -> bytes:
    raw = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def build_all(out: Path) -> dict[str, Any]:
    """Gera todo o pacote em `out` e devolve o manifesto."""

    vault = out / "vault"
    for source in f7_corpus.SOURCES:
        write_text(vault / source.path, source.markdown())
    definition, snapshot = build_catalog(vault)
    write_text(vault / "catalog-definition.json", dump_json(definition))
    write_text(vault / "catalog-snapshot.json", dump_json(snapshot))
    units = unit_index(snapshot)

    assert len(f7_tasks.TASKS) == EXPECTED_TASKS
    tasks = [build_task(number, spec, units, snapshot) for number, spec in enumerate(f7_tasks.TASKS, start=1)]
    for task in tasks:
        write_text(out / "tasks" / f"{task['task_id']}.json", dump_json(task))

    task_ids = [task["task_id"] for task in tasks]
    families = {task["task_id"]: task["target"]["family_id"] for task in tasks}
    roles = {task["task_id"]: task["target"]["role_id"] for task in tasks}
    plan = build_batch_plan(task_ids, families, roles)

    files = []
    for path in sorted(p for p in out.rglob("*") if p.is_file() and p.name != "manifest.json"):
        relative = path.relative_to(out).as_posix()
        if relative.startswith("tools/") or relative == "README.md":
            continue
        raw = path.read_bytes()
        entry: dict[str, Any] = {"path": relative, "raw_sha256": sha256_hex(raw)}
        if path.suffix == ".json":
            entry["jcs_digest"] = canonical_digest(json.loads(raw))["value"]
        files.append(entry)
    # os scripts que definem corpus, tarefas, checagens e plano também entram no digest
    for script in sorted(TOOLS.glob("*.py")):
        files.append({"path": f"tools/{script.name}", "raw_sha256": sha256_hex(script.read_bytes())})

    groups: dict[str, int] = {}
    for source in f7_corpus.SOURCES:
        groups[source.group] = groups.get(source.group, 0) + 1
    manifest = {
        "schema_version": "7f-manifest-0.1",
        "experiment_id": "7f",
        "adr": "docs/adr/0008-phase-7-subscription-hub-and-family-memory.md",
        "design": {
            "tasks": len(tasks),
            "repetitions": len(REPETITIONS),
            "conditions": list(CONDITIONS),
            "total_runs": len(tasks) * len(REPETITIONS) * len(CONDITIONS),
            "batches": BATCH_COUNT,
            "runs_per_batch": len(tasks) * len(REPETITIONS) * len(CONDITIONS) // BATCH_COUNT,
            "approved_by_owner_at": "2026-11-14",
        },
        "conditions": {
            "shared_global": "RLS relaxada: toda unidade ativa do projeto é visível a qualquer família e papel",
            "family_partitioned_rls": "RLS estrita: área comum + unidades compatíveis com (family_id, role_id) da tentativa",
        },
        "seeds": {"batch_plan": BATCH_SEED, "run_seed": "int(sha256('<seed>:run-seed:<run_key>')[:12], 16), em runs[].run_seed"},
        "catalog": {
            "catalog_id": snapshot["catalog_id"],
            "version": snapshot["version"],
            "project_id": PROJECT_ID,
            "catalog_hash": snapshot["catalog_hash"],
            "snapshot_path": "vault/catalog-snapshot.json",
            "definition_path": "vault/catalog-definition.json",
            "source_count": len(snapshot["source_entries"]),
            "sources_by_group": dict(sorted(groups.items())),
            "synthetic_source_ids": sorted(s.source_id for s in f7_corpus.SOURCES if s.synthetic),
        },
        "tasks": [
            {
                "task_id": task["task_id"],
                "path": f"tasks/{task['task_id']}.json",
                "role_id": task["target"]["role_id"],
                "family_id": task["target"]["family_id"],
                "knowledge_class_sizes": {
                    "gold": len(task["knowledge"]["gold"]),
                    "acceptable_alternatives": len(task["knowledge"]["acceptable_alternatives"]),
                    "distractors": len(task["knowledge"]["distractors"]),
                    "irrelevant": len(task["knowledge"]["irrelevant"]),
                },
            }
            for task in tasks
        ],
        "knowledge_classes": {
            "gold": "unidades esperadas; visíveis no regime particionado",
            "acceptable_alternatives": "conteúdo correto de outra família ou papel; escolher é neutro (fora do numerador e do denominador da precisão)",
            "distractors": "conteúdo inadequado ou contraditório (regras de outra família que dariam resposta errada, ou regras fictícias); só estes contam como contaminação",
            "irrelevant": "elegíveis pelo filtro de tópicos, sem resposta nem conflito; contam como ruído na precisão, não como contaminação",
            "partition_rule": "toda unidade elegível aos knowledge_requirements da tarefa no regime shared_global cai em exatamente uma classe (gold incluído)",
        },
        "metric_definitions": {
            "selection_precision": "|selecionadas ∩ gold| / |selecionadas − acceptable_alternatives|",
            "selection_recall": "|selecionadas ∩ gold| / |gold|",
            "distractor_contamination_rate": "|selecionadas ∩ distractors| / |selecionadas|",
            "cross_family_exposure_rate": "fração das selecionadas com target_families não vazio que não contém a família do run (inclui alternativas; zero por construção sob RLS)",
        },
        "files": files,
        "batch_plan": plan,
    }
    # digest do manifesto completo (arquivos, scripts, sementes e plano), exceto o próprio campo
    manifest["package_digest"] = canonical_digest(manifest)
    write_text(out / "manifest.json", dump_json(manifest))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="regenera em pasta temporária e compara com o pacote")
    arguments = parser.parse_args()
    if not arguments.check:
        build_all(PACKAGE_ROOT)
        return 0
    import tempfile

    with tempfile.TemporaryDirectory() as temporary:
        out = Path(temporary)
        build_all(out)
        diverged = []
        for path in sorted(p for p in out.rglob("*") if p.is_file()):
            relative = path.relative_to(out)
            target = PACKAGE_ROOT / relative
            if not target.is_file() or target.read_bytes() != path.read_bytes():
                diverged.append(relative.as_posix())
        if diverged:
            print("divergências:", *diverged, sep="\n  ")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
