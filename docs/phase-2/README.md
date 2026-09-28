# Fase 2 — Contratos executáveis e Vault local

Estado: em andamento. Contratos iniciais, ingestão e chunking do Vault estão implementados; seleção/ranking ainda não.

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
| 2B | Schemas de fonte, catálogo e seleção de conhecimento | Meta-schema Draft 2020-12 |
| 2B | Ingestão Markdown com UTF-8 estrito, limites, digest e path seguro | Testes de traversal, symlink e integridade |
| 2B | Frontmatter seguro e metadata sensível exclusiva do catálogo | Testes de YAML hostil e autoelevação |
| 2B | Chunking determinístico por headings, preservando fences/tabelas | Spans, digests e oversized explícitos |
| 2B | Snapshot de catálogo com status append-only | Revogação e conflito verificáveis |

Resultado atual: 33 testes aprovados. Isso é evidência local dos incrementos executáveis, não validação de runtime externo.

## Interfaces atuais

- `orq-contracts validate task <arquivo.json>`
- `orq-contracts validate workflow <arquivo.json>`
- `orq-contracts digest <arquivo.json>`
- API Python: `validate_task`, `validate_workflow`, `canonicalize` e `canonical_digest`.
- API Python do Vault: `ingest_markdown_source` e `build_catalog_snapshot`.

Erros retornam `code`, JSON Pointer em `path` e `message`. Validação estrutural ocorre antes da semântica para não interpretar documentos malformados.

## Próximos incrementos

1. Implementar filtros determinísticos e BM25 versionado sobre as unidades elegíveis.
2. Produzir KnowledgeSelectionRecord com cobertura obrigatória, exclusões e orçamento.
3. Materializar seleções com procedência para ContextManifest.
4. Adicionar fixtures de Vault em disco e CLI de catálogo/seleção.
5. Só então expor operações pequenas por MCP.

Parser, layout e ranking inicial foram decididos na [ADR 0002](../adr/0002-vault-layout-and-markdown-parsing.md). Frontmatter não é fonte de autoridade.
