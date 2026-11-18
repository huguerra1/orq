-- DDL versionado da memória (Fase 7, incremento 7F): leitor global controlado para regime shared_global. Versão do esquema: 2.
--
-- Requer que a versão 1 (`schema.sql`) já exista no schema `orq_memory`.
-- Adiciona a role sem login `orq_memory_global_reader` (NOSUPERUSER, NOBYPASSRLS) para o braço de
-- controle experimental `shared_global` do experimento 7F: exige as mesmas quatro variáveis de
-- sessão (`app.catalog_id`, `app.current_project_id`, `app.current_family_id`, `app.current_role_id`),
-- `resolved_status = 'active'` e escopo de projeto/operacional global, sem restringir `area_scope`,
-- `target_families` ou `target_roles`.

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'orq_memory_global_reader') THEN
        CREATE ROLE orq_memory_global_reader NOLOGIN;
    END IF;
END
$roles$;

ALTER ROLE orq_memory_global_reader NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;

GRANT USAGE ON SCHEMA orq_memory TO orq_memory_global_reader;

SET LOCAL ROLE orq_memory_owner;
SET LOCAL search_path = orq_memory, public;

GRANT SELECT ON orq_memory.knowledge_catalogs, orq_memory.knowledge_sources,
    orq_memory.knowledge_units TO orq_memory_global_reader;

CREATE POLICY knowledge_catalogs_global_reader_scope ON orq_memory.knowledge_catalogs
    AS PERMISSIVE FOR SELECT TO orq_memory_global_reader
    USING (
        catalog_id = current_setting('app.catalog_id', true)
        AND coalesce(current_setting('app.current_project_id', true), '') <> ''
        AND coalesce(current_setting('app.current_family_id', true), '') <> ''
        AND coalesce(current_setting('app.current_role_id', true), '') <> ''
    );

CREATE POLICY knowledge_sources_global_reader_scope ON orq_memory.knowledge_sources
    AS PERMISSIVE FOR SELECT TO orq_memory_global_reader
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
    );

CREATE POLICY knowledge_units_global_reader_scope ON orq_memory.knowledge_units
    AS PERMISSIVE FOR SELECT TO orq_memory_global_reader
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
    );

RESET ROLE;
