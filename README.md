# ORQ

Plataforma experimental de orquestração de agentes de IA com ferramentas e conhecimento acessíveis por MCP.

O baseline documental v0.1 e as Fases 2–5 estão concluídos. Já existem contratos executáveis, Vault/CLI, MCP somente leitura, Task Planner determinístico e Model Router com baselines `fixed`/`rules` e adaptador opcional TypeSafe/Jev; ainda não há runner do Orchestrator nem chamadas reais a provedores.

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
15. [Andamento da Fase 3](docs/phase-3/README.md).
16. [Andamento da Fase 4](docs/phase-4/README.md).
17. [Andamento da Fase 5](docs/phase-5/README.md).

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

Para executar a fixture completa do Vault, use três saídas novas:

```bash
.venv/bin/orq-contracts vault catalog --root fixtures/v0.1/vault --definition fixtures/v0.1/vault/catalog-definition.json --output /tmp/orq-catalog.json
.venv/bin/orq-contracts vault select --root fixtures/v0.1/vault --catalog /tmp/orq-catalog.json --request fixtures/v0.1/vault/selection-request.json --output /tmp/orq-selection.json
.venv/bin/orq-contracts vault context --root fixtures/v0.1/vault --catalog /tmp/orq-catalog.json --selection /tmp/orq-selection.json --request fixtures/v0.1/vault/context-request.json --output-dir /tmp/orq-context
```

## Próxima entrega

Especificar a Fase 6 e então implementar o primeiro executor, a persistência mínima e o runner sequencial do DAG.
