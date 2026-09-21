# ORQ

Plataforma experimental de orquestração de agentes de IA com ferramentas e conhecimento acessíveis por MCP.

O projeto está na Fase 1: especificação dos contratos. Ainda não há aplicação executável, dependências instaláveis ou chamadas a provedores.

## Princípios

- Separar papel, modelo, provedor, runtime e executor.
- Manter o controle do workflow na aplicação e ferramentas pequenas no MCP.
- Começar com execução local e sequencial, preservando a estrutura de DAG.
- Registrar decisões, contexto, tentativas, artefatos e avaliação desde a primeira execução real.
- Definir arquitetura e critérios de aceitação antes de implementar cada etapa.

## Retomar em outra conversa

Leia [AGENTS.md](AGENTS.md) e [docs/HANDOFF.md](docs/HANDOFF.md) antes de continuar. Eles registram as regras, o estado atual e o próximo passo.

## Leitura inicial

1. [Glossário](docs/glossary.md).
2. [Escopo e andamento da Fase 1](docs/phase-1/README.md).
3. [Contrato de tarefa](docs/phase-1/task-contract.md).
4. [Contrato de workflow](docs/phase-1/workflow-contract.md).
5. [Estados, limites e tentativas](docs/phase-1/execution-lifecycle.md).
6. [Cenários de aceitação](docs/phase-1/acceptance-cases.md).
7. [Perfis de papel, modelo e executor](docs/phase-1/agent-profiles.md).
8. [Decisão de roteamento e manifesto de contexto](docs/phase-1/routing-context-contract.md).
9. [Política de execução](docs/phase-1/execution-policy-contract.md).
10. [Artefatos, patches e relatórios](docs/phase-1/artifact-contract.md).

## Próxima entrega

Definir RunManifest, AttemptRecord, PatchApplicationRecord, ApprovalRecord, uso/custos/erros e EvaluationReport. Os contratos continuam sujeitos a revisão; a Fase 1 ainda não está concluída.
