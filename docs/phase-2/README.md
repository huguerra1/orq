# Fase 2 — Contratos executáveis e Vault local

Estado: concluída em 2026-09-29 como baseline executável inicial dos contratos e do Vault local.

## Escopo

Esta fase transforma o baseline documental v0.1 em schemas, validadores e fixtures e depois implementa o Vault Markdown com recuperação determinística por metadata e busca lexical.

Continuam fora desta fase: MCP, chamadas reais a agentes, Jev, scheduler concorrente e efeitos externos.

## Decisões fechadas

- [ADR 0001](../adr/0001-contract-tooling-and-canonical-json.md): Python 3.12, JSON Schema Draft 2020-12, `jsonschema`, `pytest`, RFC 8785/JCS e SHA-256.
- [ADR 0002](../adr/0002-vault-layout-and-markdown-parsing.md): layout seguro, parsing Markdown estrutural e BM25 inicial.
- [ADR 0003](../adr/0003-context-bundle-materialization.md): ContextManifest separado de bundle JSON canônico, sem truncamento silencioso.
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
| 2B | Filtros determinísticos antes do ranking BM25 | Projeto, autoridade, classificação, validade e applicability |
| 2B | Seleção por requisito, cobertura e orçamento global/local | KnowledgeSelectionRecord validado e reproduzível |
| 2B | Materialização de unidade com nova verificação de digest | Mutação posterior do arquivo é rejeitada |
| 2B | ContextManifest e bundle ordenado em JSON canônico | Procedência, digests e orçamento validados |
| 2B | Fixtures reais e CLI de catálogo, seleção e contexto | Cadeia local reproduzível e sem sobrescrita implícita |

Resultado final: 52 testes aprovados. Isso é evidência local dos incrementos executáveis, não validação de runtime externo.

## Interfaces atuais

- `orq-contracts validate task <arquivo.json>`
- `orq-contracts validate workflow <arquivo.json>`
- `orq-contracts digest <arquivo.json>`
- `orq-contracts vault catalog --root <vault> --definition <json> --output <json>`
- `orq-contracts vault select --root <vault> --catalog <json> --request <json> --output <json>`
- `orq-contracts vault context --root <vault> --catalog <json> --selection <json> --request <json> --output-dir <diretório-novo>`
- API Python: `validate_task`, `validate_workflow`, `canonicalize` e `canonical_digest`.
- API Python do Vault: `ingest_markdown_source`, `build_catalog_snapshot`, `select_knowledge` e `materialize_unit`.
- API Python de contexto: `materialize_knowledge_context` e `validate_context_manifest`.

Erros retornam `code`, JSON Pointer em `path` e `message`. Validação estrutural ocorre antes da semântica para não interpretar documentos malformados.

## Encerramento

O gate para MCP está satisfeito: as operações locais têm contratos fechados, fixtures, erros explícitos e testes determinísticos. A Fase 3 deve expor operações pequenas sobre essas funções; MCP não recebe autoridade para alterar catálogo, seleção, política ou estado do Orchestrator.

Parser, layout e ranking inicial foram decididos na [ADR 0002](../adr/0002-vault-layout-and-markdown-parsing.md). Frontmatter não é fonte de autoridade.
