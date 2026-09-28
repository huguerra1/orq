# Fase 2 — Contratos executáveis e Vault local

Estado: em andamento. O primeiro incremento executável está concluído; Vault e recuperação ainda não foram implementados.

## Escopo

Esta fase transforma o baseline documental v0.1 em schemas, validadores e fixtures e depois implementa o Vault Markdown com recuperação determinística por metadata e busca lexical.

Continuam fora desta fase: MCP, chamadas reais a agentes, Jev, scheduler concorrente e efeitos externos.

## Decisões fechadas

- [ADR 0001](../adr/0001-contract-tooling-and-canonical-json.md): Python 3.12, JSON Schema Draft 2020-12, `jsonschema`, `pytest`, RFC 8785/JCS e SHA-256.
- Schemas são o contrato estrutural; relações, ciclos e cobertura usam validação semântica explícita.
- Schemas remotos não são carregados durante validação normal.
- Dinheiro permanece decimal textual; números incompatíveis com I-JSON/JCS são rejeitados antes do digest.

## Entregue

| Incremento | Conteúdo | Verificação |
| --- | --- | --- |
| 2A | Tipos comuns, TaskSpec e WorkflowSpec em JSON Schema | Meta-schema Draft 2020-12 |
| 2A | Validador semântico de IDs, dependências, ciclos, inputs, outputs e cobertura | Fixtures e testes determinísticos |
| 2A | CLI de validação e digest canônico | Execução local sem rede |
| 2A | Dependências diretas e transitivas fixadas | Ambiente virtual reproduzível |

Resultado atual: 24 testes aprovados. Isso é evidência local do incremento executável, não validação de runtime externo.

## Interfaces atuais

- `orq-contracts validate task <arquivo.json>`
- `orq-contracts validate workflow <arquivo.json>`
- `orq-contracts digest <arquivo.json>`
- API Python: `validate_task`, `validate_workflow`, `canonicalize` e `canonical_digest`.

Erros retornam `code`, JSON Pointer em `path` e `message`. Validação estrutural ocorre antes da semântica para não interpretar documentos malformados.

## Próximos incrementos

1. Formalizar KnowledgeSource, CatalogStatusAssertion, KnowledgeUnit, KnowledgeCatalogSnapshot e KnowledgeSelectionRecord.
2. Implementar ingestão segura de Markdown/frontmatter e catálogo autorizado.
3. Implementar chunking determinístico e busca lexical local.
4. Materializar seleções com procedência e orçamento para ContextManifest.
5. Só então expor operações pequenas por MCP.

Antes do item 2, escolher e registrar parser Markdown/YAML e layout físico do Vault. Não usar frontmatter como fonte de autoridade.
