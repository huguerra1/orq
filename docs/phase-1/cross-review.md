# Revisão cruzada da Fase 1 — baseline v0.1

Estado: revisão documental concluída. Este documento aceita o núcleo normativo v0.1 e registra escolhas operacionais ainda abertas; não representa teste executado nem implementação.

## Problema, alternativas e recomendação

Os contratos foram escritos separadamente e podem parecer completos sem formar uma execução coerente. A revisão precisa provar que identidades, estados, política, conhecimento, roteamento, contexto, tentativas, artefatos, custos e avaliações se conectam sem decisões implícitas.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Aceitar cada documento isoladamente | Encerramento rápido | Não detecta conflitos entre fronteiras |
| Manter toda decisão aberta | Evita compromisso prematuro | Impede schemas e fixtures úteis |
| Aceitar o núcleo e registrar gates operacionais | Permite implementação incremental e auditável | Exige distinguir contrato de escolha de tecnologia |

Recomendação adotada: aceitar como baseline v0.1 as identidades, relações, invariantes e estados já definidos. Linguagem, bibliotecas, limites numéricos, adaptadores e algoritmos concretos continuam abertos e devem ser escolhidos por ADR antes do componente que depende deles.

## Método e limite

A revisão:

1. percorre uma execução fictícia completa de T1 a T5;
2. verifica referências imutáveis e ordem de persistência;
3. exercita dois destinos, ranking tipado, reprovação, retry e avaliação global;
4. confere os casos negativos já documentados;
5. separa lacuna normativa de decisão operacional.

`H(x)` abaixo representa o digest completo dos bytes canônicos de `x`. A notação curta evita inventar bytes de uma fixture ainda inexistente; o schema executável exigirá algoritmo e valor completos.

## Condições iniciais do cenário

| Elemento | Identidade fictícia | Condição |
| --- | --- | --- |
| Projeto | `project:demo` | Autorizado pela política do host |
| Workflow | `workflow:feature/v1`, `H(workflow)` | DAG T1 → T2 → T3 → T4 e T5 dependente de T2/T3/T4 |
| Política | `policy:default/v1`, `H(policy)` | Concorrência 1, duas tentativas, limites finitos, teto USD 1,00 |
| Entrada | `artifact:repo-0`, `H(repo-0)` | Snapshot íntegro e autorizado |
| Catálogo operacional | `catalog:profiles/v1`, `H(profiles)` | Papel e dois ExecutionTargets ativos |
| Catálogo de conhecimento | `knowledge-catalog:demo/v1`, `H(knowledge-catalog)` | Fontes/revisões fixas para todo o run |
| Destino A | `target:A`, `H(target-A)` | Elegível para T3 com evidências válidas |
| Destino B | `target:B`, `H(target-B)` | Elegível para T3 com evidências válidas |
| Ranking | `typed_decision`, `H(routing-policy)` | Jev é adaptador candidato; opções continuam fechadas aos elegíveis |

O catálogo de conhecimento contém `source:architecture/rev3` e `source:security/rev2`, cada uma com digest, unidades e autoridade adequada ao projeto. T3 exige arquitetura e segurança; ambas são obrigatórias e cabem no orçamento do contexto.

## Traço ponta a ponta

### Preparação do run

1. `RunIntent run:demo-001` é persistido antes de planejamento ou chamada cobrada.
2. O workflow é validado: IDs, referências, cobertura e DAG são válidos. O relatório vira `artifact:validation-1`.
3. Entradas, perfis, política, limites e catálogos são resolvidos por identidade e hash.
4. `RunManifest manifest:demo-001`, status `ready`, fecha `H(workflow)`, `H(policy)`, `H(profiles)`, `H(knowledge-catalog)`, `artifact:repo-0`, limites, orçamento e configuração do Orchestrator.
5. O run transita `created → running`; o manifesto não recebe decisões ou resultados futuros.

### T1 e T2

| Tarefa | Entrada | Resultado técnico | Avaliação e liberação |
| --- | --- | --- | --- |
| T1 analisar | `artifact:repo-0` | `artifact:architecture-report-1` | `evaluation:T1-1` pass; binding aceito |
| T2 projetar | binding aceito de T1 | `artifact:solution-design-1` | `evaluation:T2-1` pass; binding aceito |

Cada linha representa também RoutingDecision, ContextManifest, reserva, AttemptRecord, UsageRecords e eventos próprios. A forma abreviada não permite que T2 consuma o relatório antes do evento `outputs_accepted` de T1.

### T3, primeira tentativa

1. `KnowledgeSelectionRecord knowledge-selection:T3-1` usa exatamente `H(knowledge-catalog)`, cobre as duas exigências obrigatórias e registra candidatos, exclusões, ranking lexical e orçamento.
2. A filtragem determinística confirma A e B como elegíveis. Somente esses IDs entram no input tipado do ranking.
3. O motor retorna A com confiança fictícia 0,74, acima do limiar 0,60. `RoutingDecision routing:T3-1` preserva recomendação, política, chamada, custo e seleção efetiva A.
4. `ContextManifest context:T3-1` referencia a seleção de conhecimento, T2, o snapshot do repositório, o destino A, os bytes ordenados e `H(bundle-T3-1)`.
5. O preflight revalida integridade, status/revogação do conhecimento, classificação, credenciais, quota, limites e orçamento. Suas evidências ficam na RoutingDecision finalizada.
6. Reservas de execução e avaliação são persistidas. `AttemptRecord attempt:T3-1`, número 1, workspace limpo, é criado com `dispatch_requested` antes da chamada.
7. A tentativa transita `queued → running → completed` e produz `artifact:code-patch-P1` e `artifact:implementation-report-R1`.
8. `EvaluationReport evaluation:T3-1` referencia os mesmos digests e reprova um critério obrigatório. Não há `outputs_accepted`; T3 transita `evaluating → retry_wait`.

### T3, retry

1. O relatório da avaliação anterior é materializado como `artifact:feedback-T3-1`, com produtor `evaluation_output` e procedência preservada.
2. O catálogo de conhecimento permanece `H(knowledge-catalog)`. Como contexto e tentativa mudaram, nasce `knowledge-selection:T3-2`; o registro anterior não é reescrito.
3. Nova filtragem mantém A e B elegíveis. O input tipado inclui apenas atributos autorizados e o estado de retry identificado; o motor recomenda B com confiança fictícia 0,71.
4. `routing:T3-2` e `context:T3-2` são novos. O contexto inclui o feedback, não os arquivos mutáveis nem o patch reprovado P1.
5. `attempt:T3-2`, número 2, começa novamente de `artifact:repo-0`. `previous_attempt_id` aponta para T3-1.
6. A tentativa produz `artifact:code-patch-P2` e `artifact:implementation-report-R2`. `evaluation:T3-2` aprova todos os critérios obrigatórios e o evento `outputs_accepted` fixa P2/R2.
7. T3 transita `retry_wait → ready → running → evaluating → succeeded`. P1 continua histórico e nunca satisfaz dependência.

### T4, T5 e avaliação global

1. T4 consome somente P2 aceito. Seu `test_patch` declara `artifact:repo-0` como raiz, P2 como pré-requisito e o digest da base composta. A aplicação íntegra gera PatchApplicationRecord e snapshot resultante; a avaliação local passa.
2. T5 consome diretamente o desenho de T2, P2 e os artefatos aceitos de T4. Seu review report preserva alegações, evidências e limitações; a avaliação local passa.
3. A avaliação global materializa uma cópia limpa, aplica P2 e depois o test patch, registra as duas aplicações e avalia os outputs finais do workflow.
4. `EvaluationReport evaluation:workflow-1` possui todos os critérios obrigatórios, bindings/digests exatos e verdict pass.
5. O run transita `running → succeeded`. RunSummary agrega tarefas, outputs, consumo, cobertura e cutoff sem alterar RunManifest ou registros anteriores.

## Estados e ordem verificados

| Aspecto | Resultado da revisão |
| --- | --- |
| Identidade | Workflow, políticas, catálogos, destinos, contextos e artefatos usam versão/hash ou digest |
| Conhecimento | Filtro de autorização precede ranking; seleção e bytes materializados são registros distintos |
| Jev/TypeSafe | Só ordena IDs elegíveis; não concede capacidade, permissão, aprovação ou orçamento |
| Contexto | Cada tentativa possui bundle, ordem, orçamento e procedência próprios |
| Tentativa | Intenção precede efeito; retry cria ID, decisão, contexto e workspace novos |
| Artefatos | Produção, binding, aplicação, avaliação e aceitação permanecem separados |
| Estado | `completed` técnico não equivale a `pass`; dependências aguardam `outputs_accepted` |
| Uso e custo | Reservas, consumo e correções não são somados duas vezes; unavailable não vira zero |
| Resultado | Sucesso global referencia os artefatos integrados efetivamente avaliados |

Falhas desconhecidas, crash, cancelamento e resposta tardia não aparecem no caminho feliz, mas permanecem cobertos pelos casos C19–C24, C31, C56–C64 e X01–X10. Isso evita inserir artificialmente uma execução indeterminada no cenário de sucesso.

## Lacunas normativas resolvidas

### Catálogo fixo e revogação

O conteúdo do KnowledgeCatalogSnapshot é fixo durante o run. Retry pode refazer seleção sobre o mesmo catálogo porque tarefa, feedback, orçamento ou contexto mudaram; não pode introduzir outra revisão de fonte. Nova revisão de conteúdo exige novo run.

Revogação não muta a fonte nem o snapshot. Ela é uma declaração de status append-only, emitida por autoridade compatível e com instante efetivo. Seleção registra o estado observado; o preflight consulta novamente as declarações vigentes. Uma revogação posterior à seleção bloqueia o despacho e permite nova seleção apenas entre unidades do catálogo fixo.

### Evidência do preflight

RoutingDecision passa a registrar `preflight_checks`: controle, status, instante, evidência e, quando aplicável, referência ao rascunho do contexto/bundle. Isso prova por que a seleção foi admitida ou rejeitada sem criar uma autorização separada ou uma referência circular entre hashes.

### Significado de “aceito”

Aceito v0.1 significa: campos conceituais, relações, invariantes, estados e comportamento negativo são suficientes para iniciar schemas e fixtures. Não significa que enumerações, bibliotecas, valores operacionais ou controles de um executor real já foram validados.

## Classificação dos contratos

| Contrato | Classificação | Observação |
| --- | --- | --- |
| Glossário e identidades | Aceito v0.1 | Vocabulários extensíveis continuam versionados |
| TaskSpec | Aceito v0.1 | KnowledgeRequirement refinado faz parte do baseline |
| WorkflowSpec | Aceito v0.1 | DAG estático, tarefas obrigatórias e concorrência inicial 1 |
| Ciclo, estados e retries | Aceito v0.1 | Estados externos incertos continuam bloqueando retry |
| Perfis e ExecutionTarget | Aceito v0.1 | Evidências reais serão verificadas por adaptador |
| ExecutionPolicy | Aceito v0.1 | Valores e enforcement concreto permanecem abertos |
| RoutingDecision e ContextManifest | Aceito v0.1 | Inclui ranking tipado restrito e evidência de preflight |
| ArtifactRef, patch e relatório | Aceito v0.1 | Subconjunto físico de git diff será escolhido depois |
| Registros e avaliação | Aceito v0.1 | Backend de persistência permanece aberto |
| Metadata e seleção de conhecimento | Aceito v0.1 | Catálogo fixo no run; revogação append-only |

Nenhum contrato central permanece aberto em semântica suficiente para a próxima etapa. Todos continuam versionáveis: uma descoberta em schemas, fixtures ou adaptadores cria revisão explícita, não correção silenciosa do histórico.

## Decisões operacionais abertas e gates

| Gate | Decisão necessária | Prazo |
| --- | --- | --- |
| G1 | Linguagem, biblioteca de JSON Schema e runner de fixtures | Antes do primeiro schema executável |
| G2 | Serialização canônica e representação de digest/referência | Antes das fixtures de hash |
| G3 | Vocabulários iniciais de IDs, tipos, erros, capacidades e métricas | Antes de validar catálogos |
| G4 | Layout do Vault, parser Markdown, chunking, ranking lexical e tokenizadores | Antes da recuperação executável |
| G5 | Persistência local, transações, armazenamento e retenção | Antes do primeiro runner |
| G6 | Primeiro executor, sandbox e testes reais de enforcement | Antes de qualquer chamada de agente |
| G7 | Adaptador, versão, limiar e benchmark de TypeSafe/Jev | Antes do experimento de ranking tipado |

Esses gates não reabrem a Fase 1; cada um deve produzir ADR, contrato concreto ou evidência antes de ativar a capacidade correspondente.

## Resultado e próxima etapa

O critério 1E foi atendido documentalmente: a execução fictícia é representável sem converter unknown em sucesso/custo zero, sem usar artefato reprovado e sem permitir que o ranking amplie elegibilidade.

A próxima etapa recomendada é G1 + G2: comparar opções de linguagem/tooling e serialização, registrar a decisão e implementar primeiro os schemas de referências comuns, TaskSpec e WorkflowSpec com fixtures determinísticas. Vault, MCP e integração Jev continuam posteriores a esse núcleo.
