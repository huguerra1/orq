# Contratos de execução e armazenamento append-only

Estado: proposta da Fase 6, aguardando revisão. Concretiza, para o runner sequencial e o executor simulado, os contratos conceituais aceitos em [registros de execução e avaliação](../phase-1/execution-records-contract.md). Nenhum schema executável foi escrito.

## Problema

O contrato da Fase 1 define campos conceituais, invariantes e ordem de persistência, mas deixa abertos a tecnologia de persistência, a serialização do journal e a forma concreta de cada registro. O runner não pode ser implementado sem esses detalhes, e cada escolha precisa preservar as regras já aceitas: intenção antes do efeito, histórico sem reescrita, `completed` diferente de `pass` e `unknown` diferente de zero.

## Convenções

- `schema_version` é `"0.1"` em todos os registros desta fase.
- Identificadores seguem `common.schema.json#/$defs/identifier` (letra inicial, até 128 caracteres de `[A-Za-z0-9._:-]`).
- Datas seguem `utcTimestamp`. No executor simulado, todas as datas vêm do relógio injetado, para que o journal seja reproduzível.
- Digest é `{"algorithm": "sha256", "value": "<64 hex>"}` sobre bytes JCS (RFC 8785), conforme a [ADR 0001](../adr/0001-contract-tooling-and-canonical-json.md).
- **`record_hash`**: registros autocontidos carregam `record_hash = canonical_digest(registro sem record_hash)`, a mesma convenção já usada por PlanningRecord e RoutingDecision.
- **Referência a registro**: `versionedRef` com `id` igual ao ID do registro, `version` igual ao `schema_version` do registro e `content_digest` igual ao `record_hash`. Para ContextManifest, que não tem `record_hash` no schema atual, `content_digest` é `canonical_digest` do manifesto inteiro.
- **Referência a artefato**: `common.schema.json#/$defs/artifactRef` (`artifact_id` + `content_digest`). O descritor completo da Fase 1 aparece uma única vez, no evento `artifact_ingested`.
- Dinheiro é decimal em texto (`"0.20"`) com `currency`. Ausência de valor é `null` com `status: "unavailable"`, nunca `"0"`.
- Nos exemplos, `H(x)` abrevia um digest completo, como na [revisão cruzada](../phase-1/cross-review.md).

### IDs determinísticos propostos

| Registro | Formato | Exemplo |
| --- | --- | --- |
| Tentativa | `attempt-<task_id>-<attempt_number>` | `attempt-T3-2` |
| Intenção de despacho | `dispatch-<task_id>-<attempt_number>` | `dispatch-T3-2` |
| Avaliação local | `evaluation-<task_id>-<attempt_number>-<evaluation_number>` | `evaluation-T3-1-1` |
| Avaliação global | `evaluation-workflow-<evaluation_number>` | `evaluation-workflow-1` |
| Reserva | `reservation-<scope_id>` | `reservation-attempt-T3-2` |
| Evento | `event-<global_sequence>` | `event-42` |

IDs nunca são reutilizados dentro do run. A `dispatch_idempotency_key` é `<run_id>/<dispatch_intent_id>` e não muda durante reconciliação.

## RunIntent

Primeiro registro do run, persistido antes de planejamento. Fecha a lacuna da Fase 4, em que PlanningRequest exige um `run_id` já registrado.

| Campo | Tipo | Obrigatório | Semântica |
| --- | --- | --- | --- |
| schema_version | `"0.1"` | Sim | |
| run_id | identifier | Sim | Único no armazenamento |
| project_id | identifier | Sim | Deve coincidir com o PlanningRequest |
| objective | nonEmptyText | Sim | Objetivo solicitado |
| requested_by | objeto `{kind, ref}` | Sim | `kind`: `human` ou `automation` |
| experiment_metadata | objeto | Sim | Grupo, seed e tags; pode ser `{}` |
| created_at | utcTimestamp | Sim | |
| idempotency_key | texto | Sim | Repetir o mesmo pedido não cria outro run |
| record_hash | digest | Sim | |

## RunManifest

Fechado uma vez, ao final da preparação. Imutável depois de fechado. Segue a tabela da Fase 1, com estes ajustes concretos:

| Campo | Tipo | Obrigatório em `ready` | Semântica e ajuste da Fase 6 |
| --- | --- | --- | --- |
| schema_version | `"0.1"` | Sim | |
| run_id | identifier | Sim | Igual ao RunIntent |
| run_intent_ref | versionedRef | Sim | Novo: liga o manifesto ao RunIntent |
| manifest_status | enum | Sim | `ready`, `planning_failed` ou `cancelled_before_ready` |
| created_at, closed_at | utcTimestamp | Sim | |
| project_id | identifier | Sim | O `project_ref` versionado da Fase 1 fica para quando houver catálogo de projetos |
| objective_snapshot | `{objective, objective_hash}` | Sim | |
| planning_record_ref | versionedRef ou null | Sim | Realiza o `validation_report_ref` da Fase 1: o PlanningRecord já contém o relatório de validação |
| workflow_snapshot_ref | versionedRef ou null | Sim | `content_digest` = `canonical_digest(workflow)`, igual a `proposed_workflow_ref` do PlanningRecord aceito |
| execution_policy_snapshot_ref | versionedRef ou null | Sim | Referência opaca; mesma usada em todo RoutingRequest do run |
| resolved_limits | ResolvedLimits ou null | Sim | Ver abaixo |
| budget_snapshot | BudgetSnapshot ou null | Sim | Ver abaixo |
| routing_catalog_ref | versionedRef ou null | Sim | RoutingTargetCatalog (`content_digest` = `catalog_hash`) |
| routing_policy_ref | versionedRef ou null | Sim | RoutingPolicy usada em todas as decisões |
| knowledge_catalog_snapshot_ref | versionedRef ou null | Sim | Catálogo fixo do run |
| knowledge_status_assertions | lista de CatalogStatusAssertion | Sim | Novo: asserções de status recebidas no pedido do run, cada uma verificada por `assertion_digest` como em `build_catalog_snapshot`; pode ser vazia. Alimenta o check `knowledge_status` |
| external_input_bindings | lista `{input_id, artifact}` | Sim | Todos verificados em `ready` |
| executor_snapshot | objeto | Sim | `executor_ref`, versão do adaptador e, no simulado, `script_ref` com digest |
| evaluator_snapshot | objeto | Sim | Avaliador determinístico, versão e métodos suportados |
| orchestrator_snapshot | objeto | Sim | Versão do pacote, configuração e relógio (`injected` ou `system`) |
| scheduling_policy | `{max_concurrency: 1, tie_break: "workflow_task_order"}` | Sim | |
| experiment_metadata | objeto | Sim | Copiado do RunIntent |
| preparation_coverage | objeto | Sim | Etapas concluídas, ausentes e motivo |
| preparation_usage_refs, preparation_error_refs | listas | Sim | Consumo e falhas antes do fechamento |
| record_hash | digest | Sim | Equivale ao `manifest_hash` da Fase 1 |

Fora de `ready`, campos não resolvidos ficam `null` e aparecem em `preparation_coverage`, sem suposições.

### ResolvedLimits

Subconjunto finito de LimitsPolicy necessário ao runner. Todos os campos são obrigatórios e inteiros positivos, salvo indicação.

| Campo | Semântica |
| --- | --- |
| max_concurrency | Constante `1` |
| max_attempts | Inclui a primeira tentativa |
| attempt_timeout_ms | Execução técnica de uma tentativa |
| max_run_duration_ms | Desde `run_intent_recorded` |
| max_evaluations_per_attempt | Inclui a primeira avaliação |
| evaluation_timeout_ms | Por avaliação, local ou global |
| reconciliation_timeout_ms | Por reconciliação, limitado pelo tempo restante do run |
| retry_policy | Novo, objeto com três booleanos: `on_evaluation_fail`, `on_retryable_error`, `on_confirmed_timeout` |
| sources | Lista `{field, origin_ref}` com a camada de onde veio cada valor |

`retry_policy` não existe na Fase 1, que fala apenas em "retry autorizado". Ele torna explícita a autorização e precisa de aprovação.

### BudgetSnapshot

| Campo | Semântica |
| --- | --- |
| currency | Moeda única do run no MVP |
| max_cost | Decimal em texto ou `null` (sem teto financeiro) |
| cap_mandatory | `true` quando `max_cost` é obrigatório |
| unknown_cost_action | `reject` ou `admit_with_estimate` |
| cost_estimates | Lista `{scope, subject_ref, amount, currency, method, source_ref}`; estimativa conservadora por tentativa ou avaliação de um destino/avaliador |

Com `cap_mandatory: true`, uma operação sem custo conhecido e sem entrada em `cost_estimates` não é admitida, independentemente de `unknown_cost_action`.

### Exemplo

```json
{
  "schema_version": "0.1",
  "run_id": "run-demo-001",
  "run_intent_ref": {"id": "run-demo-001", "version": "0.1", "content_digest": {"algorithm": "sha256", "value": "H(run-intent)"}},
  "manifest_status": "ready",
  "created_at": "2026-11-10T12:00:00Z",
  "closed_at": "2026-11-10T12:00:01Z",
  "project_id": "project-demo",
  "objective_snapshot": {"objective": "Implementar autenticação", "objective_hash": {"algorithm": "sha256", "value": "H(objective)"}},
  "planning_record_ref": {"id": "planning-plan-001", "version": "0.1", "content_digest": {"algorithm": "sha256", "value": "H(planning-record)"}},
  "workflow_snapshot_ref": {"id": "workflow-feature", "version": "1", "content_digest": {"algorithm": "sha256", "value": "H(workflow)"}},
  "execution_policy_snapshot_ref": {"id": "policy-default", "version": "1", "content_digest": {"algorithm": "sha256", "value": "H(policy)"}},
  "resolved_limits": {
    "max_concurrency": 1,
    "max_attempts": 2,
    "attempt_timeout_ms": 60000,
    "max_run_duration_ms": 600000,
    "max_evaluations_per_attempt": 2,
    "evaluation_timeout_ms": 20000,
    "reconciliation_timeout_ms": 15000,
    "retry_policy": {"on_evaluation_fail": true, "on_retryable_error": true, "on_confirmed_timeout": true},
    "sources": [{"field": "max_attempts", "origin_ref": {"id": "policy-default", "version": "1", "content_digest": {"algorithm": "sha256", "value": "H(policy)"}}}]
  },
  "budget_snapshot": {
    "currency": "USD",
    "max_cost": "1.00",
    "cap_mandatory": true,
    "unknown_cost_action": "reject",
    "cost_estimates": [
      {"scope": "attempt", "subject_ref": {"id": "target-sim-a", "version": "1", "content_digest": {"algorithm": "sha256", "value": "H(target-sim-a)"}}, "amount": "0.30", "currency": "USD", "method": "fixture_ceiling", "source_ref": {"id": "sim-script", "version": "1", "content_digest": {"algorithm": "sha256", "value": "H(script)"}}}
    ]
  },
  "scheduling_policy": {"max_concurrency": 1, "tie_break": "workflow_task_order"},
  "record_hash": {"algorithm": "sha256", "value": "H(manifest)"}
}
```

O exemplo omite campos repetitivos (catálogos, bindings, snapshots e coverage). Os valores numéricos são fictícios, não defaults.

## DispatchIntent

Intenção imutável de um despacho lógico. É gravada no mesmo evento que o cabeçalho da tentativa (`dispatch_requested`) e nunca é recriada na reconciliação.

| Campo | Tipo | Obrigatório | Semântica |
| --- | --- | --- | --- |
| schema_version | `"0.1"` | Sim | |
| dispatch_intent_id | identifier | Sim | Único no run |
| dispatch_idempotency_key | texto | Sim | `<run_id>/<dispatch_intent_id>`; enviado ao executor quando ele aceitar chave |
| run_id, task_id | identifier | Sim | |
| attempt_id, attempt_number | identifier, inteiro ≥ 1 | Sim | Iguais ao cabeçalho |
| workflow_ref | versionedRef | Sim | |
| target_ref | versionedRef | Sim | Igual a `effective_selection` da RoutingDecision e a `target_ref` do ContextManifest |
| executor_ref | versionedRef | Sim | Do destino selecionado |
| routing_decision_ref | versionedRef | Sim | `content_digest` = `record_hash` da decisão |
| context_manifest_ref | versionedRef | Sim | `content_digest` = digest do manifesto |
| bundle_hash | digest | Sim | Igual ao `bundle_hash` do manifesto e recalculado sobre os bytes enviados |
| workspace_base_ref | artifactRef | Sim | Snapshot limpo inicial; no simulado, diretório vazio identificado |
| input_bindings | lista | Sim | `{input_id, artifact, basis}`; `basis` é `external_input_binding` ou `outputs_accepted` com o `event_id` correspondente |
| admission_checks | lista não vazia | Sim | `{control, status, observed_at, evidence_refs}`; todos `passed` |
| budget_reservation_ids | lista | Sim | Reservas de execução e avaliação já persistidas |
| attempt_timeout_ms | inteiro ≥ 1 | Sim | Efetivo para esta tentativa |
| deadline_at | utcTimestamp | Sim | `min(created_at + attempt_timeout_ms, fim do run)` |
| created_at | utcTimestamp | Sim | |
| record_hash | digest | Sim | |

Controles mínimos em `admission_checks`: `context_integrity` (bundle recalculado igual ao manifesto e manifesto ligado à decisão), `knowledge_status` (nenhuma fonte usada revogada no instante da admissão), `attempts_remaining`, `run_time_remaining`, `budget` e `inputs_accepted`. É aqui que ficam os checks que dependem do bundle, porque `route_and_finalize` exige `preflight_checks` antes da materialização (ver [README](README.md#lacunas-encontradas-no-código-atual)).

O check `knowledge_status` não pode usar apenas `source_entries` do catálogo: `build_catalog_snapshot` só inclui ali fontes `active` no `status_cutoff`, então uma fonte selecionada nunca aparece revogada nele. O check considera, para cada `(source_id, revision, content_digest)` usada no ContextManifest, a união de `status_assertions` do catálogo e de `RunManifest.knowledge_status_assertions` com `effective_at` posterior ao `status_cutoff` e menor ou igual ao instante do check (relógio do ORQ, injetado no simulado). A asserção mais recente decide, com a mesma regra de empate de `build_catalog_snapshot`: status diferente de `active`, ou conflito no mesmo instante, falha o check com `knowledge_revoked`. Asserções que cheguem depois do fechamento do manifesto ficam fora da Fase 6.

## AttemptRecord

Agregado formado por um cabeçalho imutável e eventos append-only, como na Fase 1.

### AttemptHeader

| Campo | Tipo | Obrigatório | Semântica |
| --- | --- | --- | --- |
| schema_version | `"0.1"` | Sim | |
| attempt_id | identifier | Sim | |
| run_id, task_id | identifier | Sim | |
| workflow_ref | versionedRef | Sim | |
| attempt_number | inteiro ≥ 1 | Sim | Contíguo por tarefa; criação consome `max_attempts` |
| previous_attempt_id | identifier ou null | Sim | |
| routing_decision_ref, context_manifest_ref | versionedRef | Sim | Iguais ao DispatchIntent |
| execution_target_snapshot | objeto | Sim | `target_ref`, `model_ref`, `provider_ref`, `runtime_ref`, `executor_ref`, `execution_config_ref` copiados do catálogo. `model_ref` é o modelo solicitado; o catálogo não tem `requested_model_id` e o modelo resolvido só é conhecido depois, no evento `result_collected` |
| effective_policy_snapshot_ref | versionedRef | Sim | No MVP, `execution_policy_snapshot_ref` + `resolved_limits` do manifesto |
| workspace_base_ref | artifactRef | Sim | |
| input_bindings | lista | Sim | Iguais ao DispatchIntent |
| budget_reservation_ids | lista | Sim | |
| dispatch_intent_id, dispatch_idempotency_key | identifier, texto | Sim | |
| deadline_at | utcTimestamp | Sim | |
| created_at | utcTimestamp | Sim | |
| record_hash | digest | Sim | |

A tentativa nasce `queued`. Erro anterior ao evento `dispatch_requested` não cria tentativa e não consome `max_attempts`.

### Eventos da tentativa

| event_type | Origem | Payload principal | Efeito no estado |
| --- | --- | --- | --- |
| `dispatch_requested` | Orchestrator | `attempt_header`, `dispatch_intent` | Cria o agregado em `queued` |
| `dispatch_acknowledged` | Executor | `external_call_id` ou null | Nenhum |
| `execution_started` | Executor | instante observado | `queued → running` via `state_transition` |
| `deadline_exceeded` | Relógio | `deadline_at`, instante | Nenhum; dispara `cancel` |
| `cancel_requested` | Orchestrator/humano | motivo | Nenhum |
| `termination_confirmed` | Executor | causa: `finished`, `failed`, `timeout`, `cancel` | Habilita transição terminal |
| `reconciliation_requested` | Orchestrator | chave, `external_call_id`, prazo | Nenhum |
| `reconciliation_observed` | Executor | `outcome` (`found_running`, `found_finished`, `found_failed`, `found_stopped`, `not_found`, `unknown`) e evidências | Pode habilitar transição |
| `result_collected` | Executor | `requested_model_id` (string ou null, como o runtime informou), `resolved_model_id` (string ou null), `provider_context_coverage` | Nenhum; gravado após `collect` e antes de `artifact_ingested`. Null não é inferido a partir de `model_ref` |
| `artifact_ingested` | Orchestrator | ArtifactRef completo da Fase 1 | Nenhum |
| `output_bound` | Orchestrator | `output_id`, `artifact` | Nenhum |
| `usage_attached` | Executor/Orchestrator | UsageRecord | Nenhum |
| `error_attached` | Executor/Orchestrator | ErrorRecord | Nenhum |
| `state_transition` | Orchestrator | `from`, `to`, `reason`, `evidence_event_ids` | Muda o estado projetado |
| `late_observation` | Executor | observação após estado terminal | Nenhum; nunca reabre |

`completed` exige `termination_confirmed` com causa `finished`, todos os `output_bound` obrigatórios e nenhum `error_attached` terminal. Não implica `pass`.

### Projeção AttemptState

Reconstruída do journal até um cutoff: `state`, `produced_artifacts`, `output_bindings`, `undeclared_artifacts`, `usage_record_ids`, `error_record_ids`, `external_call_id`, `termination_certainty`, `resolved_model_id` (de `result_collected`; null enquanto ausente) e `as_of_global_sequence`. A projeção não é fonte de auditoria.

## EvaluationReport

Mesmos campos da Fase 1, com restrições desta fase:

| Campo | Restrição na Fase 6 |
| --- | --- |
| evaluation_id, evaluation_number | Alocados no evento `evaluation_requested`, antes de avaliar; isso consome `max_evaluations_per_attempt` |
| scope | `{kind: "task_attempt", task_id, attempt_id}` ou `{kind: "workflow"}` |
| subject_bindings | Lista não vazia `{output_id, artifact}`; para workflow, `output_id` é o ID da saída final |
| patch_application_refs | Sempre vazia nesta fase |
| criteria_snapshot | `evaluation_criteria` da TaskSpec ou do WorkflowSpec, com digest |
| evaluator_snapshot | `{kind: "deterministic", evaluator_ref, version, supported_methods}` |
| context_manifest_ref | Sempre `null`; o avaliador determinístico não recebe contexto |
| execution_status | `completed`, `failed`, `timed_out`, `cancelled` ou `indeterminate` |
| criterion_results | Um por critério do snapshot, inclusive opcionais |
| verdict | `pass`, `fail`, `inconclusive` ou `not_run`, pelas sete regras de agregação da Fase 1 |
| usage_record_ids, error_record_ids | O avaliador local não chama provedor e não produz UsageRecord de custo, como o planner `fixed_template`; a lista vazia não significa custo reportado zero |
| limitations, unknowns | Obrigatórias; podem ser vazias |
| record_hash | Equivale ao `report_hash` da Fase 1 |

### Métodos determinísticos suportados

| method | pass_rule | Resultado |
| --- | --- | --- |
| `output_binding_present` | `{"output_ids": [...]}` | `pass` se todos estão vinculados; senão `fail` |
| `digest_matches` | `{"output_ids": [...]}` | Bytes relidos do armazenamento têm o digest do binding; divergência é `fail` e gera ErrorRecord `integrity` |
| `content_contract_valid` | `{"output_ids": [...]}` | JSON do artefato valida contra o schema local registrado para `content_contract_ref`; contrato não registrado é `not_run` |
| `simulated_verdict` | `{"script_key": "..."}` | Lê do roteiro (`verifier` da tentativa ou `workflow_verifier`) o veredicto indexado por esse `script_key` e pelo `evaluation_number`; serve aos testes de reprovação e inconclusão |

Método fora desta lista produz `not_run` para o critério. Se o critério for obrigatório, o veredicto global é no máximo `inconclusive`, nunca `pass`.

### Exemplo de relatório reprovado

```json
{
  "schema_version": "0.1",
  "evaluation_id": "evaluation-T3-1-1",
  "evaluation_number": 1,
  "run_id": "run-demo-001",
  "scope": {"kind": "task_attempt", "task_id": "T3", "attempt_id": "attempt-T3-1"},
  "subject_bindings": [
    {"output_id": "code_patch", "artifact": {"artifact_id": "artifact-T3-1-code_patch", "content_digest": {"algorithm": "sha256", "value": "H(P1)"}}}
  ],
  "patch_application_refs": [],
  "criteria_snapshot": {"criteria": ["..."], "criteria_hash": {"algorithm": "sha256", "value": "H(criteria-T3)"}},
  "evaluator_snapshot": {"kind": "deterministic", "evaluator_ref": {"id": "evaluator-deterministic", "version": "0.1", "content_digest": {"algorithm": "sha256", "value": "H(evaluator)"}}, "supported_methods": ["output_binding_present", "digest_matches", "content_contract_valid", "simulated_verdict"]},
  "context_manifest_ref": null,
  "started_at": "2026-11-10T12:03:00Z",
  "completed_at": "2026-11-10T12:03:00Z",
  "execution_status": "completed",
  "criterion_results": [
    {"criterion_id": "patch-present", "required": true, "requirement_ids": ["R1"], "method": "output_binding_present", "status": "pass", "evidence_refs": [], "observations": [], "error_record_ids": []},
    {"criterion_id": "behavior-ok", "required": true, "requirement_ids": ["R2"], "method": "simulated_verdict", "status": "fail", "evidence_refs": [], "observations": ["roteiro: fail"], "error_record_ids": []}
  ],
  "verdict": "fail",
  "evidence_refs": [],
  "usage_record_ids": [],
  "error_record_ids": [],
  "limitations": [],
  "unknowns": [],
  "record_hash": {"algorithm": "sha256", "value": "H(evaluation-T3-1-1)"}
}
```

## Subconjuntos mínimos de UsageRecord, ErrorRecord e reservas

### UsageRecord

Campos da Fase 1: `usage_id`, `scope_type` (`planning`, `routing`, `attempt`, `evaluation`), `scope_ref`, `provider_ref` ou null, `observed_at`, `recorded_at`, `measurements`, `cost_lines`, `coverage`, `supersedes_usage_id` ou null e `record_hash`.

Cada `cost_line` tem `amount` (texto ou `null`), `currency`, `status` (`reported`, `estimated`, `unavailable`), `pricing_ref` ou null e `method`. `status: "unavailable"` exige `amount: null`. O validador rejeita `amount: "0"` com `status: "unavailable"`.

Agregação no RunSummary:

- `known_cost`: soma de `reported` e `estimated` da cadeia `supersedes` mais recente, separadas por status;
- `unavailable_count`: número de escopos folha sem custo;
- `total_cost_exact`: `true` somente se `unavailable_count` é zero e não há `estimated`.

Planner `fixed_template` e Router `fixed`/`rules` não chamam provedores e não produzem UsageRecord; a ausência é registrada como lista vazia, como já fazem PlanningRecord e RoutingDecision.

### ErrorRecord

Campos da Fase 1, com `category`, `code`, `message` sanitizada, `retryability` (`retryable`, `non_retryable`, `unknown`) e `termination_certainty` (`confirmed`, `not_terminated`, `unknown`). Códigos iniciais do runner: `dispatch_failed`, `dispatch_not_executed`, `execution_failed`, `timeout`, `cancelled`, `output_missing`, `output_contract_mismatch`, `integrity_mismatch`, `evaluator_failed`, `budget_unknown`, `budget_exceeded`, `no_eligible_target`, `context_rejected`, `knowledge_revoked`, `idempotency_conflict`, `journal_integrity_error`.

### Reservas

Eventos do agregado `run`: `budget_reserved` (`reservation_id`, escopo, `amount`, `currency`, `method`), `budget_settled` (`reservation_id`, `usage_ids`) e `budget_released` (`reservation_id`, motivo). Reserva de tentativa e da sua avaliação obrigatória são gravadas antes de `dispatch_requested`. Liquidação substitui a reserva; não soma.

## JournalEvent

Envelope único de todos os fatos e intenções do run.

| Campo | Tipo | Obrigatório | Semântica |
| --- | --- | --- | --- |
| schema_version | `"0.1"` | Sim | |
| event_id | identifier | Sim | `event-<global_sequence>` |
| run_id | identifier | Sim | |
| global_sequence | inteiro ≥ 1 | Sim | Contíguo no arquivo; igual ao número da linha |
| aggregate_type | enum | Sim | `run`, `task`, `attempt` ou `evaluation` |
| aggregate_id | identifier | Sim | `run_id`, `task_id`, `attempt_id` ou `evaluation_id` |
| aggregate_sequence | inteiro ≥ 1 | Sim | Contíguo por agregado |
| event_type | identifier | Sim | Vocabulário fechado abaixo |
| recorded_at | utcTimestamp | Sim | Relógio do ORQ |
| occurred_at | utcTimestamp ou null | Sim | Relógio externo, quando informado |
| actor | `{kind, ref}` | Sim | `kind`: `orchestrator`, `executor`, `evaluator`, `clock` ou `human` |
| correlation_id | identifier | Sim | Liga eventos do mesmo fluxo (por exemplo, o `attempt_id`) |
| causation_event_id | identifier ou null | Sim | |
| idempotency_key | texto ou null | Sim | Obrigatório para observações externas |
| payload | objeto | Sim | Schema específico por `event_type` |
| previous_event_hash | digest ou null | Sim | `event_hash` da linha anterior; `null` só em `global_sequence` 1 |
| event_hash | digest | Sim | `canonical_digest` do evento sem `event_hash` |

O agregado `task` não aparece na lista da Fase 1 (`run`, `attempt`, `evaluation`, `approval`). Ele é proposto aqui porque as transições da tarefa precisam de sequência própria. `approval` fica reservado e sem uso nesta fase.

### Vocabulário de eventos

| aggregate_type | event_type |
| --- | --- |
| run | `run_intent_recorded`, `planning_recorded`, `run_manifest_closed`, `state_transition`, `cancel_requested`, `deadline_exceeded`, `budget_reserved`, `budget_settled`, `budget_released`, `usage_attached`, `error_attached`, `journal_tail_discarded`, `run_closed` |
| task | `state_transition`, `knowledge_selection_recorded`, `routing_decision_recorded`, `context_manifest_recorded`, `admission_rejected`, `outputs_accepted` |
| attempt | Tabela da seção [Eventos da tentativa](#eventos-da-tentativa) |
| evaluation | `evaluation_requested`, `evaluation_report_recorded`, `late_observation` |

O agregado `evaluation` não tem máquina de estados própria na Fase 6: o resultado fica em `EvaluationReport.execution_status` e `verdict`, e as transições correspondentes são da tarefa (TK16–TK22). Por isso ele não recebe `state_transition`, e o Invariante 10 vale para `run`, `task` e `attempt`.

`run_closed` registra o cutoff publicado (`as_of_global_sequence` e `event_hash`) e a projeção RunSummary com seu digest. Eventos posteriores ao cutoff são `late_observation` ou `usage_attached` e não alteram o resumo publicado.

### Exemplo de linha

```json
{"actor":{"kind":"orchestrator","ref":"orq-runner"},"aggregate_id":"attempt-T3-2","aggregate_sequence":1,"aggregate_type":"attempt","causation_event_id":"event-57","correlation_id":"attempt-T3-2","event_hash":{"algorithm":"sha256","value":"H(event-58)"},"event_id":"event-58","event_type":"dispatch_requested","global_sequence":58,"idempotency_key":"run-demo-001/dispatch-T3-2","occurred_at":null,"payload":{"attempt_header":{"...":"..."},"dispatch_intent":{"...":"..."}},"previous_event_hash":{"algorithm":"sha256","value":"H(event-57)"},"recorded_at":"2026-11-10T12:04:00Z","run_id":"run-demo-001","schema_version":"0.1"}
```

A linha está em JCS: chaves ordenadas, sem espaços. JCS escapa quebras de linha dentro de strings, então cada evento ocupa exatamente uma linha.

## Armazenamento append-only mínimo

### Alternativas

| Alternativa | Vantagens | Limitações |
| --- | --- | --- |
| Um JSON mutável por registro | Simples | Sobrescreve fatos; viola append-only |
| JSONL por run, encadeado por digest | Sem dependência nova; legível; mesma canonicalização JCS das fases anteriores; atomicidade por linha | Um escritor por run; consultas exigem varrer o arquivo |
| SQLite (biblioteca padrão) | Transações e índices | Bytes do banco não são canônicos; integridade do histórico precisaria de hash por linha mesmo assim; mais superfície para o MVP |
| Serviço de event store | Recursos completos | Serviço externo, contrário ao MVP local |

Recomendação: **JSONL por run, encadeado por digest**. SQLite continua candidato para a Fase 11 (concorrência), quando houver mais de um escritor.

### Layout

```text
<store_root>/
  runs/<run_id>/
    run.lock                    criado com exclusividade; um escritor por run
    journal.jsonl               eventos JCS, um por linha
    artifacts/sha256/<hex>      bytes de artefatos, endereçados por conteúdo
    workspaces/<attempt_id>/    workspace isolado da tentativa (vazio no simulado)
    discarded/                  cauda truncada preservada para auditoria
```

`<store_root>` vem da CLI. Criar um run cujo diretório já existe é erro (`run_exists`), mantendo a regra de escrita exclusiva da CLI atual.

### Protocolo de escrita

1. Montar o evento com `global_sequence` = última + 1, `aggregate_sequence` esperado e `previous_event_hash` da última linha.
2. Calcular `event_hash` e serializar em JCS seguido de `\n`.
3. Gravar a linha com uma única escrita em modo append, depois `flush` e `fsync` do arquivo. Na criação do arquivo, `fsync` também do diretório.
4. Somente depois do `fsync` executar o efeito correspondente (despacho, cancelamento, avaliação).

Uma intenção cuja gravação falhou não gera efeito. É assim que o runner cumpre "se a persistência não confirmar a intenção, não chamar o runtime".

### Artefatos

Bytes são gravados em arquivo temporário no mesmo diretório, sincronizados e renomeados para `artifacts/sha256/<hex>`. Se o destino já existe, o runner relê e confirma o digest em vez de sobrescrever. `locator` do ArtifactRef é `orq-store:sha256/<hex>`. O evento `artifact_ingested` só é gravado depois do rename.

### Ordenação

- `global_sequence` define a ordem causal do run. `recorded_at` e `occurred_at` nunca reordenam eventos.
- `aggregate_sequence` permite validar transições por agregado com "sequência esperada", mesmo com concorrência um.
- Um evento só pode citar em `causation_event_id` um evento anterior.

### Integridade e abertura

Ao abrir um run, o leitor verifica, linha a linha: JSON válido, bytes iguais à forma JCS, schema do envelope e do payload, `event_hash`, `previous_event_hash`, `global_sequence` contíguo e `aggregate_sequence` contíguo.

- **Cauda truncada:** se apenas a última linha estiver incompleta (sem `\n` final ou JSON inválido), ela nunca foi confirmada por `fsync` e portanto nenhum efeito dependeu dela. Os bytes são movidos para `discarded/` e o primeiro evento novo é `journal_tail_discarded` com o digest desses bytes.
- **Qualquer outra divergência:** `journal_integrity_error`. O runner recusa continuar e não tenta reparo automático.
- **Digest final:** o `event_hash` da última linha identifica o journal inteiro. Dois runs simulados com mesmo roteiro, fixtures e relógio devem produzir o mesmo digest final.

### Idempotência

- Mesmo `idempotency_key` com mesmo `payload` (comparado por digest JCS): o append devolve o evento existente e não grava outro.
- Mesmo `idempotency_key` com `payload` diferente: `idempotency_conflict`, registrado como ErrorRecord no agregado `run`, e o run vai para `stopping`.
- A `dispatch_idempotency_key` é reutilizada em `reconciliation_requested`; nunca gera outro `dispatch_requested`.

## Relação com os contratos existentes

| Contrato existente | Relação |
| --- | --- |
| TaskSpec | `task_id`, `max_attempts` local, `expected_output` (output bindings) e `evaluation_criteria` (criteria_snapshot) |
| WorkflowSpec | `workflow_snapshot_ref`; ordem de `tasks` para desempate; `outputs` e `evaluation_criteria` da avaliação global; `external_inputs` para `external_input_bindings` |
| PlanningRecord | `planning_record_ref`; só `status: accepted` permite `manifest_status: ready` |
| RoutingRequest | Montado pelo runner com `candidate_attempt_number` = próximo `attempt_number` |
| RoutingDecision | `routing_decision_ref`; só `effective_selection` não nulo permite seguir, inclusive quando preenchido por fallback com `decision_status` `low_confidence`, `invalid_response` ou `provider_error`; `effective_selection.selected_target_id` define `target_ref` |
| KnowledgeSelectionRecord | Gravado no evento `knowledge_selection_recorded`; usa o catálogo do RunManifest |
| ContextManifest | `context_manifest_ref`; `routing_decision_ref` dele precisa ter o `record_hash` da decisão; `candidate_attempt_number` igual ao `attempt_number` |
| ArtifactRef | Referências mínimas; descritor completo em `artifact_ingested` |

## Invariantes

1. Nenhum efeito do executor ocorre antes do `fsync` do evento de intenção correspondente.
2. Cada `dispatch_intent_id` aparece em exatamente um `dispatch_requested`.
3. `attempt_number` é contíguo por tarefa e nunca excede `max_attempts`.
4. `target_ref` é igual na RoutingDecision, no ContextManifest, no DispatchIntent e no cabeçalho.
5. `bundle_hash` do DispatchIntent é igual ao do manifesto e aos bytes entregues ao executor.
6. EvaluationReport referencia exatamente os digests dos output bindings da tentativa.
7. `outputs_accepted` exige EvaluationReport `pass` com os mesmos bindings.
8. Custo `unavailable` tem `amount: null` e nunca entra como zero em agregação.
9. O journal só cresce; nenhuma linha confirmada é alterada ou removida.
10. Toda transição de estado tem `from` igual ao estado projetado imediatamente antes.

## Decisões abertas

- `retry_policy` dentro de `resolved_limits`.
- `knowledge_status_assertions` no RunManifest como fonte do check `knowledge_status`.
- Evento `result_collected` para o modelo resolvido, e outcome `found_stopped` na reconciliação.
- Agregado `task` no journal.
- Registro dos checks dependentes do bundle no DispatchIntent em vez da RoutingDecision.
- JSONL encadeado em vez de SQLite.
- Tamanho máximo de uma linha do journal e de um artefato simulado.
- Política de lock: o MVP recusa run com `run.lock` existente; remoção de lock órfão é ação manual documentada.
