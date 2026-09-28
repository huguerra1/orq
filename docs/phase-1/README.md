# Fase 1 — Contratos da plataforma

Estado: baseline documental v0.1 concluído e aceito na revisão cruzada. Nenhum runtime, biblioteca ou serviço foi implementado ou instalado.

## Objetivo

Definir como representar uma tarefa, suas dependências, uma tentativa de execução e a evidência necessária para aceitar seu resultado. Os contratos devem permitir trocar o destino de execução sem reescrever a tarefa.

## Base arquitetural

- Aplicação modular, inicialmente local e com execução sequencial.
- Papéis, modelos, provedores, executores e runtimes possuem identidades distintas.
- O Orchestrator controla estados, limites e tentativas.
- O MCP expõe conhecimento e ferramentas; não concentra o controle do workflow.
- A comparação inicial proposta é entre ambientes completos de agentes, registrando também modelo, ferramentas e configuração.
- O grafo de tarefas é definido desde o início, mesmo antes do paralelismo.
- Tentativas, contexto, avaliação e consumo serão registrados desde a primeira execução real.
- A implementação futura dos contratos usará JSON Schema, complementado por validação semântica. Esta entrega contém apenas especificação em Markdown.

## Dependências agora

Nenhuma biblioteca de aplicação é necessária para revisar esta etapa. Precisamos apenas de arquivos Markdown e acesso ao diretório do projeto. Git será útil para versionar decisões; Obsidian é opcional para leitura e edição do Vault futuro.

Não são requisitos desta fase: chaves de API, SDKs de agentes, servidor MCP em execução, embeddings, banco vetorial, Docker ou serviços remotos.

## Ordem dos contratos

1. Glossário e convenções comuns: identidades, versões, referências e unidades.
2. TaskSpec e seus objetos menores: Requirement, InputSpec, OutputSpec, KnowledgeRequirement, EvaluationCriterion e limites.
3. WorkflowSpec: agrupamento das tarefas, referências entre elas, validade do DAG e orçamento global.
4. Perfis de papel, modelo e executor: elegibilidade e tetos de permissão.
5. ExecutionPolicy: limites, permissões, roteamento, orçamento e validade de evidências.
6. ContextManifest e RoutingDecision: quais informações e decisões antecederam a execução.
7. ArtifactRef, RunManifest, AttemptRecord e EvaluationReport: entregas, condições da execução, fatos observados e aceitação.
8. KnowledgeSource, KnowledgeUnit, catálogo e seleção: elegibilidade, procedência e materialização do conhecimento.

Essa é uma ordem de especificação, não uma sequência de serviços a implementar. Algumas regras são revisadas em conjunto, especialmente TaskSpec e WorkflowSpec.

## Entregas pequenas

| Entrega | Conteúdo | Condição de conclusão |
| --- | --- | --- |
| 1A | Glossário e fronteiras | Os termos centrais não possuem significados conflitantes |
| 1B | Contratos e campos | Cada campo tem tipo, obrigatoriedade, origem e semântica |
| 1C | Estados e invariantes | Sucesso, falha, retry e resultado indeterminado são distintos |
| 1D | Cenários de aceitação | Há exemplos positivos e negativos com resultados esperados |
| 1E | Revisão da versão 0.1 | Uma execução fictícia completa é representável sem decisões implícitas |

## Dependências posteriores

| Momento | Necessidade | Decisão a tomar |
| --- | --- | --- |
| Formalização dos schemas | Validador JSON Schema e ferramenta de testes | Escolher a linguagem antes das bibliotecas e versões |
| RAG Markdown | Leitura segura de YAML/Markdown e busca local | Manter metadata e busca lexical como ponto de partida |
| MCP de conhecimento | SDK e cliente MCP compatíveis | Fixar versão do protocolo e transporte suportados |
| Primeiro executor | Integração programática, credenciais e workspace de teste | Validar acesso, permissões e telemetria do runtime escolhido |
| Primeira execução real | Persistência local e armazenamento de artefatos | Registrar decisões e resultados antes de expandir relatórios |

## Critério de saída da fase

Uma execução fictícia deve representar: objetivo, plano válido, dois destinos elegíveis, seleção de um deles, contexto identificado, tentativa reprovada, retry limitado e avaliação final. Toda aprovação deve apontar para os artefatos efetivamente avaliados; custo desconhecido não pode ser convertido em zero.

O baseline v0.1 contém TaskSpec, WorkflowSpec, perfis, estados, limites, roteamento, contexto, política, artefatos, registros de execução/avaliação e metadata de conhecimento, com 83 cenários gerais, oito casos de perfis, dez de roteamento/contexto, 12 de política, 14 de artefatos, 16 de registros e 17 de conhecimento. Os casos ainda não são testes executados. A revisão cruzada representou uma execução completa e classificou todos os contratos centrais como aceitos para formalização executável.

## Andamento

| Entrega | Situação |
| --- | --- |
| 1A — glossário | Baseline v0.1 aceito |
| 1B — contratos | Baseline v0.1 aceito; escolhas operacionais permanecem versionadas |
| 1C — estados | Baseline v0.1 aceito; capacidades reais dos executores serão validadas depois |
| 1D — aceitação | Matriz e cenário fictício aceitos; fixtures executáveis são a próxima camada |
| 1E — revisão final | Concluída; execução ponta a ponta representada sem decisões normativas implícitas |

## Documentos desta entrega

- [Glossário](../glossary.md)
- [Contrato de tarefa — baseline v0.1](task-contract.md)
- [Contrato de workflow — baseline v0.1](workflow-contract.md)
- [Estados, limites e tentativas](execution-lifecycle.md)
- [Cenários de aceitação](acceptance-cases.md)
- [Perfis de papel, modelo e executor](agent-profiles.md)
- [Decisão de roteamento e manifesto de contexto](routing-context-contract.md)
- [Política de execução](execution-policy-contract.md)
- [Artefatos, patches e relatórios](artifact-contract.md)
- [Registros de execução, uso, aprovação e avaliação](execution-records-contract.md)
- [Metadata de conhecimento do Vault](knowledge-metadata-contract.md)
- [Revisão cruzada e baseline v0.1](cross-review.md)

O próximo passo é decidir linguagem/tooling de schemas e serialização canônica, registrando as escolhas antes de implementar referências comuns, TaskSpec e WorkflowSpec com fixtures determinísticas. Integrações de agentes, MCP e Jev continuam fora dessa primeira implementação.
