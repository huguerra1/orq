# Memória por família em Postgres, Row-Level Security e entrega via MCP

Estado: aceita em 2026-11-14 ([ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md)). Substitui parcialmente a restrição da [ADR 0002](../adr/0002-vault-layout-and-markdown-parsing.md) sobre banco de dados, estende o [contrato de metadata de conhecimento](../phase-1/knowledge-metadata-contract.md), a [materialização de contexto](../adr/0003-context-bundle-materialization.md) e a [fronteira MCP da Fase 3](../phase-3/README.md). O Vault Markdown com `bm25-v1` permanece ativo como **modo offline**.

## Problema

Nas Fases 2 e 3, todo o conhecimento vive em arquivos Markdown locais e o seletor aplica filtros determinísticos seguidos por `bm25-v1` em memória, expondo unidades exatas como `resources` MCP estáticos. Quando o ORQ passa a orquestrar quatro famílias de assinaturas reais (**Claude**, **Codex**, **Gemini** e **Kiro**) combinadas com múltiplos papéis (`software_architect`, `backend_engineer`, `tester`, `code_reviewer`, `security_reviewer`), surgem três necessidades:

1. **Direcionamento por família e papel:** cada família de modelo/assinatura tem particularidades operacionais próprias (flags de CLI, comportamento de sandbox no Windows/WSL, formatos de ferramenta e armadilhas conhecidas) e cada papel exige recortes específicos do conhecimento. Misturar instruções exclusivas do Codex ou do Kiro no contexto do Claude desperdiça orçamento de tokens e cria interferência entre instruções.
2. **Isolamento forçado pela camada de dados:** o isolamento entre a área exclusiva de uma família e a de outra não pode depender apenas de um `if` na camada cliente ou do bom comportamento do modelo; deve ser garantido por **Row-Level Security (RLS)** no banco de dados.
3. **Busca textual rica sem modelo externo, com escolha final pelo agente:** consultas reais em português combinam termos flexionados com identificadores de código e nomes de flags. O agente precisa buscar candidatos dentro da sua área permitida (área da sua família + área comum) e fazer a **escolha final** de quais unidades incorporar à tentativa, sem exigir modelos de embeddings (que pediriam API paga ou LLM local) e sem abrir mão de consolidar tudo em um `ContextManifest` auditável com digests SHA-256.

## Famílias de agentes, áreas de consulta e audiência

### Definição de `AgentFamily`

Uma **família de agentes** identifica o agrupamento lógico ao qual uma tentativa pertence no momento da execução. Ela combina dois eixos **independentes** declarados no catálogo (decisão do dono de 2026-11-14: não existe chave composta `assinatura:papel`; a audiência de cada unidade declara cada eixo, e a visibilidade combina os dois):

- **Eixo de modelo/assinatura (`family_id`):** `family_claude`, `family_codex`, `family_gemini` ou `family_kiro` (além de `family_simulated` nos testes do executor simulado).
- **Eixo de papel (`role_id`):** identificador do `AgentRoleProfile` da tarefa (por exemplo, `backend_engineer`, `software_architect`, `security_reviewer`).

### Área por família e área comum

Todo conhecimento publicado no catálogo autorizado recebe uma política de audiência (`AudienceMetadata`) que o classifica em uma de duas áreas:

| Área (`area_scope`) | Visibilidade | Exemplos de conteúdo |
| --- | --- | --- |
| **Área comum (`common`)** | Visível para **todas** as famílias de agentes autorizadas no projeto e escopo operacional | Contratos centrais da arquitetura, convenções gerais do repositório, regras de Git, glossário e requisitos de negócio do projeto |
| **Área da família (`family`)** | Visível **somente** quando a sessão satisfaz `target_families` (lista de `family_id`) e/ou `target_roles` (lista de `role_id`) | Guia de flags e limites de sandbox do Codex no Windows (`--pool threads`), regras de permissão do `agy`, restrições de `--trust-tools` no Kiro, checklist específico de `security_reviewer` |

Regras de audiência:

- Quando `area_scope = "common"`, `target_families` e `target_roles` são vazios (visível a qualquer família/papel compatível com o `project_scope` e `data_classification`).
- Quando `area_scope = "family"`, pelo menos uma das listas (`target_families` ou `target_roles`) é não vazia. Se ambas estiverem preenchidas, a unidade exige que a tentativa pertença a uma das famílias listadas **e** a um dos papéis listados (interseção determinística).
- Assim como `authority` e `project_scope` na Fase 1, `area_scope`, `target_families` e `target_roles` são campos sensíveis atribuídos pelo **catálogo autorizado** (`CatalogPolicy`), nunca aceitos por autoafirmação livre no frontmatter do Markdown.

## PostgreSQL configurável via Docker e isolamento por Row-Level Security (RLS)

### Infraestrutura configurável

- O banco PostgreSQL (`postgres:16-alpine`) é provisionado via `docker-compose.yml` local, inteiramente configurável por variáveis de ambiente (`.env` baseado em `.env.example`, sem credenciais commitadas no Git): `ORQ_PG_HOST`, `ORQ_PG_PORT`, `ORQ_PG_DB`, `ORQ_PG_ADMIN_USER`, `ORQ_PG_READER_USER` e `ORQ_KNOWLEDGE_BACKEND` (`postgres` ou `offline_markdown_bm25`).
- O processo de sincronização/migração conecta-se com a role administrativa (`orq_admin`), enquanto o servidor MCP e o seletor de execução conectam-se exclusivamente com a role restrita e somente leitura (`orq_reader`), que **não** possui `SUPERUSER` nem `BYPASSRLS`.

### Esquema relacional e políticas SQL de exemplo

O esquema armazena snapshots de catálogo, fontes, unidades e asserções de status, preservando os mesmos digests JCS/SHA-256 da Fase 2:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE TABLE knowledge_units (
    catalog_id       TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    revision         TEXT NOT NULL,
    unit_id          TEXT NOT NULL,
    ordinal          INTEGER NOT NULL,
    project_id       TEXT, -- NULL somente para operational global autorizado
    knowledge_kind   TEXT NOT NULL CHECK (knowledge_kind IN ('operational', 'project')),
    status           TEXT NOT NULL CHECK (status IN ('active', 'deprecated', 'revoked')),
    authority_level  TEXT NOT NULL,
    authority_scope  TEXT NOT NULL,
    instruction_scope TEXT NOT NULL CHECK (instruction_scope IN ('none', 'project', 'operational')),
    classification   TEXT NOT NULL,
    area_scope       TEXT NOT NULL CHECK (area_scope IN ('common', 'family')),
    target_families  TEXT[] NOT NULL DEFAULT '{}',
    target_roles     TEXT[] NOT NULL DEFAULT '{}',
    heading_path     TEXT[] NOT NULL,
    title            TEXT NOT NULL,
    content          TEXT NOT NULL,
    content_digest   TEXT NOT NULL,
    source_digest    TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    search_tsv       TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('portuguese', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('portuguese', coalesce(array_to_string(heading_path, ' '), '')), 'B') ||
        setweight(to_tsvector('portuguese', coalesce(content, '')), 'C')
    ) STORED,
    PRIMARY KEY (catalog_id, source_id, revision, unit_id)
);

CREATE INDEX idx_knowledge_units_tsv ON knowledge_units USING GIN (search_tsv);
CREATE INDEX idx_knowledge_units_trgm ON knowledge_units USING GIN (content gin_trgm_ops);
CREATE INDEX idx_knowledge_units_families ON knowledge_units USING GIN (target_families);
CREATE INDEX idx_knowledge_units_roles ON knowledge_units USING GIN (target_roles);
```

Isolamento estrito via **Row-Level Security (RLS)**:

```sql
ALTER TABLE knowledge_units ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_units FORCE ROW LEVEL SECURITY;

GRANT SELECT ON knowledge_units TO orq_reader;

CREATE POLICY family_and_project_isolation_policy
ON knowledge_units
FOR SELECT
TO orq_reader
USING (
    -- 1. Mesmo snapshot de catálogo fixado pelo RunManifest e status ativo
    catalog_id = current_setting('app.catalog_id', true)
    AND status = 'active'
    -- 2. Isolamento de projeto (operacional global ou projeto exato do run)
    AND (
        (knowledge_kind = 'operational' AND project_id IS NULL)
        OR project_id = current_setting('app.current_project_id', true)
    )
    -- 3. Área comum OU área compatível com a família e o papel da sessão
    AND (
        area_scope = 'common'
        OR (
            area_scope = 'family'
            AND (
                cardinality(target_families) = 0
                OR current_setting('app.current_family_id', true) = ANY (target_families)
            )
            AND (
                cardinality(target_roles) = 0
                OR current_setting('app.current_role_id', true) = ANY (target_roles)
            )
        )
    )
);
```

A cada transação de consulta, o adaptador de banco do ORQ abre uma transação somente leitura (`BEGIN TRANSACTION READ ONLY`) e fixa o contexto antes de qualquer `SELECT`:

```sql
SET LOCAL app.catalog_id = 'cat-v0.1-001';
SET LOCAL app.current_project_id = 'proj-orq';
SET LOCAL app.current_family_id = 'family_codex';
SET LOCAL app.current_role_id = 'backend_engineer';
```

Como `SET LOCAL` vale apenas durante a transação corrente e os valores vêm do `RunManifest` e da `RoutingDecision` no processo host (nunca da string de busca do agente), um agente executando como `family_codex` **jamais** consegue listar, contar ou ler linhas exclusivas de `family_claude`, `family_gemini` ou `family_kiro`.

## Busca full-text em português + `pg_trgm` com escolha final pelo agente

### Ranking híbrido sem embeddings

Para respeitar a diretriz de **zero API paga e zero LLM local**, a Fase 7 não utiliza embeddings. A busca no PostgreSQL combina dois sinais determinísticos sobre o conjunto já filtrado pelo RLS e pelos requisitos de autoridade/classificação:

1. **Full-Text Search em português (`ts_rank_cd`):** usa `websearch_to_tsquery('portuguese', :query)` contra a coluna gerada `search_tsv` (com pesos `A` para título, `B` para `heading_path` e `C` para o corpo), tratando plural, conjugações e stopwords da língua portuguesa.
2. **Similaridade por trigramas (`pg_trgm`):** calcula `word_similarity(:query, title || ' ' || array_to_string(heading_path, ' ') || ' ' || content)` para recuperar identificadores exatos ou parciais de código, nomes de schemas (`DispatchIntent`, `QuotaStateRecord`), flags de CLI (`--trust-tools`, `--no-interactive`) e termos com pequenas variações ortográficas que o stemmer português não reduz.

A pontuação combinada é versionada (`pg-fts-trgm-v1`, por exemplo `score = 0.70 * norm_fts + 0.30 * trgm_sim`), mantendo a mesma regra de desempate determinística da Fase 2: `(score DESC, source_id ASC, revision ASC, ordinal ASC, unit_id ASC)`.

### Escolha final pelo próprio agente

Diferentemente da seleção puramente estática da Fase 2 (em que o host escolhia sozinho o top-$k$ até encher o orçamento), na Fase 7 o fluxo de recuperação em modo conectado divide a responsabilidade em duas etapas:

1. **Geração de candidatos sob guarda do host:** a ferramenta de busca retorna ao agente uma lista enxuta de candidatos elegíveis (filtrados por RLS, projeto, catálogo, validade e autoridade), contendo para cada unidade: `source_id`, `revision`, `unit_id`, `area_scope` (`common` ou `family`), `heading_path`, `unit_kind`, `estimated_tokens`, `content_digest`, `score` e um excerto curto.
2. **Escolha final pelo agente:** analisando o objetivo da tarefa e os candidatos retornados, **o próprio agente seleciona quais `unit_id`s deseja materializar** para compor o seu contexto de trabalho. O host valida se a seleção do agente:
   - pertence exclusivamente ao subconjunto elegível retornado sob o RLS daquela família;
   - satisfaz a regra de cobertura (`coverage_rule`) dos `KnowledgeRequirements` obrigatórios da tarefa;
   - cabe dentro do orçamento de tokens disponível (`available_input_tokens` e `max_context_tokens`).

Se o agente omitir um requisito obrigatório ou exceder o orçamento, a ferramenta MCP informa o erro estruturado (`mandatory_requirement_uncovered` ou `context_budget_exceeded`) para que o agente ajuste a escolha antes de fechar a seleção.

### Evolução futura: embeddings

A adição de uma coluna `embedding vector(d)` (via extensão `pgvector`) fica documentada apenas como evolução futura opcional para fases posteriores, caso o dono decida autorizar um gerador de embeddings específico. Na Fase 7, nenhuma tabela ou fluxo depende de vetores densos.

## Entrega via ferramenta MCP com escopo da família (estendendo a Fase 3)

O servidor `orq-knowledge-mcp` da Fase 3 (`mcp==2.2.0`, transporte `stdio`) mantém os `resources` existentes (`orq://catalog/summary` e `orq://knowledge/{source_id}/{revision}/{unit_id}`, agora filtrados pelo RLS da família da sessão) e passa a expor duas **tools somente leitura**:

| Tool MCP | Argumentos aceitos do agente | Resposta | Garantias do host |
| --- | --- | --- | --- |
| `search_family_knowledge` | `requirement_id`, `query`, `limit` (opcional, teto `20`) | Lista ordenada de candidatos elegíveis (metadados, `area_scope`, `estimated_tokens`, `score` e excerto) | Executa sob `SET LOCAL` com `catalog_id`, `project_id`, `family_id` e `role_id` fixados na inicialização do servidor pelo runner; o agente **não pode** informar `family_id` ou `role_id` |
| `select_family_knowledge` | `selections` (mapa `requirement_id -> [unit_refs]`), `rationale` (opcional, limitado) | Confirmação da cobertura, orçamento consumido, unidades materializadas com digests e `selection_id` gerado | Verifica elegibilidade no snapshot/RLS, cobertura obrigatória, orçamento de tokens e integridade SHA-256 antes de devolver o conteúdo completo |

Regras da fronteira MCP na Fase 7:

- O processo `orq-knowledge-mcp` é iniciado pelo runner para uma tentativa específica, recebendo `run_id`, `task_id`, `attempt_number`, `catalog_id`, `project_id`, `family_id`, `role_id` e o orçamento de contexto por configuração confiável do processo host (fora dos argumentos MCP).
- O servidor continua **estritamente somente leitura** quanto ao catálogo e ao Vault: não cria fontes, não altera audiência, não modifica políticas e não escreve artefatos de tarefa.
- Toda chamada a `search_family_knowledge` e `select_family_knowledge` fica registrada em trilha auditável vinculada à tentativa.
- **Escolha durante a execução (decisão do dono de 2026-11-14):** o agente chama essas tools durante a própria execução da CLI, sem passo de pré-seleção. Ao final da tentativa, o runner audita a trilha do servidor MCP escopado (o que foi consultado e o que foi escolhido) e lacra o `ContextManifest` efetivo com os digests das unidades realmente entregues.

## Consolidação em `ContextManifest` com digests

A escolha final feita pelo agente não contorna a rastreabilidade da [ADR 0003](../adr/0003-context-bundle-materialization.md):

1. Ao concluir a seleção via `select_family_knowledge`, o host gera um `KnowledgeSelectionRecord` válido, registrando todos os candidatos retornados pelo Postgres (`pg-fts-trgm-v1`), as unidades efetivamente escolhidas pelo agente (`selection_mode: "agent_mcp_choice"`), a cobertura de cada `KnowledgeRequirement` e o `selection_hash`.
2. O materializador verifica novamente o `content_digest` de cada unidade escolhida e monta o **`ContextManifest`** fechado e o **bundle JSON canônico (RFC 8785 / JCS)** com `bundle_hash`.
3. Se uma tentativa sofrer fallback para outra família (por exemplo, `family_codex` esgotou a cota e a tentativa 2 vai para `family_claude`), o servidor MCP da tentativa 2 é iniciado com `app.current_family_id = 'family_claude'`, produzindo um novo `KnowledgeSelectionRecord` e um novo `ContextManifest` sem nenhuma unidade exclusiva de `family_codex`.

## Migração a partir do Vault Markdown e modo offline

### Sincronização Vault Markdown → PostgreSQL

O Vault em arquivos Markdown continua sendo a fonte primária de autoria humana:

1. O comando `orq-contracts vault sync-db --root <vault> --catalog <snapshot.json>` lê as fontes e unidades apenas por meio das funções já validadas na Fase 2 (`ingest_markdown_source` e `build_catalog_snapshot`).
2. O sincronizador valida as regras de audiência (`area_scope`, `target_families`, `target_roles`) definidas no catálogo autorizado, recalcula `source_digest` e `content_digest` de cada unidade e insere os registros no PostgreSQL de forma idempotente dentro de uma única transação.
3. Qualquer divergência de digest entre o arquivo Markdown e o snapshot ou tentativa de sobrescrever uma revisão existente com bytes diferentes aborta a transação (`catalog_sync_integrity_error`).

### Modo offline (`offline_markdown_bm25`)

Quando o PostgreSQL/Docker não estiver disponível ou quando o usuário executar em modo desconectado (`--offline` ou `ORQ_KNOWLEDGE_BACKEND=offline_markdown_bm25`):

- O ORQ utiliza diretamente os arquivos Markdown locais e o algoritmo `bm25-v1` da Fase 2.
- O filtro de audiência (`area_scope = 'common'` ou compatível com `family_id`/`role_id`) é aplicado deterministicamente em memória antes do cálculo do BM25, mantendo a mesma semântica de visibilidade por família.
- O `KnowledgeSelectionRecord` registra explicitamente `backend: "offline_markdown_bm25"`, distinguindo execuções offline das execuções sobre PostgreSQL (`backend: "postgres_rls_fts_trgm"`).
