# Cenários de aceitação da Fase 6

Estado: especificação aceita em 2026-11-10 ([ADR 0007](../adr/0007-phase-6-runner-and-simulated-executor.md)). Os 63 cenários S01–S63 ainda não são testes do runner executados. O 6A implementa contratos e tem 276 testes aprovados de schemas, fixtures, hashes e igualdade local de critérios globais; a automação dos cenários segue nos incrementos 6B–6E, somente com executor simulado, avaliador determinístico, relógio injetado e fixtures locais.

## Estratégia

Os cenários seguem a matriz da [Fase 1](../phase-1/acceptance-cases.md), com as colunas Dado, Quando e Então separadas para deixar explícitos o roteiro do executor simulado e a evidência esperada no journal. A coluna "Base" liga cada cenário aos casos já aceitos.

Convenções:

- "Roteiro" é o roteiro do executor simulado descrito em [runner-state-machine.md](runner-state-machine.md#executor-simulado).
- Salvo indicação, `max_attempts` = 2, `max_evaluations_per_attempt` = 2, `retry_policy` com os três booleanos `true` e sem teto financeiro obrigatório. Valores fictícios, não defaults.
- "Sem consumo de tentativa" significa nenhum `dispatch_requested` para aquele `attempt_number`.
- "Journal íntegro" significa cadeia de `event_hash` válida e sequências contíguas.
- Criação é implícita em `run_intent_recorded` (run `created`), `dispatch_requested` (tentativa `queued`) e manifesto `ready` (tarefas `pending`); só mudanças posteriores emitem `state_transition`.
- Entradas externas são `artifact_ingested` em `run` antes de `ready`; artefatos/erros de avaliação ficam em `evaluation`, e erros de admissão podem ficar em `task`.
- Sem estimativa nem teto obrigatório, a reserva admitida tem `status: unavailable`, `amount: null`; reserva da avaliação local é liberada após término confirmado, sem UsageRecord fictício. Reconciliação reutiliza a chave do despacho no payload e usa chave própria no envelope.
- Os IDs usam o prefixo S, ainda livre no projeto (C, P, R, E, A, X e K já estão em uso).

## Caminho feliz

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| S01 | Workflow mínimo com uma tarefa; roteiro `default` | O run é executado | Eventos em ordem: `run_intent_recorded`, `planning_recorded`, `run_manifest_closed`, seleção, decisão, contexto, reservas, `dispatch_requested`, término, `output_bound`, `evaluation_report_recorded` (`pass`), `outputs_accepted`, avaliação global `pass`, `run_closed`. Run `succeeded` | C02 |
| S02 | Workflow T1–T5; roteiro reprova T3/1 e aprova T3/2 | O run é executado | T3 tem duas tentativas, duas RoutingDecisions e dois ContextManifests; T3/2 parte da base limpa; T4 consome somente os bindings de T3/2; run `succeeded` | Cenário completo da Fase 1, C18 |
| S03 | Mesmo roteiro, fixtures e relógio | O run de S02 é executado duas vezes em diretórios novos | `event_hash` final idêntico; todos os `record_hash` idênticos | ADR 0001 |
| S04 | T1 `completed`, avaliação ainda não gravada | O harness interrompe após `output_bound` e projeta o estado | T1 em `evaluating`; T2 continua `pending` | C62, X07 |
| S05 | Roteiro informa `resolved_model_id: null` | A tentativa termina | `execution_target_snapshot.model_ref` do cabeçalho identifica o modelo solicitado; `result_collected` grava `resolved_model_id: null`; a projeção AttemptState mantém `resolved_model_id: null`, sem inferência a partir de `model_ref` | ModelProfile, RoutingPolicy |

## Preparação e admissão

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| S06 | PlanningRequest com objetivo diferente do template | O run é iniciado | PlanningRecord `not_applicable`; manifesto `planning_failed`; run `failed`; nenhuma tentativa; `cost_summary.currency: null` se não houve orçamento resolvido | C56, X01 |
| S07 | Nenhum destino elegível para a tarefa | A admissão roda | RoutingDecision `no_eligible_target` gravada; `admission_rejected`; tarefa `failed`; sem consumo de tentativa | C12, R10 |
| S08 | Conhecimento obrigatório sem candidato | A admissão roda | Seleção gravada com status não admissível; `admission_rejected` (`knowledge_unsatisfied`); sem consumo de tentativa | C29, C75 |
| S09 | Fonte `active` no `status_cutoff` do catálogo e selecionada; `RunManifest.knowledge_status_assertions` traz asserção `revoked` dessa fonte com `effective_at` posterior ao cutoff e anterior ou igual ao instante do check | O check `knowledge_status` roda | Check `failed` com `knowledge_revoked`; `budget_released`; sem `dispatch_requested`. Com `effective_at` posterior ao instante do check, o check passa | C80 |
| S10 | Retry de T3 | A nova seleção roda | Usa o mesmo `knowledge_catalog_snapshot_ref` do manifesto; revisão fora do catálogo é rejeitada | C81, C83 |

## Divergência de digest

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| S11 | Bundle alterado depois da materialização | O check `context_integrity` recalcula o digest | Check `failed`; sem despacho; sem consumo de tentativa | C36, R08 |
| S12 | `routing_decision_ref` do ContextManifest com digest diferente do `record_hash` da decisão | O check `context_integrity` roda | Check `failed`; sem despacho | Invariante 6 de roteamento |
| S13 | Roteiro com `declared_digest_mismatch` em um output | O runner ingere o artefato | O ORQ usa o digest que calculou; a divergência gera ErrorRecord `integrity_mismatch`; tentativa `failed`, não `completed` | C47, A03 |
| S14 | Bytes de um artefato vinculado são alterados no armazenamento antes da avaliação | O critério `digest_matches` roda | Critério `fail`; relatório `fail`; sem `outputs_accepted` | C17, C55 |
| S15 | Relatório `pass` cujos `subject_bindings` têm digest diferente do binding da tentativa | O runner tenta gravar `outputs_accepted` | Gravação rejeitada; dependentes continuam `pending` | C70, X16 |
| S16 | Linha do meio do journal alterada | O runner reabre o run | `journal_integrity_error`; o runner recusa continuar e não repara o arquivo | Invariante 9 de run-contracts |

## Falhas técnicas e retry

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| S17 | Roteiro: tentativa 1 `fail_retryable`, tentativa 2 `finish` | A tarefa executa | Tentativa 1 `failed` com `retryable`; tarefa `retry_wait → ready`; tentativa 2 com novo `attempt_id`, `previous_attempt_id` preenchido e workspace novo | C18 |
| S18 | Roteiro: `fail_non_retryable` em T2 do workflow T1–T5 | A tarefa executa | T2 `failed` sem segunda tentativa; T3, T4 e T5 `blocked`; run `failed` | C25 |
| S19 | `max_attempts` = 2; as duas tentativas `fail_retryable` | A tarefa executa | Exatamente dois `dispatch_requested`; nenhuma terceira admissão; tarefa `failed` | C14 |
| S20 | Roteiro omite um output declarado | A tentativa termina | ErrorRecord `output_missing`; tentativa `failed`, nunca `completed` | C48, A04 |
| S21 | Roteiro com `wrong_contract` em um output | O binding é validado | Binding rejeitado; ErrorRecord `output_contract_mismatch`; tentativa `failed` | C48 |
| S22 | Avaliação `fail` e `retry_policy.on_evaluation_fail` = `false` | O relatório é gravado | Tarefa `failed`; sem nova tentativa | C15 |
| S23 | Reavaliação de um `fail` válido | O runner decide o próximo passo | Nenhum novo `evaluation_requested` para os mesmos artefatos | Ciclo da Fase 1 |

## Avaliação

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| S24 | Verificador simulado: avaliação 1 `inconclusive`, avaliação 2 `pass` | A tarefa é avaliada | Dois relatórios com `evaluation_id` diferentes e os mesmos `subject_bindings`; tarefa `succeeded` com base no segundo | C16, C69 |
| S25 | Critério obrigatório com método fora da lista suportada | A avaliação roda | Critério `not_run`; veredicto `inconclusive`; nunca `pass` | C67, X13 |
| S26 | Todas as tarefas aceitas; critério global obrigatório `fail` | A avaliação global roda | Relatório global `fail`; run `failed` | C26 |

## Timeout e reconciliação

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| S27 | Roteiro `hang` com `on_cancel: confirmed` | `deadline_at` passa | `deadline_exceeded`, `cancel_requested`, `termination_confirmed` (`timeout`); tentativa `timed_out`; retry permitido pela `retry_policy` | Ciclo da Fase 1 |
| S28 | Roteiro `hang` com `on_cancel: unconfirmed` | `deadline_at` passa | Tentativa `unknown`; tarefa `reconciling`; `reconciliation_requested` com a mesma `dispatch_idempotency_key` no payload e chave distinta no envelope; nenhum segundo `dispatch_requested` | C19, X08 |
| S29 | S28 com `reconcile: found_finished` | A reconciliação roda | Tentativa `unknown → completed` sem novo despacho; tarefa `evaluating` | C20 |
| S30 | S28 com `reconcile: unknown` até o prazo | `reconciliation_timeout_ms` esgota | Tentativa permanece `unknown`; tarefa e run `indeterminate`; workspace da tentativa não é reutilizado | C21 |
| S31 | Roteiro `ack_lost`, depois `reconcile: found_running` | O despacho não confirma | Tentativa `queued → unknown → running` na mesma tentativa; um único `dispatch_requested` | C20, C23 |
| S32 | Roteiro `not_executed` com `reconcile_not_found_authoritative` e `reconcile: not_found` | A reconciliação roda | Tentativa 1 `failed` com `dispatch_not_executed` (retryable); tentativa 1 continua contada; tentativa 2 admitida | Ciclo da Fase 1 |
| S33 | Mesmo cenário de S32, mas o executor não declara `reconcile_not_found_authoritative` | A reconciliação recebe `not_found` | `not_found` tratado como `unknown`; sem retry; reconciliação até o prazo | Perfis, P03 |

## Crash e persistência

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| S34 | `crash_after: dispatch_requested` | O runner é reiniciado | Reconciliação pela mesma intenção; o simulado recebeu um único despacho; o journal tem um único `dispatch_requested` para aquela tentativa | C57, X02 |
| S35 | `crash_after: budget_reserved`, antes de `dispatch_requested` | O runner é reiniciado | `budget_released` (`admission_aborted`); `max_attempts` não consumido; nova admissão normal | Ciclo da Fase 1 |
| S36 | Última linha do journal truncada no meio | O runner reabre o run | Bytes movidos para `discarded/`; `journal_tail_discarded` com o digest deles; execução continua | Protocolo de escrita |
| S37 | Observação externa repetida com mesma `idempotency_key` e mesmo payload | O runner grava | Evento existente devolvido; nenhuma nova linha, transição ou contabilização | C24, C58, X03 |
| S38 | Mesma `idempotency_key` com payload diferente | O runner grava | `idempotency_conflict`; run `stopping` | C59, X04 |
| S39 | `state_transition` com `from` diferente do estado projetado | O validador recebe o evento | Evento rejeitado; nada gravado | C60, X05 |
| S40 | `crash_after: evaluation_requested` | O runner é reiniciado | Relatório com `execution_status: failed` e `verdict: not_run` gravado para aquela avaliação; execução não concluída só admite `inconclusive` ou `not_run`; nova avaliação somente se restar limite | Retomada após crash |

## Custo e orçamento

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| S41 | Sem teto financeiro nem estimativa; T3/2 com custo `unavailable` | O run termina | `budget_reserved` com `status: unavailable`, `amount: null`; UsageRecord com `amount: null`; RunSummary com subtotal conhecido, `unavailable_count` = 1 e `total_cost_exact: false`; nenhum zero inserido; reserva da avaliação local liberada após término confirmado | C27 |
| S42 | Teto obrigatório; destino sem entrada em `cost_estimates` | A admissão roda | Check `budget` `failed` com `budget_unknown`; sem consumo de tentativa | C44, E10 |
| S43 | Teto obrigatório; estimativa conservadora de 0,30; custo real `unavailable` | A tentativa termina | Reserva liquidada com usage `unavailable`; o valor reservado continua contando contra o teto nas próximas admissões; resumo mostra a lacuna | C27, C44 |
| S44 | Orçamento restante cobre a execução, mas não a avaliação reservada | A admissão roda | Tentativa não admitida | C28, E11 |
| S45 | Reserva de 0,30 liquidada com custo `reported` de 0,20 | O resumo é projetado | Somente 0,20 entra no custo conhecido; reserva não é somada | C64, X10 |
| S46 | Validador recebe cost_line `status: unavailable` com `amount: "0"` | A validação roda | Rejeição estrutural | Invariante 8 de run-contracts |
| S47 | UsageRecord chega depois de `run_closed` | O runner grava | `usage_attached` após o cutoff; estado do run e RunSummary publicado inalterados | C31, C61, X06 |

## Cancelamento e prazo do run

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| S48 | `cancel_requested` antes de `run_manifest_closed` | O runner processa o pedido | Manifesto `cancelled_before_ready`; run `cancelled`; nenhuma tentativa; `cost_summary.currency: null` se não houve orçamento resolvido | Registros da Fase 1 |
| S49 | Cancelamento com tentativa ativa e `on_cancel: confirmed` | O runner processa o pedido | Tentativa e tarefa `cancelled`; tarefas não iniciadas `cancelled` (`run_cancelled`); nenhum retry; run `cancelled` | C22 |
| S50 | Cancelamento com tentativa ativa e `on_cancel: unconfirmed`, `reconcile: unknown` | O prazo de reconciliação esgota | Tarefa e run `indeterminate` | C21, C22 |
| S51 | Tempo do run esgotado com tarefas pendentes | O laço verifica o prazo antes de admitir | `deadline_exceeded`; run `stopping` e depois `failed`; nenhuma nova admissão | Ciclo da Fase 1 |

## Transições complementares

Cenários acrescentados para cobrir transições que não apareciam em S01–S51 e as regras revistas de roteamento e cancelamento.

| ID | Dado | Quando | Então | Transições | Base |
| --- | --- | --- | --- | --- | --- |
| S52 | Roteiro: T1/1 `dispatch: reject_confirmed`; T1/2 `default` | A tarefa executa | ErrorRecord `dispatch_failed` (`retryable`, `confirmed`); tentativa 1 `queued → failed` e contada; tarefa `running → retry_wait → ready`; tentativa 2 admitida; o simulado executou uma única vez | AT03, TK06 | Ciclo da Fase 1 |
| S53 | S52 com `max_attempts` = 1 no workflow T1–T5 | A tarefa executa | Tentativa 1 `failed`; tarefa `failed` sem segunda admissão; T2–T5 `blocked`; run `failed` | AT03, TK07, TK02 | C14, C25 |
| S54 | `cancel_after: dispatch_requested` | O runner chega ao passo 10 da admissão | Run `stopping`; tentativa `queued → cancelled` sem chamada a `executor.dispatch` (o simulado registra zero despachos); tarefa `running → cancelled`; não iniciadas `cancelled` (`run_cancelled`); run `cancelled` | AT04, TK09, TK24, RN07, RN09 | C22 |
| S55 | Roteiro `hang`, `on_cancel: unconfirmed`, `reconcile: found_stopped`; T1/2 `default` | `deadline_at` passa e a reconciliação roda | Tentativa 1 `running → unknown → timed_out`; tarefa `reconciling → retry_wait`; tentativa 2 admitida porque `on_confirmed_timeout` = `true`; um único `dispatch_requested` para a tentativa 1 | AT10, AT14, TK12 | C19, C20 |
| S56 | Cancelamento com tentativa ativa, `on_cancel: unconfirmed`, `reconcile: found_stopped` | A reconciliação roda | Tentativa `unknown → cancelled`; tarefa `reconciling → cancelled`; nenhum retry; run `cancelled` | AT15, TK14, RN09 | C22 |
| S57 | Roteiro `dispatch: not_executed`, `reconcile_not_found_authoritative`, `reconcile: not_found`, `cancel_after: reconciliation_requested` | A reconciliação recebe `not_found` | Com run `stopping`, `not_found` autoritativo leva a tentativa a `cancelled`, não a `failed`; tarefa `cancelled`; nenhuma tentativa 2 | AT05, AT15, TK14 | Ciclo da Fase 1 |
| S58 | Roteiro `ack_lost`, `execution: fail_non_retryable`, `reconcile: found_failed` | A reconciliação roda | Tentativa `unknown → failed` com ErrorRecord `non_retryable`; tarefa `reconciling → failed`; dependentes `blocked`; run `failed` | AT13, TK13 | C20, C25 |
| S59 | Verificador simulado: `unknown_termination` em T1/1 | A avaliação é requisitada | `evaluation_requested` sem relatório; tarefa `evaluating → reconciling`; prazo esgota sem relatório; tarefa e run `indeterminate`; nenhum `outputs_accepted` e nenhuma nova tentativa | TK21, TK15, RN10 | C21 |
| S60 | Verificador simulado: `["inconclusive", "inconclusive"]`; `max_evaluations_per_attempt` = 2 | A tarefa é avaliada | Dois relatórios `inconclusive` com os mesmos `subject_bindings`; tarefa `evaluating → waiting_evaluation → evaluating → failed` com motivo `evaluation_inconclusive`; nenhuma terceira avaliação | TK19, TK22, TK20 | C16, C69 |
| S61 | `cancel_after: evaluation_requested`; verificador `pass` | O runner processa o cancelamento | Relatório `pass` gravado; nenhum `outputs_accepted`; tarefa `evaluating → cancelled` (`run_cancelled`); dependentes `cancelled`, não `blocked`; run `cancelled` | TK24, RN09 | C22 |
| S62 | Política `typed_decision` com motor simulado de baixa confiança e `low_confidence_action: rules_fallback` | A admissão roda | RoutingDecision com `decision_status: low_confidence`, `effective_selection.used_fallback: true`; admissão segue; `target_ref` do DispatchIntent e do ContextManifest igual a `effective_selection.selected_target_id` | AT01, TK03 | R03 |
| S63 | Mesmo motor, com `low_confidence_action: no_dispatch` | A admissão roda | RoutingDecision com `effective_selection: null`; `admission_rejected` com motivo `low_confidence`; sem consumo de tentativa | TK04 | R03, C12 |

## Critério de revisão

Os cenários devem corresponder às tabelas de transição de [runner-state-machine.md](runner-state-machine.md) e aos contratos de [run-contracts.md](run-contracts.md). Toda transição AT, TK e RN precisa aparecer em pelo menos um cenário antes do fim do incremento 6C; transições sem cenário são lacunas a corrigir na especificação, não no código.

O fechamento só inclui tarefas terminais. Códigos de erro e motivos de rejeição seguem os enums de `run-contracts.md`, incluindo os códigos de limites/lock da ADR 0007. A avaliação global usa os artefatos aceitos da tentativa vencedora; nos três métodos de outputs, IDs do critério e do pass rule devem ser iguais, conforme `validate_instance` do 6A.
