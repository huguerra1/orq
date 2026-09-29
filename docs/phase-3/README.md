# Fase 3 — MCP local de conhecimento

Estado: especificação inicial aceita; implementação ainda não iniciada.

## Problema

O Vault já ingere, seleciona e materializa conhecimento com integridade. A próxima etapa precisa disponibilizar unidades exatas pelo protocolo MCP sem transferir ao servidor autoridade para selecionar fontes, mudar política, ampliar escopo ou escrever no catálogo.

## Fronteira recomendada

- O Orchestrator continua responsável por requisitos, filtros, ranking, orçamento e KnowledgeSelectionRecord.
- O servidor MCP recebe na inicialização uma raiz de Vault e um KnowledgeCatalogSnapshot confiáveis e imutáveis durante seu processo.
- O cliente nunca fornece caminho de arquivo. Ele usa apenas `source_id`, `revision` e `unit_id` já existentes no snapshot.
- Conhecimento é exposto como **resource**, pois a aplicação decide anexá-lo ao contexto. Busca livre como tool permitiria ao modelo contornar a seleção e fica fora do MVP.
- O servidor é somente leitura. Não publica, revoga, seleciona, resume, transforma ou grava artefatos.

## Recursos v0.1

| URI | Resultado | Limites |
| --- | --- | --- |
| `orq://catalog/summary` | Identidade do catálogo e fontes/unidades ativas sem locator físico | JSON canônico, tamanho limitado |
| `orq://knowledge/{source_id}/{revision}/{unit_id}` | Bytes UTF-8 exatos da unidade com metadata de integridade | Referência exata, digest reconfirmado |

Uma unidade inexistente, fora do snapshot, alterada ou acima do limite retorna erro identificado. Não há fallback por título, revisão próxima ou conteúdo semelhante.

## Transporte e lifecycle

- SDK oficial Python `mcp==2.2.0`, fixado exatamente para o primeiro baseline.
- Transporte de produção inicial: `stdio`, local e sem listener de rede.
- Testes: cliente oficial conectado ao servidor em memória.
- O catálogo é lido e validado uma vez na criação do servidor. Mudança de revisão ou catálogo inicia outro processo/configuração.
- Logs e diagnósticos nunca usam stdout durante `stdio`.

## Erros e segurança

- Entrada MCP é não confiável e limitada pelos schemas gerados pelo SDK e pelas validações do domínio.
- IDs são resolvidos em índices do snapshot; não são concatenados em caminhos.
- O materializador existente reconfirma digest de fonte e unidade antes de devolver conteúdo.
- Erros esperados informam código estável, sem revelar caminhos físicos, stack traces ou conteúdo adicional.
- Segredos, credenciais, paths internos e conteúdo de outras unidades não aparecem no resumo do catálogo.
- Timeout e cancelamento não mudam estado porque os handlers são somente leitura.

## Testes exigidos

1. descoberta lista somente os recursos previstos;
2. resumo não contém `content_locator`;
3. leitura exata retorna conteúdo e digests esperados;
4. IDs desconhecidos não resolvem outra unidade;
5. mutação após snapshot produz `integrity_mismatch`;
6. servidor não expõe ferramentas de seleção ou escrita;
7. processo `stdio` inicia sem texto estranho no canal do protocolo.

## Fora desta fase

- HTTP, OAuth, servidor remoto ou multi-tenant;
- prompts MCP, sampling e elicitation;
- seleção feita pelo modelo;
- Task Planner, Model Router, TypeSafe/Jev e executores.

## Referências

- [SDKs oficiais e classificação Tier 1](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/2026-07-28/sdk.mdx).
- [MCP Python SDK 2.2.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0).
- [Primitivas e semântica de resources/tools](https://py.sdk.modelcontextprotocol.io/get-started/first-steps/).
- [Testes com Client em memória](https://py.sdk.modelcontextprotocol.io/get-started/testing/).
