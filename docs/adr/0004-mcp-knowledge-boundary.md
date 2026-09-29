# ADR 0004 — Fronteira MCP do conhecimento

Estado: aceito em 2026-09-29.

## Contexto

O ORQ precisa oferecer conhecimento por MCP sem permitir que o modelo refaça seleção, ignore autoridade ou leia caminhos arbitrários. O primeiro transporte deve ser local e testável sem rede.

## Alternativas

| Alternativa | Vantagens | Limitações |
| --- | --- | --- |
| JSON-RPC/MCP manual | Sem dependência adicional | Maior risco de incompatibilidade e framing incorreto |
| SDK oficial Python | Protocolo, schemas, transporte e cliente de teste mantidos | Dependência e mudanças entre versões |
| API HTTP própria | Familiar | Não atende ao objetivo MCP |

Para o conteúdo, tools de busca dariam autonomia ao modelo; resources exatos mantêm a decisão no host.

## Decisão

- Adotar o SDK oficial Python `mcp==2.2.0`, com pin exato e atualização deliberada.
- Usar `MCPServer`, `stdio` e `Client(server)` em memória nos testes.
- Expor somente resources de resumo sanitizado do catálogo e unidade exata.
- Configuração de raiz e snapshot ocorre fora dos argumentos MCP e é confiada ao processo local.
- Reutilizar `materialize_unit`; o servidor não duplica parsing, autorização ou integridade.
- Não expor operações de catálogo, ranking, seleção, contexto, política ou escrita.

## Consequências

- O SDK pode evoluir sem contaminar o núcleo: imports e handlers ficam em módulo adaptador.
- Um upgrade exige revisar release notes, lock e testes MCP.
- `stdio` evita superfície HTTP/OAuth no MVP, mas ainda exige stdout reservado ao protocolo.
- Resources preservam controle do host; agentes recebem apenas o que o Orchestrator escolheu materializar.

## Referências

- [Release 2.2.0 do SDK oficial](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0).
- [Documentação oficial de servidores](https://py.sdk.modelcontextprotocol.io/servers/).
- [Documentação oficial de testes em memória](https://py.sdk.modelcontextprotocol.io/get-started/testing/).
