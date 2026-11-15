-- DDL versionado da memória por família (Fase 7, incremento 7B). Versão do esquema: 1.
--
-- Aplicado inteiro numa única transação por `orq_contracts.memory_store.postgres.apply_schema`,
-- que registra a versão e o digest SHA-256 deste arquivo (LF) em `orq_memory.schema_migrations`.
-- Mudar este arquivo exige nova versão: um banco com outro digest para a versão 1 é recusado.
--
-- A conexão administrativa precisa ser superusuário (ou equivalente com CREATEROLE e permissão
-- para CREATE EXTENSION), como no container local e no service container do CI.
--
-- Roles (sem login; nenhuma tem SUPERUSER nem BYPASSRLS):
--   orq_memory_owner  dono do schema e das tabelas. Não tem política: com FORCE ROW LEVEL SECURITY
--                     o próprio dono não lê nenhuma linha.
--   orq_memory_writer usado só pela sincronização Vault -> Postgres (SELECT e INSERT; sem UPDATE/DELETE).
--   orq_memory_reader leitura com escopo. Só enxerga linhas permitidas pelas variáveis de transação
--                     app.catalog_id, app.current_project_id, app.current_family_id e app.current_role_id.
--
-- Um login de aplicação recebe acesso com `GRANT orq_memory_reader TO <login>`; o host sempre abre
-- a transação com `SET LOCAL ROLE orq_memory_reader` e fixa as quatro variáveis com
-- `set_config(..., true)` (equivalente a SET LOCAL), nunca com valores vindos do modelo.

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'orq_memory_owner') THEN
        CREATE ROLE orq_memory_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'orq_memory_writer') THEN
        CREATE ROLE orq_memory_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'orq_memory_reader') THEN
        CREATE ROLE orq_memory_reader NOLOGIN;
    END IF;
END
$roles$;

-- Reafirma os atributos mesmo quando as roles já existiam no cluster.
ALTER ROLE orq_memory_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE orq_memory_writer NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
ALTER ROLE orq_memory_reader NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;

-- Trigramas para a busca do 7D (o índice já fica pronto; a API de busca não existe no 7B).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS orq_memory AUTHORIZATION orq_memory_owner;
ALTER SCHEMA orq_memory OWNER TO orq_memory_owner;
REVOKE ALL ON SCHEMA orq_memory FROM PUBLIC;
GRANT USAGE ON SCHEMA orq_memory TO orq_memory_writer, orq_memory_reader;

-- Daqui em diante os objetos são criados pelo dono sem privilégio de bypass.
SET LOCAL ROLE orq_memory_owner;
SET LOCAL search_path = orq_memory, public;

CREATE TABLE orq_memory.schema_migrations (
    version    INTEGER PRIMARY KEY CHECK (version > 0),
    ddl_digest TEXT NOT NULL CHECK (ddl_digest ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.now()
);

-- Um snapshot de catálogo por catalog_id, imutável depois de sincronizado.
CREATE TABLE orq_memory.knowledge_catalogs (
    catalog_id      TEXT PRIMARY KEY CHECK (catalog_id ~ '^[A-Za-z][A-Za-z0-9._:-]*$'),
    catalog_version TEXT NOT NULL CHECK (catalog_version <> ''),
    catalog_hash    TEXT NOT NULL CHECK (catalog_hash ~ '^[0-9a-f]{64}$'),
    created_at      TEXT NOT NULL,
    status_cutoff   TEXT NOT NULL,
    source_count    INTEGER NOT NULL CHECK (source_count >= 0),
    unit_count      INTEGER NOT NULL CHECK (unit_count >= 0),
    sync_digest     TEXT NOT NULL CHECK (sync_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE orq_memory.knowledge_sources (
    catalog_id         TEXT NOT NULL REFERENCES orq_memory.knowledge_catalogs (catalog_id),
    source_id          TEXT NOT NULL CHECK (source_id <> ''),
    revision           TEXT NOT NULL CHECK (revision <> ''),
    knowledge_kind     TEXT NOT NULL CHECK (knowledge_kind IN ('operational', 'project')),
    project_scope_kind TEXT NOT NULL CHECK (project_scope_kind IN ('project', 'collection', 'global_operational')),
    project_id         TEXT,
    collection_id      TEXT,
    resolved_status    TEXT NOT NULL CHECK (resolved_status IN ('active', 'deprecated', 'revoked')),
    area_scope         TEXT NOT NULL CHECK (area_scope IN ('common', 'family')),
    target_families    TEXT[] NOT NULL,
    target_roles       TEXT[] NOT NULL,
    title              TEXT NOT NULL,
    source_digest      TEXT NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    metadata_digest    TEXT NOT NULL CHECK (metadata_digest ~ '^[0-9a-f]{64}$'),
    source_metadata    JSONB NOT NULL,
    PRIMARY KEY (catalog_id, source_id, revision),
    CONSTRAINT knowledge_sources_project_scope CHECK (
        (project_scope_kind = 'project' AND project_id IS NOT NULL AND project_id <> '' AND collection_id IS NULL)
        OR (project_scope_kind = 'collection' AND collection_id IS NOT NULL AND collection_id <> '' AND project_id IS NULL)
        OR (project_scope_kind = 'global_operational' AND knowledge_kind = 'operational'
            AND project_id IS NULL AND collection_id IS NULL)
    ),
    CONSTRAINT knowledge_sources_audience CHECK (
        target_families <@ ARRAY['family_claude', 'family_codex', 'family_gemini', 'family_kiro', 'family_simulated']::TEXT[]
        AND pg_catalog.array_position(target_roles, NULL::TEXT) IS NULL
        AND (
            (area_scope = 'common' AND cardinality(target_families) = 0 AND cardinality(target_roles) = 0)
            OR (area_scope = 'family' AND (cardinality(target_families) > 0 OR cardinality(target_roles) > 0))
        )
    )
);

-- Unidades desnormalizadas: cada linha carrega a audiência efetiva e o escopo de projeto,
-- para que a política RLS decida sem consultar outras tabelas.
CREATE TABLE orq_memory.knowledge_units (
    catalog_id         TEXT NOT NULL,
    source_id          TEXT NOT NULL,
    revision           TEXT NOT NULL,
    unit_id            TEXT NOT NULL CHECK (unit_id <> ''),
    ordinal            INTEGER NOT NULL CHECK (ordinal >= 0),
    unit_kind          TEXT NOT NULL CHECK (unit_kind IN ('prose', 'code', 'table', 'list', 'frontmatter', 'mixed')),
    parent_unit_id     TEXT,
    knowledge_kind     TEXT NOT NULL CHECK (knowledge_kind IN ('operational', 'project')),
    project_scope_kind TEXT NOT NULL CHECK (project_scope_kind IN ('project', 'collection', 'global_operational')),
    project_id         TEXT,
    collection_id      TEXT,
    resolved_status    TEXT NOT NULL CHECK (resolved_status IN ('active', 'deprecated', 'revoked')),
    area_scope         TEXT NOT NULL CHECK (area_scope IN ('common', 'family')),
    target_families    TEXT[] NOT NULL,
    target_roles       TEXT[] NOT NULL,
    title              TEXT NOT NULL,
    heading_path       TEXT[] NOT NULL,
    heading_text       TEXT NOT NULL,
    content            TEXT NOT NULL,
    content_digest     TEXT NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    source_digest      TEXT NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    size_bytes         INTEGER NOT NULL CHECK (size_bytes >= 0),
    estimated_tokens   INTEGER NOT NULL CHECK (estimated_tokens >= 0),
    unit_record        JSONB NOT NULL,
    unit_record_digest TEXT NOT NULL CHECK (unit_record_digest ~ '^[0-9a-f]{64}$'),
    search_tsv         TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('portuguese'::regconfig, title), 'A')
        || setweight(to_tsvector('portuguese'::regconfig, heading_text), 'B')
        || setweight(to_tsvector('portuguese'::regconfig, content), 'C')
    ) STORED,
    PRIMARY KEY (catalog_id, source_id, revision, unit_id),
    UNIQUE (catalog_id, source_id, revision, ordinal),
    FOREIGN KEY (catalog_id, source_id, revision)
        REFERENCES orq_memory.knowledge_sources (catalog_id, source_id, revision),
    -- O banco recalcula o SHA-256 dos bytes UTF-8: o conteúdo gravado é exatamente o do Vault.
    CONSTRAINT knowledge_units_content_integrity CHECK (
        content_digest = encode(sha256(convert_to(content, 'UTF8')), 'hex')
        AND size_bytes = octet_length(convert_to(content, 'UTF8'))
    ),
    CONSTRAINT knowledge_units_project_scope CHECK (
        (project_scope_kind = 'project' AND project_id IS NOT NULL AND project_id <> '' AND collection_id IS NULL)
        OR (project_scope_kind = 'collection' AND collection_id IS NOT NULL AND collection_id <> '' AND project_id IS NULL)
        OR (project_scope_kind = 'global_operational' AND knowledge_kind = 'operational'
            AND project_id IS NULL AND collection_id IS NULL)
    ),
    CONSTRAINT knowledge_units_audience CHECK (
        target_families <@ ARRAY['family_claude', 'family_codex', 'family_gemini', 'family_kiro', 'family_simulated']::TEXT[]
        AND pg_catalog.array_position(target_roles, NULL::TEXT) IS NULL
        AND (
            (area_scope = 'common' AND cardinality(target_families) = 0 AND cardinality(target_roles) = 0)
            OR (area_scope = 'family' AND (cardinality(target_families) > 0 OR cardinality(target_roles) > 0))
        )
    )
);

CREATE INDEX knowledge_units_search_tsv_idx ON orq_memory.knowledge_units USING GIN (search_tsv);
CREATE INDEX knowledge_units_content_trgm_idx ON orq_memory.knowledge_units USING GIN (content gin_trgm_ops);
CREATE INDEX knowledge_units_target_families_idx ON orq_memory.knowledge_units USING GIN (target_families);
CREATE INDEX knowledge_units_target_roles_idx ON orq_memory.knowledge_units USING GIN (target_roles);

-- Row-Level Security habilitada e forçada também para o dono das tabelas.
ALTER TABLE orq_memory.knowledge_catalogs ENABLE ROW LEVEL SECURITY;
ALTER TABLE orq_memory.knowledge_catalogs FORCE ROW LEVEL SECURITY;
ALTER TABLE orq_memory.knowledge_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE orq_memory.knowledge_sources FORCE ROW LEVEL SECURITY;
ALTER TABLE orq_memory.knowledge_units ENABLE ROW LEVEL SECURITY;
ALTER TABLE orq_memory.knowledge_units FORCE ROW LEVEL SECURITY;

REVOKE ALL ON orq_memory.schema_migrations, orq_memory.knowledge_catalogs,
    orq_memory.knowledge_sources, orq_memory.knowledge_units FROM PUBLIC;
GRANT SELECT ON orq_memory.knowledge_catalogs, orq_memory.knowledge_sources,
    orq_memory.knowledge_units TO orq_memory_reader;
GRANT SELECT, INSERT ON orq_memory.knowledge_catalogs, orq_memory.knowledge_sources,
    orq_memory.knowledge_units TO orq_memory_writer;

-- Sincronização: o writer vê e insere todas as linhas (é a única role com essa política).
CREATE POLICY knowledge_catalogs_writer ON orq_memory.knowledge_catalogs
    AS PERMISSIVE FOR ALL TO orq_memory_writer USING (true) WITH CHECK (true);
CREATE POLICY knowledge_sources_writer ON orq_memory.knowledge_sources
    AS PERMISSIVE FOR ALL TO orq_memory_writer USING (true) WITH CHECK (true);
CREATE POLICY knowledge_units_writer ON orq_memory.knowledge_units
    AS PERMISSIVE FOR ALL TO orq_memory_writer USING (true) WITH CHECK (true);

-- Leitura com escopo. Regras comuns às três políticas:
--   * current_setting(..., true) devolve NULL (ou '' após uso anterior na sessão) quando a variável
--     não foi fixada; escopo incompleto não lê nada (fail-closed);
--   * só o snapshot fixado em app.catalog_id.
CREATE POLICY knowledge_catalogs_reader_scope ON orq_memory.knowledge_catalogs
    AS PERMISSIVE FOR SELECT TO orq_memory_reader
    USING (
        catalog_id = current_setting('app.catalog_id', true)
        AND coalesce(current_setting('app.current_project_id', true), '') <> ''
        AND coalesce(current_setting('app.current_family_id', true), '') <> ''
        AND coalesce(current_setting('app.current_role_id', true), '') <> ''
    );

-- Fontes e unidades, além do escopo completo e do catálogo:
--   * só status ativo;
--   * projeto: operacional global, ou projeto exato do run (coleções não são visíveis no 7B);
--   * audiência: área comum, ou área de família compatível nos dois eixos independentes
--     (lista vazia num eixo não restringe esse eixo; listas nos dois eixos exigem interseção).
CREATE POLICY knowledge_sources_reader_scope ON orq_memory.knowledge_sources
    AS PERMISSIVE FOR SELECT TO orq_memory_reader
    USING (
        catalog_id = current_setting('app.catalog_id', true)
        AND coalesce(current_setting('app.current_project_id', true), '') <> ''
        AND coalesce(current_setting('app.current_family_id', true), '') <> ''
        AND coalesce(current_setting('app.current_role_id', true), '') <> ''
        AND resolved_status = 'active'
        AND (
            (project_scope_kind = 'global_operational' AND knowledge_kind = 'operational')
            OR (project_scope_kind = 'project' AND project_id = current_setting('app.current_project_id', true))
        )
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

CREATE POLICY knowledge_units_reader_scope ON orq_memory.knowledge_units
    AS PERMISSIVE FOR SELECT TO orq_memory_reader
    USING (
        catalog_id = current_setting('app.catalog_id', true)
        AND coalesce(current_setting('app.current_project_id', true), '') <> ''
        AND coalesce(current_setting('app.current_family_id', true), '') <> ''
        AND coalesce(current_setting('app.current_role_id', true), '') <> ''
        AND resolved_status = 'active'
        AND (
            (project_scope_kind = 'global_operational' AND knowledge_kind = 'operational')
            OR (project_scope_kind = 'project' AND project_id = current_setting('app.current_project_id', true))
        )
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

RESET ROLE;
