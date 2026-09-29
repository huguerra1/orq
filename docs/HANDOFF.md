# Retomada do projeto ORQ

Atualizado em 2026-09-29. Este é um resumo operacional da conversa e do repositório, não uma transcrição integral do chat. Atualizar após cada entrega; em caso de divergência, conferir arquivos e histórico Git.

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
- Verificações feitas: 52 testes locais aprovados, dependências íntegras, links locais, formatação Git, sequência dos IDs dos cenários e soma do exemplo financeiro fictício.
- Fase 3 implementada com MCP Python SDK 2.2.0, `stdio`, JSON canônico, testes em memória e resources concretos somente leitura.
- Verificações atuais: 58 testes aprovados; o smoke `stdio` precisou rodar fora da sandbox porque o pool de threads do AnyIO é bloqueado nela, e passou no ambiente local irrestrito.
- Fase 4 implementada com schemas, interface `Planner`, `FixedTemplatePlanner`, PlanningRecord e gate central de WorkflowSpec.
- Fase 5 implementou schemas de roteamento, filtro obrigatório, baselines `fixed`/`rules`, interface tipada, transporte simulado e adaptador opcional Jev.
- Verificações atuais: 91 testes aprovados, inclusive smoke MCP fora da sandbox; dependências consistentes.
- Publicações anteriores confirmadas em main. Sempre conferir o remoto novamente ao retomar.

## Próximo passo concreto

Iniciar a especificação da Fase 6 antes de código de execução:

1. Escolher o primeiro executor/runtime e documentar seus controles realmente verificáveis.
2. Formalizar RunManifest, AttemptRecord, DispatchIntent, EvaluationReport e armazenamento append-only mínimo.
3. Definir a máquina sequencial do DAG, admissão, timeout, reconciliação e retry sem repetir efeito externo indeterminado.
4. Implementar um executor simulado antes do adaptador real e provar o fluxo planner → router → contexto → tentativa → avaliação.

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
