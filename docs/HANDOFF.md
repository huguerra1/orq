# Retomada do projeto ORQ

Atualizado em 2026-11-10. Este é um resumo operacional da conversa e do repositório, não uma transcrição integral do chat. Atualizar após cada entrega; em caso de divergência, conferir arquivos e histórico Git.

## Leitura para retomar

Leia [AGENTS.md](../AGENTS.md), este resumo e o [índice da Fase 1](phase-1/README.md). O usuário prefere respostas curtas em português.

## Objetivo e método

Criar uma plataforma experimental de orquestração de agentes baseada em MCP, independente de provedores. Integrações de execução desejadas: Codex, Claude e Antigravity/AGY, ainda sem SDK ou versão concreta escolhidos; o SDK TypeSafe 0.7.2 foi escolhido apenas para o Router opcional.

O usuário pediu arquitetura antes de implementação: problema, alternativas, recomendação, interfaces, schemas, riscos e testes. Apoiou o plano incremental. A implementação começa por schemas, validadores e testes após fechar a Fase 1; não construir o sistema inteiro antecipadamente.

Papéis previstos incluem orchestrator, software_architect, backend_engineer, frontend_engineer, researcher, tester, code_reviewer e security_reviewer. São responsabilidades, não processos permanentes nem vínculos fixos com modelos.

## Estado atual

- Baseline documental v0.1 e Fases 2–5 concluídos; ainda não há runner do Orchestrator nem chamada real de modelo.
- Fase 1 concluiu contratos; Fase 2 entregou contratos/Vault; Fase 3 expôs conhecimento por MCP; Fase 4 entregou planner fixo e admissão central do WorkflowSpec.
- Aceitos após revisão cruzada: TaskSpec, WorkflowSpec, ciclo, perfis, RoutingDecision, ContextManifest, ExecutionPolicy, artefatos, registros de execução/avaliação e metadata de conhecimento do Vault.
- Especificados 83 cenários gerais, oito casos de perfis, dez de roteamento/contexto, 12 de política, 14 de artefatos, 16 de registros e 17 de conhecimento. Não são testes executados.
- TypeSafe AI/Jev foi incorporado como adaptador opcional de `DecisionEngine`, atrás de uma interface independente de provedor. O SDK oficial 0.7.2 está fixado; nenhuma credencial foi adicionada nem houve chamada real.
- ADR 0001 escolheu Python 3.12, JSON Schema Draft 2020-12, jsonschema/pytest e RFC 8785/JCS.
- Implementados schemas comuns, TaskSpec/WorkflowSpec e conhecimento, validação semântica, CLI, fixtures e digest SHA-256 canônico.
- Implementados parsing seguro de frontmatter, paths confinados, ingestão por digest, chunking por headings e snapshot com revogação append-only.
- Implementados filtros de projeto/autoridade/classificação/validade/applicability, BM25 determinístico por requisito, cobertura obrigatória antes da opcional, orçamento global/local e materialização com nova verificação de digest.
- Implementados ContextManifest fechado e bundle JSON canônico separado, com ordem, procedência, digests cruzados e orçamento do destino.
- Implementadas fixtures reais e CLI atômica de catálogo, seleção e contexto, sem sobrescrita implícita.
- Histórico da Fase 2: 52 testes locais aprovados, dependências íntegras, links locais, formatação Git, sequência dos IDs dos cenários e soma do exemplo financeiro fictício.
- Fase 3 implementada com MCP Python SDK 2.2.0, `stdio`, JSON canônico, testes em memória e resources concretos somente leitura.
- Histórico da Fase 3: 58 testes aprovados; o smoke `stdio` precisou rodar fora da sandbox porque o pool de threads do AnyIO é bloqueado nela, e passou no ambiente local irrestrito.
- Fase 4 implementada com schemas, interface `Planner`, `FixedTemplatePlanner`, PlanningRecord e gate central de WorkflowSpec.
- Fase 5 implementou schemas de roteamento, filtro obrigatório, baselines `fixed`/`rules`, interface tipada, transporte simulado e adaptador opcional Jev.
- Histórico da Fase 5: 91 testes aprovados, inclusive smoke MCP fora da sandbox; dependências consistentes. O resultado atual está abaixo, no fechamento do 6A.
- Fase 6 especificada e aceita em 2026-11-10 ([phase-6/README.md](phase-6/README.md), [ADR 0007](adr/0007-phase-6-runner-and-simulated-executor.md)): contratos, máquina de estados e 63 cenários S01–S63. O 6A entrega oito schemas, 14 fixtures válidas, oito inválidas e testes estruturais/digests. O 6B implementa journal e armazenamento de artefatos; runner, executor e CLI permanecem sem implementação. Os cenários do runner ainda não são testes executados.
- A especificação recomenda executor simulado primeiro e só a interface do adaptador real. Os controles das CLIs candidatas (Claude Code, Codex CLI, Antigravity CLI, Kiro CLI) estão todos marcados a verificar; a web estava indisponível na redação e nenhuma CLI foi executada.
- A lacuna entre `route_and_finalize` e `materialize_knowledge_context` tem solução aceita na ADR 0007: checks dependentes do bundle ficam no DispatchIntent. O 6A fecha `admission_checks`, incluindo os seis controles mínimos, todos `passed`.
- Escolhas do 6A e da revisão independente estão em [Notas de implementação 6A](phase-6/run-contracts.md#notas-de-implementação-6a). Corrigidos os achados 1–10 e 12–16: reservas mantêm `amount: null`/`status: unavailable` sem estimativa, agregados de artefatos/erros correspondem à origem, criação é implícita nos eventos iniciais, resumo contém só tarefas terminais e não inventa moeda. Fixtures têm referências e hashes consistentes; códigos/motivos são enums e textos livres do 6A têm limite de 4096 caracteres. Execução de avaliação não concluída só admite `inconclusive`/`not_run`.
- Achado 10: o baseline exige `pass_rule` não vazio. Mantida a forma existente; `validate_instance` passou a exigir igualdade de `output_ids` do critério global e pass rule, após validação estrutural, em EvaluationReport e payloads de `evaluation_requested`/`evaluation_report_recorded`. Consumidores do journal em 6B/6C precisam usar essa API; `validator_for` continua estrutural. Outros vínculos e agregação completa seguem no 6C. Não há novo serviço/dependência.
- Achados 14–15: chave do despacho só é reutilizada no payload de reconciliação, com chave própria por requisição no envelope; a reserva da avaliação local é liberada após término confirmado, sem UsageRecord fictício. Término indeterminado mantém a reserva pendente.
- 6A: 41 pares agregado/evento permitidos e 79 proibidos cobertos por testes.
- **6B implementado e achados 1–19 corrigidos**, somente armazenamento: `RunJournal.create/open/append/close`, lock com PID/host/horário/UUID e remoção condicionada ao token; escrita JCS seguida de flush/fsync; validação, cadeia de hashes/sequências/causas e idempotência por payload/tipo/agregado. `ArtifactStore(journal_aberto)` exige a posse do lock e publica bytes por SHA-256, com temporário no mesmo diretório, fsync, link + unlink no POSIX ou rename no Windows e conferência do destino. Destino existente e journal reaberto são sincronizados antes do uso. Limites de 1 MiB incluindo LF e 8 MiB, inclusive na leitura; erros tipados também para ausência, confinamento e colisão por caixa. `.pending-*` é reportado para inspeção manual. Sem mudanças nos schemas, dependências ou testes antigos. Decisões e API nas [notas do 6B](phase-6/run-contracts.md#notas-de-implementação-6b). ErrorRecord/stopping, projeções e máquina de estados continuam fora do incremento.
- Decisão do orquestrador em **2026-11-10**: só a cauda **sem LF** é descartável; linha inválida com LF e journal só com cauda são `journal_integrity_error`. O último evento de escrita com falha pode reaparecer confirmado após reabertura; o 6C deve tratar isso como intenção sem efeito comprovado, encaminhando despacho à reconciliação. Colisão de run IDs apenas por caixa é recusada com `run_id_case_collision`.
- Histórico integral verificado pelo orquestrador (2026-11-10), comando `PYTHONPATH=src python -m pytest -p no:cacheprovider --basetemp=<pasta gravável>`: **6A = 366 aprovados e 1 ignorado**, CI verde no PR #8; **6B antes da revisão = 432 aprovados e 1 ignorado**; **6B após a correção dos achados 1–19 = 485 aprovados e 4 ignorados** (symlinks sem privilégio no Windows; junctions cobertas).
- Após as correções: **119 aprovados e 3 ignorados** nos 122 casos de `tests/test_journal.py`, `tests/test_artifact_store.py` e `tests/test_storage.py`, com fixture temporária de verificação herdando as ACLs da worktree. Os três skips são symlinks sem privilégio no Windows; os três casos de junction passaram. Essa execução complementar não substitui a suíte integral histórica acima. Trabalho local em `claude/fase6-6b`, sem commit/push por instrução do usuário.
- **6C implementado** em `state_machine.py` e `projections.py`: tabelas das 49 linhas AT/TK/RN, guardas com evidências anteriores, replay puro dos dicionários do journal, avaliações/bindings, erros/usos, reservas e custo por cadeia de correções, cutoff publicado e ações de retomada sem I/O. `project_journal(events, workflow=...)` recebe o WorkflowSpec e confere sua referência/digest, pois seus bytes não estão no journal. Criações continuam implícitas; `unknown` e intenção reaparecida após fsync falho nunca autorizam novo despacho. `not_found` só é conclusivo com controle verificado explicitamente informado e evidência no evento; AT04 exige prova volátil do runner e nunca é recomendado após crash. Decisões nas [Notas 6C](phase-6/runner-state-machine.md#notas-de-implementação-6c).
- Verificação inicial do 6C: **140 testes próprios aprovados**; suíte integral anterior verificada pelo orquestrador: **625 aprovados e 4 ignorados**. A revisão independente (Kiro, Claude Opus) gerou as correções dos **achados 1–13**, registradas nas [Notas 6C](phase-6/runner-state-machine.md#notas-de-implementação-6c): evidência de falha posterior/confirmada e causa do retry preservada; prazo inicial de reconciliação fixo; dependentes falhos bloqueados; avaliação terminal sem relatório/liberação tardios; orçamento opcional com `budget_committed: None` e `budget_gaps`; AT04 sem prova financeira de não execução; confirmação compatível com AT03/AT07; causa de parada fixada em RN07; rejeição de admissão com erros e reservas liberadas.
- Verificação pós-revisão do 6C pelo orquestrador: suíte integral com **702 aprovados e 4 ignorados** (symlinks sem privilégio no Windows).
- Após a revisão: **217 aprovados, nenhum ignorado**, em `tests/test_state_machine.py` e `tests/test_projections.py`, com `PYTHONPATH=src /c/dev/orq/.venv/Scripts/python -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp tests/test_state_machine.py tests/test_projections.py` e fixture temporária de diretórios herdando as ACLs da worktree, removida após a verificação. Cobrem linhas AT/TK/RN, todos os saltos proibidos entre estados, regressões negativas, S31 completo, tentativa 2 em S32 e integração com `RunJournal`/falha de fsync. A suíte integral após estas correções cabe ao orquestrador; os 63 cenários não foram executados pelo runner. Sem mudanças nos schemas, dependências ou testes anteriores ao 6C; trabalho local em `claude/fase6-6c`, sem commit/push por instrução do usuário.
- Publicações anteriores confirmadas em main. Sempre conferir o remoto novamente ao retomar.

## Próximo passo concreto

A especificação da Fase 6 foi aceita em 2026-11-10 ([ADR 0007](adr/0007-phase-6-runner-and-simulated-executor.md), "Decisões do dono"; o dono delegou o aceite ao Claude).

1. O orquestrador verifica a suíte integral das correções 6C. Próximo incremento: **6D — executor simulado e avaliador determinístico**, com testes próprios e sem rede. Consumir os contratos do 6A e as projeções/guardas/ações puras do 6C; não implementar ainda o laço do runner ou a CLI. O chamador fornece o WorkflowSpec preservado e os controles verificados do executor; mantém desconhecidos, limites e reservas sem inventar uso/custo. Usar os quatro métodos conhecidos do avaliador; método desconhecido continua `not_run`.
2. 6E prova o fluxo planner → router → contexto → tentativa → avaliação ponta a ponta com o executor simulado e o subcomando `orq run simulate`. Deve transformar conflito de idempotência do append em ErrorRecord do run e parada, validar antes de gravar transições e executar as ações de retomada uma etapa por vez, reprojetando. AT04 só antes de chamar dispatch no mesmo processo; após crash, reconciliação da intenção existente. **Pendência do achado 11 da revisão do 6A (somente 6E):** criar template próprio com os quatro métodos conhecidos (`output_binding_present`, `digest_matches`, `content_contract_valid`, `simulated_verdict`) para o caminho feliz S01; manter um método desconhecido para S25. As fixtures antigas `workflow-minimal.json`, `task-minimal.json` e `routing-request.json` continuam intactas com `schema_validation`, que no avaliador do 6D produz `not_run` e não deve aprovar S01.

O adaptador real fica para depois do runner simulado e de uma sessão de verificação com versão fixada (checklist V01–V12 em [phase-6/executor-choice.md](phase-6/executor-choice.md)).

Não é necessário pedir novamente autorização para a documentação ou para seu envio ao repositório já autorizado.

## Fechamento da Fase 1

A revisão cruzada está em [phase-1/cross-review.md](phase-1/cross-review.md). Todos os contratos centrais foram aceitos como baseline v0.1; decisões tecnológicas e valores operacionais permanecem gates explícitos para as etapas que dependem deles.

Não iniciar MCP ou SDKs de agentes antes do Vault local e de seus testes determinísticos.

## Decisões aceitas no baseline v0.1

- Aplicação modular local, sem microserviços no MVP.
- Separação entre papel, modelo, provedor, runtime e executor.
- Comparação inicial de runtimes completos, identificando modelo e ferramentas; não atribuir toda diferença ao modelo.
- MCP como camada de ferramentas/conhecimento; Orchestrator determinístico controla o workflow.
- DAG estático e versionado desde os schemas; concorrência inicial igual a um.
- Todas as tarefas do workflow inicial são obrigatórias. Dependências em TaskSpec são a única fonte das arestas.
- max_attempts inclui a primeira tentativa. Falha definitiva interrompe novas admissões.
- completed não implica pass. Timeout sem término confirmado exige reconciliação antes de retry.
- Retry parte de workspace limpo e só usa artefatos aceitos de dependências, além de feedback identificado.
- Política central no workflow; tarefa só restringe os limites herdados.
- Model Profile contém declarações com procedência, não médias históricas editadas manualmente.
- Capacidades, permissões e qualidade medida são conceitos diferentes.
- Fonte de conhecimento e contexto precisam de identidade, revisão e hash; custo desconhecido não é zero.
- O catálogo de conhecimento é fixo durante o run; revogação append-only pode bloquear despacho, mas revisão nova exige novo run.
- JSON Schema é o formato canônico proposto, com validação semântica complementar.
- Stack, primeiro runtime, valores operacionais dos limites e projeto de benchmark ainda precisam ser escolhidos.

## Roadmap acordado

| Fase | Entrega |
| --- | --- |
| 1 | Contratos, estados e cenários de aceitação |
| 2 | Schemas/fixtures v0.1 e Vault Markdown com recuperação inicial por metadata/busca lexical |
| 3 | MCP para acesso ao conhecimento |
| 4 | Task Planner e validação do plano |
| 5 | Model Router por regras |
| 6 | Primeiro executor e runner sequencial, já com persistência e avaliação mínimas |
| 7 | Segundo provedor/runtime |
| 8 | Evaluator ampliado |
| 9 | Execution Memory ampliada |
| 10 | Métricas e relatórios |
| 11 | DAG concorrente e integração de artefatos |
| 12 | Scheduling experimental |

O objetivo científico é comparar Single-Agent, Multi-Agent com roteamento fixo, LLM Router, regras, métricas e scheduling otimizado. Registrar sobrecarga de planejamento, roteamento, avaliação e retries; preservar condições experimentais. Algoritmos avançados ficam para depois de baselines e evidência suficiente.

## Arquivos e marcos

- [TaskSpec](phase-1/task-contract.md).
- [WorkflowSpec](phase-1/workflow-contract.md).
- [Estados e limites](phase-1/execution-lifecycle.md).
- [Cenários gerais](phase-1/acceptance-cases.md).
- [Perfis](phase-1/agent-profiles.md).
- [Roteamento e contexto](phase-1/routing-context-contract.md).
- [Política de execução](phase-1/execution-policy-contract.md).
- [Artefatos, patches e relatórios](phase-1/artifact-contract.md).
- [Registros de execução e avaliação](phase-1/execution-records-contract.md).
- [Metadata de conhecimento do Vault](phase-1/knowledge-metadata-contract.md).
- [Revisão cruzada e baseline v0.1](phase-1/cross-review.md).
- [Andamento da Fase 2](phase-2/README.md).
- [ADR 0001 — tooling e JSON canônico](adr/0001-contract-tooling-and-canonical-json.md).
- [ADR 0002 — Vault e parsing Markdown](adr/0002-vault-layout-and-markdown-parsing.md).
- [ADR 0003 — materialização do bundle de contexto](adr/0003-context-bundle-materialization.md).
- [ADR 0004 — fronteira MCP do conhecimento](adr/0004-mcp-knowledge-boundary.md).
- [Andamento da Fase 3](phase-3/README.md).
- [ADR 0005 — interface do planner e baseline fixo](adr/0005-planner-interface-and-fixed-baseline.md).
- [Andamento da Fase 4](phase-4/README.md).
- [ADR 0006 — fronteira do Router e decisão tipada](adr/0006-router-boundary-and-typed-decision.md).
- [Andamento da Fase 5](phase-5/README.md).
- [Fase 6 — runner sequencial e executor simulado](phase-6/README.md), especificação aceita e 6A implementado.
- [ADR 0007 — runner sequencial e executor simulado](adr/0007-phase-6-runner-and-simulated-executor.md), aceita.
- 69f7360: documentos iniciais.
- bd5904e: workflow, ciclo e cenários.
- 05050eb: perfis e oito casos de aceitação.
- 8bab500: instruções e contexto para retomada.
- 5961293: roteamento, contexto e encaixe documental de TypeSafe AI/Jev.
- 2cde742: política de execução, permissões, orçamento e evidências.
- 921c06d: ArtifactRef e contratos de patch/relatório.
- 8959bf3: registros de execução, uso, aprovação e avaliação.
- 4720205: metadata de conhecimento do Vault.
- Consultar git log para trabalhos posteriores.

## Git e ambiente

Diretório utilizado: /var/www/orq. Remoto: git@github.com:huguerra1/orq.git; branch main. Identidade pessoal local: huguerra1 <hugofmourao@gmail.com>. A configuração global é de trabalho e não deve ser alterada. Os dois primeiros commits mantêm a identidade anterior; o usuário não autorizou reescrever esse histórico.

A autenticação SSH funcionou após cadastro da chave pelo usuário. O ambiente usou temporariamente /tmp/orq-github-known-hosts, validado contra a chave oficial do GitHub, em core.sshCommand somente por comando. Esse arquivo pode desaparecer; verificar o ambiente atual e a identidade do servidor ao retomar, sem desabilitar verificação de host.

A sandbox apresentou falha bwrap antes de executar comandos e patches. Nesta sessão foram necessárias execuções com aprovação fora da sandbox. Não assumir que essa limitação persiste nem que aprovações são transferidas. Nenhuma credencial foi incluída na documentação.

## Como começar outra conversa

Abrir este repositório no ambiente de trabalho e enviar:

> Leia AGENTS.md e docs/HANDOFF.md. Continue pelo próximo passo registrado. Responda de forma curta.

Para outros ambientes sem leitura automática dessas instruções, fornecer explicitamente esses dois arquivos. Em outro clone, conferir/configurar a identidade Git local: .git/config não é transportado pelo Git.

O uso de AGENTS.md como instruções do projeto segue a [documentação oficial do Codex](https://developers.openai.com/pt-BR/docs/agent-configuration/agents-md). Isso preserva orientações e referências no repositório, sem prometer transferência automática de toda a conversa.
