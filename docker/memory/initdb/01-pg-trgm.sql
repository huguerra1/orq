-- Executado pelo entrypoint oficial do Postgres só na primeira inicialização do volume.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
