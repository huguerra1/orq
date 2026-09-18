# Fase 1 — Contratos da plataforma

Estado: especificação em andamento, para revisão. Nenhum runtime, biblioteca ou serviço foi implementado ou instalado.

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
4. Perfis de papel, modelo e executor: elegibilidade e permissões.
5. ContextManifest e RoutingDecision: quais informações e decisões antecederam a execução.
6. RunManifest, AttemptRecord e EvaluationReport: condições da execução, fatos observados e aceitação.

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

Já existem propostas documentais de TaskSpec, WorkflowSpec, estados, limites e 32 casos de aceitação. Os casos ainda não são testes executados. A fase não está encerrada: faltam perfis e contratos de contexto, decisão, artefatos, metadata de conhecimento, registros de execução e avaliação.

## Andamento

| Entrega | Situação |
| --- | --- |
| 1A — glossário | Primeira versão documentada |
| 1B — contratos | Tarefa e workflow propostos; demais estruturas pendentes |
| 1C — estados | Proposta documentada; capacidades reais dos executores serão validadas depois |
| 1D — aceitação | Matriz e cenário fictício documentados; fixtures executáveis pendentes |
| 1E — revisão final | Pendente; Fase 1 ainda não concluída |

## Documentos desta entrega

- [Glossário](../glossary.md)
- [Contrato de tarefa — proposta inicial](task-contract.md)
- [Contrato de workflow — proposta inicial](workflow-contract.md)
- [Estados, limites e tentativas](execution-lifecycle.md)
- [Cenários de aceitação](acceptance-cases.md)

O próximo passo é especificar perfis de papel, modelo e executor, distinguindo capacidades declaradas de capacidades efetivamente controláveis. Não é necessário instalar dependências. A implementação deve começar apenas depois do fechamento dos contratos correspondentes.
