# ADR 0002 — Layout do Vault e parsing Markdown

Estado: aceito em 2026-09-28.

## Contexto

O Vault precisa manter autoria simples em Markdown sem permitir que o documento atribua a si mesmo projeto, autoridade, classificação ou poder instrucional. Chunking deve preservar headings, code fences e tabelas e produzir unidades reproduzíveis.

## Alternativas

| Alternativa | Vantagens | Limitações |
| --- | --- | --- |
| Regex para frontmatter e headings | Poucas dependências | Frágil para YAML, fences, tabelas e nesting |
| Biblioteca de frontmatter completa | API conveniente | Pode esconder loader/configuração e ampliar campos aceitos |
| Extração delimitada + safe YAML + AST Markdown | Fronteiras explícitas e chunking estrutural | Exige código de composição próprio |

## Decisão

- Layout inicial: `vault/content/operational/` e `vault/content/projects/<project_id>/` para Markdown; `vault/catalog/` para regras e atribuições autorizadas.
- Caminhos do catálogo são relativos à raiz do Vault, normalizados em POSIX e não podem escapar por `..`, absoluto ou symlink.
- Frontmatter é opcional, precisa começar no primeiro byte com `---` e termina em outro delimitador `---`; tamanho máximo inicial de 64 KiB.
- PyYAML 6.0.3 somente com `safe_load`; tags não seguras são rejeitadas.
- Campos editoriais iniciais aceitos: `title`, `language`, `topics` e `relationships`.
- Campos sensíveis no frontmatter são erro explícito: `project_scope`, `authority`, `instruction_scope`, `data_classification`, `status`, validade e owner.
- `markdown-it-py` 4.2.0 em modo CommonMark com tabela habilitada produz blocos e intervalos de linhas.
- Uma unidade inicial corresponde ao preâmbulo ou ao bloco iniciado por um heading até o heading seguinte; `heading_path` preserva a hierarquia. Code fences e tabelas não são cortados.
- Unidade acima do limite é marcada `oversized`; não é truncada nem resumida silenciosamente.
- Ranking inicial usa BM25 versionado (`bm25-v1`, k1 1,2 e b 0,75) após filtros determinísticos. Desempate: score, source_id, revision, ordinal e unit_id.

## Autoridade e segurança

O catálogo autorizado fornece os campos sensíveis. Frontmatter nunca os substitui. O conteúdo Markdown e a query são dados não confiáveis; instruções só têm o alcance previamente atribuído no catálogo.

Arquivos são lidos com limite de bytes, UTF-8 estrito e digest antes de parsing. O caminho real precisa permanecer na raiz autorizada. O parser não renderiza HTML nem executa código.

## Consequências

- Alterar parser, opções, política de unidade ou parâmetros BM25 cria nova versão/hash de política.
- O MVP não usa embeddings nem banco vetorial.
- O catálogo JSON é deliberadamente separado do conteúdo editorial.
- Tokenização de modelo continua aberta; o seletor usa orçamento conservador configurado e registra o método.

## Testes exigidos

- frontmatter seguro e campos sensíveis rejeitados;
- YAML com tag de objeto rejeitado;
- path traversal e symlink fora do Vault rejeitados;
- digest divergente rejeitado;
- headings, fences e tabelas permanecem íntegros;
- oversized explícito;
- filtro de projeto/autoridade antes do BM25;
- ranking e desempate reproduzíveis.

## Referências

- [PyYAML e safe_load](https://pyyaml.org/wiki/PyYAMLDocumentation)
- [PyYAML 6.0.3](https://pypi.org/project/PyYAML/)
- [markdown-it-py 4.2.0](https://pypi.org/project/markdown-it-py/)
