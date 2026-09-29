# ORQ

Plataforma experimental de orquestração de agentes de IA com ferramentas e conhecimento acessíveis por MCP.

O baseline documental v0.1 da Fase 1 está concluído. A Fase 2 já possui schemas, validadores, fixtures, CLI e recuperação lexical segura do Vault; ainda não há Orchestrator, MCP ou chamadas a provedores.

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
11. [Registros de execução e avaliação](docs/phase-1/execution-records-contract.md).
12. [Metadata de conhecimento do Vault](docs/phase-1/knowledge-metadata-contract.md).
13. [Revisão cruzada e fechamento do baseline v0.1](docs/phase-1/cross-review.md).
14. [Andamento da Fase 2](docs/phase-2/README.md).

## Executar os contratos

Requer Python 3.12:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pytest
.venv/bin/orq-contracts validate workflow fixtures/v0.1/valid/workflow-minimal.json
```

As dependências resolvidas do ambiente verificado também estão em `requirements.lock`.

## Próxima entrega

Adicionar fixtures em disco e CLI para catálogo, seleção e ContextManifest; depois fechar a Fase 2. MCP e Jev permanecem posteriores a esse núcleo.
