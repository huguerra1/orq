# Fase 6 — Runner sequencial do Orchestrator e executor simulado

Estado: especificação proposta em 2026-11-10, aguardando revisão do dono. Nenhum código, schema executável ou fixture desta fase foi implementado. A decisão correspondente está na [ADR 0007](../adr/0007-phase-6-runner-and-simulated-executor.md), com status Proposta.

## Problema

As Fases 1 a 5 produzem um WorkflowSpec aceito, uma RoutingDecision finalizada e um ContextManifest íntegro, mas nada consome esses registros. Não existe quem admita tentativas, persista a intenção antes do efeito, observe o término, avalie os artefatos e decida retry ou parada. Sem esse runner, os contratos de ciclo de vida e de registros da Fase 1 continuam apenas documentais.

A fase precisa provar o fluxo `planner → router → contexto → tentativa → avaliação` sem chamar provedores reais, para separar erros de contrato e de máquina de estados da variabilidade, do custo e das credenciais de um runtime externo.

## Objetivo

Especificar e, após o aceite, implementar:

1. contratos executáveis de RunIntent, RunManifest, DispatchIntent, AttemptRecord, EvaluationReport e do envelope JournalEvent;
2. armazenamento append-only local e mínimo, com integridade por digest JCS;
3. máquina de estados sequencial de run, tarefa e tentativa, incluindo admissão, timeout, reconciliação, retry e cancelamento;
4. executor simulado determinístico, roteirizado por fixture versionada;
5. avaliador determinístico mínimo;
6. interface do adaptador de executor real, sem implementar nenhum adaptador real.

## Escopo

### Entra

| Item | Conteúdo |
| --- | --- |
| Contratos de execução | RunIntent, RunManifest, DispatchIntent, AttemptRecord (cabeçalho + eventos), EvaluationReport, JournalEvent, subconjuntos mínimos de UsageRecord, ErrorRecord e reserva de orçamento |
| Persistência | Journal JSONL local por run, encadeado por digest, mais armazenamento de artefatos endereçado por conteúdo |
| Runner | Laço sequencial (concorrência igual a um) sobre o DAG aceito, com desempate pela ordem de `tasks` |
| Admissão | Seleção de conhecimento, roteamento, materialização de contexto, checks de admissão, reservas e persistência da intenção antes do despacho |
| Recuperação | Timeout, reconciliação pela mesma intenção, retry com workspace limpo, retomada após crash |
| Cancelamento | Pedido de cancelamento do run, confirmação de parada e encerramento `indeterminate` quando a parada não é confirmada |
| Executor simulado | Comportamento roteirizado por `(task_id, attempt_number)`, relógio injetado, sem rede |
| Avaliador | Métodos determinísticos fechados; método desconhecido produz `not_run` |
| Interface de adaptador real | Operações, dados trocados e checklist de verificação dos controles |
| CLI | Proposta de subcomando para execução simulada ponta a ponta (sujeita a aprovação) |

### Fica fora

- Qualquer adaptador real (Claude Code, Codex CLI, Antigravity CLI, Kiro CLI) e qualquer chamada a provedor.
- Credenciais, rede e efeitos externos; ApprovalRecord continua especificado, mas sem uso nesta fase.
- Aplicação de patches e PatchApplicationRecord. Os artefatos simulados são relatórios JSON; a avaliação global não integra patches.
- Concorrência maior que um, leases, workers e scheduling adaptativo (Fases 11 e 12).
- Evaluator por modelo ou rubrica textual (Fase 8) e relatórios de métricas (Fase 10).
- Schema completo de ExecutionPolicy. A Fase 6 usa somente os limites resolvidos necessários ao runner (ver [run-contracts.md](run-contracts.md#resolvedlimits)).
- Sandbox real de filesystem. O executor simulado não executa código do projeto.

## Dependências das Fases 1 a 5

| Fase | O que a Fase 6 usa | Situação |
| --- | --- | --- |
| 1 | Ciclo de vida, registros de execução/avaliação, ExecutionPolicy, ArtifactRef, cenários C14–C31 e X01–X16 | Contratos aceitos; nada executado |
| 2 | `canonical_digest`/`canonicalize` (JCS + SHA-256), `validate_workflow`, `select_knowledge`, `materialize_knowledge_context`, `validate_context_manifest` | Implementado e testado |
| 3 | Servidor MCP somente leitura | Não é necessário ao runner; o executor simulado recebe o bundle diretamente |
| 4 | `plan_and_validate(planner, request, completed_at=...)` → `PlanningResult(record, workflow)` | Implementado; PlanningRequest exige `run_id` já registrado por RunIntent, que ainda não existe |
| 5 | `route_and_finalize(request, catalog, policy, preflight_checks=..., decision_engine=...)` → RoutingDecision com `record_hash` | Implementado; não despacha nem consome tentativa |

### Lacunas encontradas no código atual

Estas lacunas não invalidam as fases anteriores, mas exigem decisão nesta fase:

1. **Ordem entre roteamento e contexto.** O contrato da Fase 1 prevê preflight de contexto registrado na RoutingDecision antes de finalizá-la. No código, `route_and_finalize` exige `preflight_checks` já resolvidos e `materialize_knowledge_context` exige `routing_decision_ref` com `content_digest`, isto é, uma decisão já finalizada. A proposta é registrar os checks dependentes do bundle no DispatchIntent (`admission_checks`), mantendo na RoutingDecision apenas os checks disponíveis antes da materialização. Ver [runner-state-machine.md](runner-state-machine.md#pipeline-de-admissão).
2. **RunIntent inexistente.** PlanningRequest referencia um `run_id` "já registrado"; a Fase 6 cria o primeiro registro do run.
3. **ExecutionPolicy sem schema.** RoutingRequest só carrega `execution_policy_snapshot_ref`. O runner precisa de valores finitos; a proposta é um objeto `resolved_limits` fechado no RunManifest.
4. **ArtifactRef mínimo.** `common.schema.json#/$defs/artifactRef` contém apenas `artifact_id` e `content_digest`. O descritor completo da Fase 1 passa a ser o payload do evento `artifact_ingested`; as referências continuam mínimas.
5. **ContextManifest sem hash próprio.** O schema não tem campo `record_hash`; a referência ao manifesto usa `canonical_digest` do documento inteiro, calculado por quem o referencia.

## Incrementos de implementação (após aceite)

| Incremento | Entrega | Gate |
| --- | --- | --- |
| 6A | Schemas de JournalEvent, RunIntent, RunManifest, DispatchIntent, AttemptHeader, EvaluationReport, UsageRecord e ErrorRecord mínimos | Meta-schema, fixtures válidas e inválidas, hashes reproduzíveis |
| 6B | Journal append-only e armazenamento de artefatos | Cadeia de digests, sequência contígua, cauda truncada, conflito de idempotência |
| 6C | Projeções e máquina de estados (validador de transições) | Toda transição da tabela coberta; saltos inválidos rejeitados |
| 6D | Executor simulado e avaliador determinístico | Roteiros dos cenários S01–S63 executáveis sem rede |
| 6E | Runner ponta a ponta e CLI proposta | Cenário completo reproduzível com o mesmo digest final do journal |

Cada incremento termina com testes, atualização do HANDOFF e commit próprio, conforme AGENTS.md.

## Documentos

- [Escolha do primeiro executor/runtime](executor-choice.md)
- [Contratos de execução e armazenamento append-only](run-contracts.md)
- [Máquina de estados sequencial do runner](runner-state-machine.md)
- [Cenários de aceitação da Fase 6](acceptance-scenarios.md)
- [ADR 0007 — Runner sequencial e executor simulado](../adr/0007-phase-6-runner-and-simulated-executor.md)

## Critério de saída da fase

Um run simulado do workflow de referência T1–T5 percorre planejamento, roteamento, contexto, tentativa, avaliação, retry de T3 e avaliação global, terminando `succeeded`. O journal é reproduzível byte a byte com o mesmo roteiro, relógio e fixtures. Os cenários de falha, timeout, reconciliação, custo desconhecido, divergência de digest e cancelamento da [matriz](acceptance-scenarios.md) passam como testes automatizados. Até lá, os cenários são especificação, não testes executados.
