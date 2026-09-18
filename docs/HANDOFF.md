# Retomada do projeto ORQ

Atualizado em 2026-09-18. Este é um resumo operacional da conversa e do repositório, não uma transcrição integral do chat. Atualizar após cada entrega; em caso de divergência, conferir arquivos e histórico Git.

## Leitura para retomar

Leia [AGENTS.md](../AGENTS.md), este resumo e o [índice da Fase 1](phase-1/README.md). O usuário prefere respostas curtas em português.

## Objetivo e método

Criar uma plataforma experimental de orquestração de agentes baseada em MCP, independente de provedores. Integrações desejadas: Codex, Claude e Antigravity/AGY, sem SDK ou versão concreta escolhidos ainda.

O usuário pediu arquitetura antes de implementação: problema, alternativas, recomendação, interfaces, schemas, riscos e testes. Apoiou o plano incremental. A implementação começa por schemas, validadores e testes após fechar a Fase 1; não construir o sistema inteiro antecipadamente.

Papéis previstos incluem orchestrator, software_architect, backend_engineer, frontend_engineer, researcher, tester, code_reviewer e security_reviewer. São responsabilidades, não processos permanentes nem vínculos fixos com modelos.

## Estado atual

- Somente documentação Markdown. Nenhuma aplicação, schema executável, dependência instalada ou integração real com modelo.
- Fase 1 em andamento e ainda não concluída.
- Propostos: TaskSpec, WorkflowSpec, estados/limites/retries e perfis de papel, modelo e executor.
- Especificados 32 cenários gerais e oito casos de perfis. Não são testes executados.
- Verificações feitas: links locais, formatação Git, sequência dos IDs dos cenários e soma do exemplo financeiro fictício.
- Publicações anteriores confirmadas em main. Sempre conferir o remoto novamente ao retomar.

## Próximo passo concreto

Criar uma proposta documental para ContextManifest e RoutingDecision, sem implementar código:

1. ContextManifest: itens enviados, ordem, fonte, revisão/hash, escopo, orçamento e conteúdo materializado.
2. RoutingDecision: tarefa/tentativa, snapshots utilizados, candidatos elegíveis/excluídos, motivos, política e destino recomendado/selecionado.
3. Explicitar a sequência entre seleção de destino, montagem de contexto e checagem final de limites.
4. Definir exemplos válidos/inválidos e manter coerência com TaskSpec e os perfis.
5. Atualizar índices e este resumo; verificar e publicar uma entrega pequena.

Não é necessário pedir novamente autorização para a documentação ou para seu envio ao repositório já autorizado.

## O que falta para fechar a Fase 1

- ContextManifest e RoutingDecision.
- ExecutionPolicy: estrutura completa de limites e permissões, inclusive semântica de caminhos e validade de evidências.
- ArtifactRef e contratos de patch/relatório.
- RunManifest, AttemptRecord, uso/custos/erros e EvaluationReport.
- Metadata de conhecimento para o Vault.
- Revisão cruzada, cenários completos e definição explícita do que está fechado versus pendente.

Não iniciar MCP, RAG executável ou SDKs de agentes enquanto essa etapa documental estiver aberta.

## Decisões propostas a preservar

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
- JSON Schema é o formato canônico proposto, com validação semântica complementar.
- Stack, primeiro runtime, valores operacionais dos limites e projeto de benchmark ainda precisam ser escolhidos.

## Roadmap acordado

| Fase | Entrega |
| --- | --- |
| 1 | Contratos, estados e cenários de aceitação |
| 2 | Vault Markdown e recuperação inicial por metadata/busca lexical |
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
- 69f7360: documentos iniciais.
- bd5904e: workflow, ciclo e cenários.
- 05050eb: perfis e oito casos de aceitação.
- Esta entrega acrescenta instruções de retomada; consultar git log para seu hash e para trabalhos posteriores.

## Git e ambiente

Diretório utilizado: /var/www/orq. Remoto: git@github.com:huguerra1/orq.git; branch main. Identidade pessoal local: huguerra1 <hugofmourao@gmail.com>. A configuração global é de trabalho e não deve ser alterada. Os dois primeiros commits mantêm a identidade anterior; o usuário não autorizou reescrever esse histórico.

A autenticação SSH funcionou após cadastro da chave pelo usuário. O ambiente usou temporariamente /tmp/orq-github-known-hosts, validado contra a chave oficial do GitHub, em core.sshCommand somente por comando. Esse arquivo pode desaparecer; verificar o ambiente atual e a identidade do servidor ao retomar, sem desabilitar verificação de host.

A sandbox apresentou falha bwrap antes de executar comandos e patches. Nesta sessão foram necessárias execuções com aprovação fora da sandbox. Não assumir que essa limitação persiste nem que aprovações são transferidas. Nenhuma credencial foi incluída na documentação.

## Como começar outra conversa

Abrir este repositório no ambiente de trabalho e enviar:

> Leia AGENTS.md e docs/HANDOFF.md. Continue a Fase 1 pelo próximo passo registrado. Responda de forma curta.

Para outros ambientes sem leitura automática dessas instruções, fornecer explicitamente esses dois arquivos. Em outro clone, conferir/configurar a identidade Git local: .git/config não é transportado pelo Git.

O uso de AGENTS.md como instruções do projeto segue a [documentação oficial do Codex](https://developers.openai.com/pt-BR/docs/agent-configuration/agents-md). Isso preserva orientações e referências no repositório, sem prometer transferência automática de toda a conversa.
