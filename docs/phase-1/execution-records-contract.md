# Registros de execução, uso, aprovação e avaliação

Estado: proposta documental da Fase 1. Não implementa persistência, executor, cobrança, aprovação ou avaliador.

## Problema e alternativas

O ORQ precisa reconstruir o que foi planejado, autorizado, despachado, observado e aceito, inclusive após crash ou resposta tardia. Um estado atual isolado perde causalidade; event sourcing integral adiciona complexidade desnecessária ao MVP.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Registros mutáveis com estado atual | Implementação direta | Sobrescreve fatos e dificulta reconciliar incerteza |
| Snapshot final por tarefa | Leitura simples | Não protege intenção anterior ao despacho nem tentativas falhas |
| Event sourcing integral | Histórico completo e projeções flexíveis | Complexidade operacional prematura |
| Cabeçalhos imutáveis, eventos append-only e projeções | Auditável sem exigir infraestrutura distribuída | Requer validar sequência e idempotência |

Recomendação: persistir intenções e referências imutáveis antes dos efeitos; acrescentar eventos identificados conforme fatos são observados; derivar estado corrente por transições validadas. Projeções podem ser atualizadas para consulta, mas não são a fonte única de auditoria.

## Convenções comuns

Todo registro possui `schema_version`, identificador único, `run_id`, `recorded_at` em UTC e origem/ator. Registros externos distinguem `occurred_at` informado da hora em que o ORQ os recebeu. Ordem de gravação usa sequência monotônica por agregado; relógio externo não reordena causalidade.

Referências persistidas incluem identidade e hash/revisão quando aplicável. IDs não são reutilizados. Um evento repetido com a mesma idempotency_key e conteúdo é deduplicado; mesma chave com conteúdo diferente é conflito.

Mensagens, argumentos e payloads potencialmente sensíveis são reduzidos ou preservados como ArtifactRefs classificados. Registros centrais não armazenam credenciais, segredos nem respostas brutas ilimitadas.

Os contratos diferenciam:

- **intenção:** decisão persistida antes de um possível efeito externo;
- **observação:** fato recebido do host, executor ou provedor;
- **interpretação:** classificação do Orchestrator, como retryable;
- **veredicto:** conclusão de EvaluationReport sobre critérios;
- **projeção:** estado derivado para consulta.

## JournalEvent e projeções

JournalEvent é o envelope append-only usado pelos ciclos de run, tentativa e avaliação.

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| event_id | Identificador | Único no run |
| aggregate_type, aggregate_id | Enum e ID | Run, attempt, evaluation ou approval afetado |
| sequence | Inteiro positivo | Contíguo dentro do agregado |
| event_type | Identificador controlado | Tipo do fato ou intenção |
| recorded_at | Data UTC | Momento persistido pelo ORQ |
| occurred_at | Data UTC ou null | Momento externo, quando conhecido |
| actor | Referência | Orchestrator, executor, provedor, verificador ou humano |
| correlation_id | Identificador | Liga operações do mesmo fluxo |
| causation_event_id | ID ou null | Evento anterior que causou este evento |
| idempotency_key | Texto ou null | Chave de deduplicação quando aplicável |
| payload | Objeto tipado | Dados limitados pelo event_type |

Transições de estado são eventos próprios e registram `from`, `to`, razão e evidências. O validador rejeita salto inválido, sequência duplicada ou estado anterior divergente. Eventos informativos tardios podem ser anexados após estado terminal, mas não produzem nova transição nem reabrem o agregado.

RunStateProjection, TaskStateProjection, AttemptStateProjection e EvaluationStateProjection são visões reconstruíveis. Cada uma informa `as_of_event_id`/sequence e instante de corte. Relatórios experimentais usam um cutoff explícito para que dados tardios não alterem silenciosamente resultados já publicados.

## RunIntent e RunManifest

RunIntent é o registro mínimo criado antes de planejamento ou outro efeito: `run_id`, solicitação/objetivo, project_id, ator solicitante, configuração de experimento, created_at e idempotency_key. Custos e falhas de planejamento podem assim existir mesmo se nenhum workflow válido for produzido.

RunManifest é fechado quando o plano foi validado ou quando a preparação termina sem plano executável. Depois de fechado é imutável.

| Campo | Tipo conceitual | Obrigatoriedade e significado |
| --- | --- | --- |
| schema_version | Texto | Obrigatório |
| run_id | Identificador | Obrigatório; igual ao RunIntent |
| manifest_status | Enum | `ready`, `planning_failed` ou `cancelled_before_ready` |
| created_at, closed_at | Datas UTC | Obrigatórias |
| project_ref | Referência versionada | Obrigatória |
| objective_snapshot | Objeto/hash | Obrigatório; objetivo efetivamente planejado |
| workflow_snapshot_ref | Referência com hash ou null | Obrigatória para ready; null nos encerramentos sem plano |
| validation_report_ref | ArtifactRef ou null | Relatório do plano quando produzido |
| execution_policy_snapshot | Objeto/referência com hash ou null | Obrigatório para ready; null se preparação terminou antes da resolução |
| catalog_snapshot_refs | Lista | Perfis, targets, ferramentas e contratos resolvidos; pode ser parcial fora de ready |
| knowledge_catalog_snapshot_ref | Referência com hash ou null | [Catálogo de conhecimento](knowledge-metadata-contract.md) disponível no início; conteúdo usado aparece no ContextManifest |
| external_input_bindings | Lista | input_id para ArtifactRef; todos verificados em ready, possivelmente parciais nos demais status |
| resolved_limits | Objeto ou null | Valores efetivos e origem; obrigatório para ready |
| budget_snapshot | Objeto ou null | Teto, envelopes, moeda e reservas iniciais; obrigatório antes de chamada cobrada |
| orchestrator_snapshot | Objeto | Versão, configuração, host e capacidades relevantes |
| scheduling_policy | Objeto ou null | Concorrência e desempate; obrigatório para ready |
| experiment_metadata | Objeto | Grupo, seed e tags controladas; pode ser vazio |
| preparation_coverage | Objeto | Etapas concluídas, ausentes e motivo |
| preparation_usage_refs, preparation_error_refs | Listas | Consumo e falhas anteriores ao fechamento |
| manifest_hash | Digest | Representação canônica do manifesto fechado |

RunManifest não contém decisões futuras, estados finais, custos observados ou artefatos ainda inexistentes. Esses fatos entram no journal e nos registros específicos.

Para `ready`, todas as referências obrigatórias resolvem, o DAG é válido, entradas externas existem e limites finitos foram calculados. Nos demais status, campos não resolvidos ficam null/parciais e aparecem em preparation_coverage; não são preenchidos com suposições. `planning_failed` preserva UsageRecords/ErrorRecords do planejamento, mas não autoriza tarefas.

Eventos de run incluem início, manifesto fechado, admissão/parada, cancel_requested, deadline_exceeded, reconciliação e término. RunSummary é uma projeção final com estado, tarefas, bindings finais, consumo agregado, lacunas e cutoff; não substitui registros fonte.

## AttemptRecord

AttemptRecord é um agregado com cabeçalho imutável persistido antes do despacho e JournalEvents append-only.

### Cabeçalho

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| attempt_id | Identificador | Único no run |
| task_id, workflow_ref | Referências | Tarefa imutável executada |
| attempt_number | Inteiro positivo | Contíguo por tarefa; criação consome max_attempts |
| previous_attempt_id | ID ou null | Tentativa anterior da mesma tarefa |
| created_at | Data UTC | Persistência anterior ao despacho |
| routing_decision_ref | Referência com hash | Decisão finalizada usada |
| context_manifest_ref | Referência com hash | Contexto exato usado |
| execution_target_snapshot | Objeto/referência com hash | Modelo, provedor, runtime, executor e configuração |
| effective_policy_snapshot_ref | Referência com hash | Política resolvida para a tentativa |
| workspace_base_ref | ArtifactRef | Snapshot limpo inicial |
| input_bindings | Lista | Inputs externos verificados e outputs aceitos de dependências, com a base de autorização/aceitação |
| budget_reservation_refs | Lista | Reservas anteriores ao despacho, inclusive avaliação obrigatória |
| dispatch_intent_id | Identificador | Identidade lógica do despacho |
| dispatch_idempotency_key | Texto | Reutilizada na reconciliação; nunca cria nova intenção |
| deadline_at | Data UTC | Derivada dos limites aplicáveis |

O cabeçalho começa em queued. Erro anterior à sua criação não consome tentativa. Depois de criado, cancelamento antes da chamada continua sendo tentativa registrada.

### Eventos e fatos da tentativa

Tipos iniciais incluem `dispatch_requested`, `dispatch_acknowledged`, `execution_started`, `model_call_observed`, `tool_call_observed`, `artifact_ingested`, `output_bound`, `cancel_requested`, `deadline_exceeded`, `termination_confirmed`, `reconciliation_observed`, `usage_attached`, `error_attached` e `state_transition`.

Um dispatch_requested precisa estar persistido antes da chamada externa. A confirmação registra external_call_id quando disponível. Queda entre os dois deixa a tentativa queued ou unknown conforme evidência; o Orchestrator consulta usando a mesma intenção antes de considerar novo despacho.

Tool/model events registram ferramenta/modelo efetivo, operação, horários, status, política autorizadora, UsageRecord/ErrorRecord e artefatos de entrada/saída quando preservados. Argumentos sensíveis são resumidos por hash ou ArtifactRef classificado. Telemetria indisponível é declarada, não omitida como se a chamada não existisse.

AttemptRecord separa:

- `produced_artifacts`: ArtifactRefs ingeridos pela tentativa;
- `output_bindings`: output_id para ArtifactRef validado contra OutputSpec;
- `undeclared_artifacts`: diagnósticos/anexos que não satisfazem outputs;
- `patch_application_refs`: aplicações realizadas dentro da tentativa;
- `usage_record_refs` e `error_record_refs`.

Estado técnico segue o ciclo já definido: queued, running, completed, failed, timed_out, cancelled ou unknown. `completed` exige término confirmado, outputs recebidos íntegros o bastante para avaliação e nenhuma falha técnica terminal; não significa pass.

## PatchApplicationRecord

PatchApplicationRecord é finalizado e torna-se imutável quando a operação termina ou quando o cutoff de reconciliação registra status unknown. A intenção de aplicação é persistida antes do efeito e recebe application_id/idempotency_key.

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| application_id | Identificador | Único no run |
| purpose | Enum | `attempt`, `evaluation` ou `integration` |
| owner_ref | Referência | attempt_id, evaluation_id ou run_id, conforme purpose |
| patch_ref | ArtifactRef | PatchArtifact exato |
| root_snapshot_ref | ArtifactRef | Snapshot raiz verificado |
| prerequisite_patch_refs | Lista ordenada | Composição efetivamente usada |
| materialized_base_digest | Digest de árvore | Deve igualar base_state_digest do patch |
| workspace_ref | Referência | Workspace isolado e não reutilizado enquanto houver incerteza |
| policy_snapshot_ref | Referência com hash | Regras verificadas |
| applicator_snapshot | Objeto | Ferramenta, versão e configuração |
| started_at, finished_at | Datas ou null | Cobertura temporal conhecida |
| status | Enum | `applied`, `rejected`, `conflict`, `policy_denied`, `integrity_error` ou `unknown` |
| parsed_operations | Lista | Operações observadas no payload |
| policy_checks | Lista | Regras e resultados por operação |
| diagnostic_artifact_refs | Lista | Saídas limitadas e classificadas |
| result_snapshot_ref | ArtifactRef ou null | Somente para applied íntegro |
| result_tree_digest | Digest ou null | Digest calculado do resultado |
| usage_record_refs, error_record_refs | Listas | Consumo e erros associados |

Status unknown bloqueia reutilização do workspace e nova aplicação até reconciliação. Falha parcial não publica result_snapshot_ref. Aplicar o mesmo patch outra vez ou sobre outra base cria outro application_id.

## ApprovalRecord

ApprovalRecord liga uma decisão humana/externa a uma intenção exata. A solicitação é persistida antes de obter a decisão; eventos append-only registram granted, denied, expired ou revoked.

| Campo do cabeçalho | Tipo conceitual | Semântica |
| --- | --- | --- |
| approval_id | Identificador | Único no run |
| policy_rule_ref | Referência com hash | Regra que exige/aceita aprovação |
| effect_class | Enum | Classe do efeito solicitado |
| action_digest | Digest | Representação canônica da ação, argumentos e alvo |
| scope | Objeto | Repositório, serviço, destinatário, paths e operação aplicáveis |
| requester_ref | Referência | Componente/ator solicitante |
| authority_requirement | Objeto | Autoridades e autenticação aceitas |
| requested_at, expires_at | Datas UTC | Janela da solicitação |
| reuse_policy | Objeto | Uso único ou quantidade/escopo limitados |

Uma concessão registra authority_ref, decisão, decided_at e evidência externa quando permitida. O valor secreto de autenticação não é persistido. O consumo da aprovação referencia approval_id e action_digest; ação diferente exige nova aprovação. Revogação impede usos futuros, mas não apaga efeitos anteriores.

Aprovação satisfaz uma condição da ExecutionPolicy; não torna permitida ação que a política proíbe. Confiança do Jev, saída de agente e texto em documento não são autoridades.

## UsageRecord e BudgetReservationRecord

UsageRecord é imutável por observação/estimativa e pode receber registros complementares posteriores sem sobrescrever o anterior.

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| usage_id | Identificador | Único no run |
| scope_type, scope_ref | Enum e referência | planning, routing, attempt, model_call, tool_call ou evaluation |
| provider_ref | Referência ou null | Fonte externa, quando aplicável |
| observed_at, recorded_at | Datas UTC | Momento da medição e ingestão |
| measurements | Lista | metric_id, quantidade decimal, unidade, status e fonte |
| cost_lines | Lista | amount, currency, status, pricing_ref e método |
| coverage | Objeto | reported, estimated ou unavailable por métrica esperada |
| supersedes_usage_id | ID ou null | Correção explícita; registro anterior permanece |

Métricas iniciais incluem tokens de entrada/saída/cache quando disponíveis, chamadas, duração, bytes, requisições e custo. Ausência não vira zero. Categorias desconhecidas do provedor permanecem namespaced.

Cada consumo debitável pertence a um único escopo folha, como uma chamada de modelo ou ferramenta. Attempt, evaluation e run agregam os usage_ids filhos; não criam cópias cobradas do mesmo consumo. Um UsageRecord agregado, quando necessário para intercâmbio, lista seus componentes e é marcado como resumo não debitável.

BudgetReservationRecord contém reservation_id, scope, amount/currency ou quantidade, método conservador, created_at, expires_at e status derivado de eventos `active`, `released`, `settled` ou `expired`. Liquidação aponta para UsageRecords correspondentes. Agregação usa o registro mais recente de uma cadeia supersedes, soma custos liquidados uma vez e mantém reservas ativas separadas.

Conversões de moeda preservam taxa, fonte e timestamp. Sem conversão autorizada, o resumo apresenta subtotais por moeda e lacuna, não um total falso.

## ErrorRecord

ErrorRecord descreve um erro observado sem determinar sozinho a transição de estado.

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| error_id | Identificador | Único no run |
| scope_type, scope_ref | Enum e referência | Run, planning, routing, attempt, call, patch ou evaluation |
| phase | Identificador | Operação em curso |
| category | Enum | `validation`, `policy`, `integrity`, `transport`, `provider`, `timeout`, `cancellation`, `evaluation` ou `internal` |
| code | Identificador estável | Classificação legível por software |
| message | Texto sanitizado | Sem segredo ou payload ilimitado |
| occurred_at, recorded_at | Datas UTC | Tempos conhecidos |
| retryability | Enum | `retryable`, `non_retryable` ou `unknown` |
| termination_certainty | Enum | `confirmed`, `not_terminated` ou `unknown` |
| external_call_ref | Referência ou null | Chamada afetada |
| cause_error_refs | Lista | Cadeia causal acíclica |
| diagnostic_artifact_refs | Lista | Detalhes preservados com classificação |
| provider_extension | Objeto opcional | Dados namespaced e limitados |

Retryability é interpretação versionada, não texto livre do provedor. Timeout com termination_certainty unknown leva a reconciliação; não autoriza retry. Erros repetidos podem compartilhar código, mas recebem error_ids distintos por ocorrência.

## EvaluationReport

EvaluationReport registra uma execução de avaliação sobre sujeitos imutáveis. Cada intenção de avaliação recebe evaluation_id e evaluation_number antes da chamada; isso consome o limite mesmo se terminar unknown e não gerar relatório final naquele momento. Um novo relatório nunca sobrescreve o anterior.

| Campo | Tipo conceitual | Obrigatoriedade e significado |
| --- | --- | --- |
| schema_version | Texto | Obrigatório |
| evaluation_id | Identificador | Único no run |
| evaluation_number | Inteiro positivo | Contíguo dentro do mesmo escopo/sujeito |
| scope | Enum/objeto | `task_attempt` ou `workflow`; identifica task/attempt quando local |
| subject_bindings | Lista não vazia | output_id/final output para ArtifactRef e digest exatos |
| patch_application_refs | Lista | Aplicações usadas para avaliar estado integrado |
| criteria_snapshot | Objeto/referência com hash | Critérios e rubricas efetivamente aplicados |
| evaluator_snapshot | Objeto | Determinístico, humano, modelo ou composição; versões/configuração |
| context_manifest_ref | Referência ou null | Obrigatória quando avaliador recebeu contexto materializado |
| started_at, completed_at | Datas UTC ou null | Cobertura temporal |
| execution_status | Enum | `completed`, `failed`, `timed_out`, `cancelled` ou `indeterminate` |
| criterion_results | Lista | Um resultado por critério aplicável |
| verdict | Enum | `pass`, `fail`, `inconclusive` ou `not_run` |
| evidence_refs | Lista | ArtifactRefs, eventos e aplicações usados |
| usage_record_refs, error_record_refs | Listas | Consumo e erros da avaliação |
| limitations, unknowns | Listas | Cobertura incompleta explicitada |
| report_hash | Digest | Representação canônica final |

CriterionResult contém criterion_id, required, requirement_ids, método/verificador, status `pass`, `fail`, `inconclusive` ou `not_run`, evidence_refs, observações limitadas e ErrorRefs. Todos os critérios do snapshot aparecem; ausência não é interpretada como pass.

Regras de agregação:

1. qualquer critério obrigatório fail determina verdict fail;
2. sem fail obrigatório, qualquer obrigatório inconclusive/not_run determina inconclusive;
3. pass exige todos os critérios obrigatórios pass e execução completed;
4. avaliação não iniciada ou falha antes de aplicar critérios produz not_run;
5. falha determinística obrigatória não pode ser sobreposta por rubrica textual;
6. resultado opcional não altera sozinho o veredicto global, mas permanece registrado;
7. verdict nunca se aplica a artefato com digest diferente do subject_bindings.

Enquanto o término do avaliador é unknown, nenhum EvaluationReport final é fabricado e o journal permanece em reconciliação. Se o prazo de reconciliação se esgota, um relatório com execution_status indeterminate, verdict not_run e cutoff explícito pode fechar a observação sem afirmar término externo. Falha confirmada do verificador pode finalizar not_run ou inconclusive conforme critérios efetivamente executados. Reavaliar exige limite e orçamento, usa os mesmos subject_bindings e cria novo evaluation_id. Um fail válido não é repetido para buscar variação favorável.

Quando um relatório fundamenta aceitação, um evento `outputs_accepted` liga task_id, output_ids, ArtifactRefs/digests e evaluation_id. Dependências usam apenas esse binding. Avaliação global referencia os bindings locais aceitos e PatchApplicationRecords do resultado integrado.

## Relações e ordem de persistência

Fluxo mínimo de uma tentativa:

1. RoutingDecision finalizada e ContextManifest íntegro.
2. Reservas de execução e avaliação persistidas.
3. Cabeçalho de AttemptRecord e dispatch_requested persistidos atomicamente.
4. Chamada externa; acknowledgement/eventos ou estado unknown.
5. ArtifactRefs e output bindings preservados antes de completed.
6. EvaluationReport referencia exatamente esses bindings.
7. outputs_accepted libera dependências ou registra retry/falha.
8. UsageRecords tardios podem complementar custo sem reabrir estados.

Não é obrigatório usar um banco transacional distribuído. No MVP local, uma transação de persistência local deve proteger cada intenção antes do efeito e cada transição com sua evidência.

## Interfaces conceituais

| Operação | Entrada | Saída |
| --- | --- | --- |
| close_run_manifest | RunIntent, plano, política, catálogos e inputs | RunManifest imutável |
| append_event | Agregado, sequência esperada e evento | Evento aceito ou conflito |
| project_state | Manifesto e eventos até cutoff | Projeções reproduzíveis |
| create_attempt_intent | Tarefa pronta, decisão, contexto e reservas | AttemptRecord queued |
| reconcile_external_call | Intenção, external ID e observações | Eventos, sem novo despacho implícito |
| record_usage | Medição e cobertura | UsageRecord ou correção explícita |
| request_approval | Ação e regra | ApprovalRecord pendente |
| apply_patch_recorded | Intenção, patch, base e política | PatchApplicationRecord |
| evaluate_subjects | Critérios, subjects e evaluator | EvaluationReport ou avaliação em reconciliação |
| accept_outputs | Report pass e bindings idênticos | Evento outputs_accepted |

## Invariantes

1. Intenção relevante é persistida antes do efeito externo correspondente.
2. Sequência append-only não é reescrita; correções apontam para registros anteriores.
3. Estado projetado resulta somente de transições válidas e informa cutoff.
4. Eventos tardios acrescentam fatos/custos, mas não reabrem estado terminal.
5. RunManifest fechado não recebe decisões ou artefatos futuros.
6. attempt_number é contíguo por tarefa e sua criação consome max_attempts.
7. Mesma dispatch_intent nunca gera dois despachos lógicos.
8. Attempt completed não implica Evaluation pass nem outputs_accepted.
9. Retry exige término confirmado, orçamento, limite e nova tentativa/workspace.
10. Usage unavailable não vira zero; correção não apaga medição anterior.
11. ErrorRecord não autoriza retry sem transição validada e término confirmado.
12. ApprovalRecord só vale para action_digest, escopo, autoridade e validade registrados.
13. PatchApplicationRecord applied referencia base e resultado íntegros exatos.
14. Evaluation pass referencia todos os critérios obrigatórios e os mesmos digests aceitos.
15. Artefato reprovado ou avaliação de digest diferente não libera dependências.
16. Dados sensíveis entram por ArtifactRef classificado, não por payload irrestrito de evento.

## Riscos e verificações

- **Atualização perdida:** usar sequência esperada/controle de concorrência mesmo com concurrency um.
- **Duplo despacho:** persistir intenção e idempotency_key; reconciliar antes de reenviar.
- **Relógios divergentes:** separar occurred_at de recorded_at e ordenar por journal.
- **Evento duplicado:** deduplicar por identidade/chave e rejeitar conteúdo conflitante.
- **Estado derivado incorreto:** testar projeções desde o journal e comparar com snapshots de leitura.
- **Custo duplicado:** separar reservas, liquidação e correções; agregar por cadeia supersedes.
- **Erro simplificado demais:** preservar certeza de término e diagnósticos classificados.
- **Aprovação reaproveitada:** vincular digest da ação, escopo, validade e consumo.
- **Avaliador parcial:** listar todos os critérios e unknowns; ausência nunca significa pass.
- **Resposta tardia:** registrar com cutoff sem reabrir execução nem alterar relatório publicado.

## Casos específicos a formalizar

| ID | Caso | Resultado esperado |
| --- | --- | --- |
| X01 | Planejamento cobra e falha antes de workflow válido | RunIntent, Usage/Error e manifesto planning_failed preservados |
| X02 | Crash após dispatch_requested, antes da resposta | Mesma intenção reconciliada; nenhum segundo despacho lógico |
| X03 | Evento repetido com mesma chave e conteúdo | Deduplicado sem nova transição/contagem |
| X04 | Mesma chave chega com conteúdo diferente | Conflito registrado e processamento interrompido |
| X05 | Transição parte de estado divergente | Evento rejeitado |
| X06 | Resposta/custo chega após run terminal | Fato anexado; estado e cutoff publicados não reabertos |
| X07 | Tentativa completed com output binding íntegro | Aguarda avaliação; dependências continuam bloqueadas |
| X08 | Timeout sem término confirmado | Attempt unknown/reconciling; retry negado |
| X09 | UsageRecord corrige estimativa anterior | Cadeia preservada; agregação usa correção uma vez |
| X10 | Reserva é liquidada | Não somar reserva e custo liquidado simultaneamente |
| X11 | Aprovação concedida para action_digest diferente | Uso rejeitado |
| X12 | Patch applied gera snapshot íntegro | Registro referencia base, ferramenta e resultado exatos |
| X13 | Critério obrigatório ausente do relatório | Verdict pass inválido |
| X14 | Critério obrigatório fail e rubrica textual pass | Verdict final fail |
| X15 | Reavaliação dos mesmos artefatos após inconclusive | Novo evaluation_id dentro do limite; ambos preservados |
| X16 | Evaluation pass usa digest diferente do binding | outputs_accepted rejeitado |

Esses casos especificam persistência e projeção futuras; não são testes executados.

## Decisões ainda abertas

- Tecnologia de persistência local e transações.
- Serialização canônica, retenção e compactação do journal.
- Granularidade inicial dos eventos de tools/model calls.
- Vocabulário final de métricas, códigos de erro e status de aprovação.
- Formato de assinatura/autenticação de decisões humanas.
- Política de publicação e congelamento de relatórios experimentais.

O próximo passo é realizar a revisão cruzada da Fase 1 com uma execução fictícia completa, fechar lacunas normativas e classificar contratos como aceitos ou ainda abertos, antes de escolher stack ou implementar schemas.
