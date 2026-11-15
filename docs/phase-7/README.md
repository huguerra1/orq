# Fase 7 — Hub de assinaturas pessoais, roteamento por cota e memória por família

Estado: aceita em 2026-11-14 ([ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md), seção "Decisões do dono"); **7A implementado**; **7B implementado** (revisado pelo Gemini 4 Argon High sem problemas; CI com Postgres 16 de serviço: 1355 aprovados e 3 ignorados, incluindo os 33 testes de banco/RLS). Próximo incremento: **7C**. Os incrementos 7C–7F permanecem pendentes.

## Problema

As Fases 1 a 6 definem os contratos da plataforma, o Vault Markdown local com busca lexical BM25, o servidor MCP somente leitura, o Planner, o Router e o runner sequencial com executor simulado (Fase 6 concluída em 2026-11-14, com `orq run simulate`). Contudo, a plataforma ainda não executa tarefas em agentes reais nem particiona o conhecimento entre diferentes famílias de agentes.

Conectar agentes reais por meio de APIs pagas por token geraria custo financeiro contínuo, enquanto usar LLMs locais limitaria a qualidade ou exigiria hardware dedicado. O autor dispõe de quatro **assinaturas pessoais** — **Kiro Power** (Kiro CLI), **ChatGPT Plus** (Codex CLI), **Claude Enterprise** (Claude Code) e **Gemini** (Gemini CLI/`agy`) — todas confirmadas para uso em projetos pessoais. Para utilizá-las com segurança e rigor experimental, o ORQ precisa resolver três problemas:

1. **Adaptadores de CLI sem extração de credenciais:** invocar exclusivamente as CLIs oficiais em modo não interativo sobre a interface `ExecutorAdapter` do 6D, cumprindo o checklist V01–V12, sem jamais extrair tokens de sessão nem automatizar interfaces web.
2. **Esgotamento de cota em tempo de execução:** assinaturas aplicam limites de uso por janela de tempo (exibidos na saída da CLI, como `"You've hit your usage limit ... try again at 10:05 PM"` no Codex CLI). O runner e o Router precisam reconhecer esse limite, bloquear temporariamente o destino até o reset, redirecionar a execução para outra família elegível e registrar o consumo de cota mantendo o custo monetário como `unavailable` (nunca zero).
3. **Contaminação e isolamento de memória entre famílias:** instruções operacionais, peculiaridades de ferramentas e aprendizados curados costumam ser específicos de uma família de agentes (modelo/assinatura e/ou papel), coexistindo com uma base comum do projeto. Entregar todo o repositório de conhecimento indistintamente a qualquer agente consome orçamento de contexto e pode induzir falhas por instruções conflitantes. É necessário isolar áreas por família e área comum em banco relacional (PostgreSQL com Row-Level Security), permitindo que o próprio agente consulte sua área via MCP e faça a escolha final das unidades, preservando o Vault Markdown com BM25 como modo offline.

## Objetivo

Especificar e, após a conclusão do 6E e o aceite da [ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md) pelo dono, implementar incrementalmente:

1. contratos de família de agentes (`AgentFamily`), áreas de memória (`common` e por família), audiência de conhecimento, estado de disponibilidade de cota e registro de consumo de cota;
2. armazenamento PostgreSQL configurável via Docker com isolamento estrito por **Row-Level Security (RLS)** e migração determinística a partir do Vault Markdown, mantendo o BM25 em Markdown como modo offline;
3. quatro adaptadores reais de executor em modo não interativo (**Claude Code**, **Codex CLI**, **Gemini CLI/`agy`** e **Kiro CLI**), verificados contra o checklist V01–V12;
4. detecção determinística de limite de uso na saída das CLIs, marcação de indisponibilidade até o reset e fallback automático para outra família elegível;
5. busca textual em português (`tsvector`/`tsquery` + `pg_trgm`) exposta por ferramenta MCP com escopo fixado na família da tentativa, escolha final das unidades pelo próprio agente e consolidação em `ContextManifest` com digests SHA-256;
6. protocolo e execução de um experimento reproduzível comparando memória compartilhada versus particionada por família e roteamento ciente de cota, visando a redação de um artigo científico.

## Escopo

### Entra

| Item | Conteúdo |
| --- | --- |
| Contratos da Fase 7 (`7A`) | `AgentFamily`, metadados de audiência (`common` + famílias/papéis), `SubscriptionProfile`, `QuotaStateRecord`, `QuotaConsumptionRecord` e extensões de eventos/motivos de preflight |
| Banco e segurança (`7B`) | PostgreSQL via Docker configurável, schema relacional de fontes/unidades/catálogo, políticas Row-Level Security (RLS) por família e papel, pipeline de sincronização do Vault Markdown e fallback para o modo offline `bm25-v1` |
| Adaptadores de assinatura (`7C`) | Implementações de `ExecutorAdapter` para Claude Code, Codex CLI, Gemini CLI (`agy`) e Kiro CLI em modo não interativo, mais evidências do checklist V01–V12 |
| Roteamento ciente de cota (`7C`/`7E`) | Detector de limite na saída da CLI, bloqueio do destino até `reset_at` (ou cooldown), fallback para outra família elegível, registro de consumo de cota e manutenção de custo monetário `unavailable` (`amount: null`) |
| Busca e contexto (`7D`) | Full-text search do Postgres em português combinado com `pg_trgm`, seleção final pelo agente e geração de `KnowledgeSelectionRecord` e `ContextManifest` com digests canônicos |
| MCP por família (`7E`) | Extensão do servidor `orq-knowledge-mcp` da Fase 3 com ferramentas somente leitura escopadas pela família da tentativa ativa e integração completa com o runner do 6E |
| Experimento científico (`7F`) | Execução experimental controlada (memória compartilhada × particionada; roteamento fixo × ciente de cota), coleta de artefatos/journals e consolidação de métricas para artigo |

### Fica fora

- **APIs pagas por token:** nenhuma integração com endpoints tarifados por uso (OpenAI API, Anthropic API, Google Vertex/Gemini API paga ou AWS Bedrock tarifado por chamada).
- **LLMs locais:** nenhuma dependência de Ollama, llama.cpp, vLLM ou modelos locais de inferência/reranking.
- **Extração de tokens ou automação web:** proibição absoluta de ler tokens OAuth/cookies internos das CLIs ou automatizar navegadores sobre interfaces web de chat.
- **Embeddings e busca vetorial nesta fase:** geração de embeddings exige um modelo (local ou via API); fica documentada apenas como evolução futura sobre a estrutura do Postgres.
- **Jev / TypeSafe AI no fluxo principal:** continua como adaptador opcional fora do caminho crítico.
- **Execução concorrente do DAG (`max_concurrency > 1`):** permanece na Fase 11; o runner continua sequencial.
- **Promoção automática para o catálogo:** saídas de agentes não entram na memória das famílias sem validação e registro explícito de revisão.

## Dependências das Fases 1 a 6

| Fase | O que a Fase 7 usa | Situação |
| --- | --- | --- |
| 1 | Contratos de perfis (`AgentRoleProfile`, `ModelProfile`, `ExecutorProfile`), `RoutingDecision`, `ContextManifest`, `ExecutionPolicy`, `UsageRecord` e invariantes de custo desconhecido | Aceitos no baseline v0.1 |
| 2 | Parser Markdown seguro, chunking por headings, `canonical_digest` (RFC 8785/JCS + SHA-256), `bm25-v1` (mantido como modo offline) e materializador de `ContextManifest` | Implementado e testado |
| 3 | Servidor `orq-knowledge-mcp` (`mcp==2.2.0`, transporte `stdio`, somente leitura) | Implementado; será estendido com tools escopadas por família em `7E` |
| 4 | `FixedTemplatePlanner`, `PlanningRecord` e validação central de `WorkflowSpec` | Implementado e testado |
| 5 | `route_and_finalize`, baselines `fixed` e `rules`, `RoutingTargetCatalog` e checks de preflight | Implementado; receberá filtro/check de disponibilidade de cota |
| 6 | Journal JCS encadeado (`6B`), projeções e máquina de estados (`6C`), protocolo `ExecutorAdapter`, checklist V01–V12 e `DeterministicEvaluator` (`6D`), e runner sequencial ponta a ponta (`6E`) | `6A`–`6E` implementados (Fase 6 concluída em 2026-11-14) |

## Incrementos de implementação (após 6E e aceite)

| Incremento | Entrega | Gate |
| --- | --- | --- |
| **7A** | Contratos e schemas JSON da Fase 7: `AgentFamily`, áreas (`common` e por família), audiência de conhecimento, `SubscriptionProfile`, `QuotaStateRecord` e `QuotaConsumptionRecord` | Meta-schema Draft 2020-12, validadores semânticos, fixtures válidas/inválidas e digests JCS fixados em testes |
| **7B** | PostgreSQL configurável via Docker, DDL com Row-Level Security (RLS) por família/papel, script de migração do Vault Markdown e preservação do modo offline em Markdown (`bm25-v1`) | Testes automatizados comprovando isolamento RLS entre famílias, paridade de digests na migração do Vault e funcionamento integral do modo offline quando o banco não está ativo |
| **7C** | Adaptadores das quatro CLIs (Claude Code, Codex CLI, Gemini CLI/`agy`, Kiro CLI) sobre `ExecutorAdapter`, parser de detecção de cota e checklist V01–V12 | Evidências V01–V12 registradas por versão fixada; testes unitários com fixtures reais de saída das CLIs (incluindo limite de uso do Codex) e smoke tests não interativos com `stdin` fechado |
| **7D** | Busca full-text em português (`tsvector` + `pg_trgm`) sob RLS, protocolo de escolha final das unidades pelo agente e geração de `KnowledgeSelectionRecord` + `ContextManifest` com digests | Testes de relevância lexical/trigramas em português, validação de orçamento sem truncamento silencioso e reprodutibilidade byte a byte do `ContextManifest` e do bundle JCS |
| **7E** | Extensão do servidor MCP com escopo de família fixado pelo host e integração completa com o runner sequencial (detecção de cota → bloqueio até reset → fallback de família → novo `ContextManifest`) | Cenários de aceitação `Q01`–`Q27` executados como testes automatizados; fallback entre famílias provado ponta a ponta com custo monetário `unavailable` |
| **7F** | Execução do desenho experimental (memória compartilhada × particionada por família; roteamento fixo × ciente de cota), scripts de agregação determinística e pacote de reprodutibilidade para o artigo | Cenários `Q28`–`Q30` verificados, protocolo experimental executado sem intervenção manual, journals/manifestos íntegros e tabelas de resultados consolidadas |

Cada incremento exige testes automatizados verdes, atualização do [HANDOFF](../HANDOFF.md) e do [ROADMAP](../../ROADMAP.md) e revisão antes do próximo passo, conforme [AGENTS.md](../../AGENTS.md).

## Notas de implementação 7A

- Cinco schemas fechados: `AgentFamily`, `AudienceMetadata`, `SubscriptionProfile`, `QuotaStateRecord` e `QuotaConsumptionRecord`. Família e papel são campos independentes; audiência restrita nos dois eixos significa interseção. Família/audiência usam o digest do contêiner; os três registros têm `record_hash` JCS sem o próprio hash.
- Extensões opcionais preservam o baseline: `audience` na fonte/unidade; família/assinatura/pool no destino; `subscription_id` em uso/reserva; `reason` na decisão/preflight. Mantidos os nomes executáveis **`cost_lines`** e **`code`**. Com assinatura, custo exige `unavailable`, valor/moeda nulos e cobertura indisponível; a moeda conhecida das fixtures antigas continua aceita sem assinatura.
- `quota_exhausted_recorded`/`quota_reset_observed` usam agregado `run`; `quota_consumption_recorded`, `attempt`. Payloads são os registros novos. `quota_limit_reached` exige erro retryable de provedor, término confirmado e `quota_state` ligado ao mesmo run/tentativa. Motivos de roteamento conservam `{code, subject, evidence_refs}`; fallback usa `effective_selection.rule: quota_fallback`, e rejeição total, `reason.code: all_eligible_families_quota_exhausted`.
- `unknown` permanece disponibilidade inicial/inconclusiva, sem fabricar evento. Bloqueios exigem tentativa de origem e excerto de até 512 caracteres. `cli_reported` exige `reset_at = blocked_until > observed_at`; `cooldown_policy`, reset nulo e 60 minutos, salvo `default_cooldown_ms` positivo explícito. Overrides do perfil/run são lacrados no registro; validação não insere defaults.
- Reativação limpa os horários de bloqueio, usa `clock_expired` e registra `previous_status`. Após cooldown, exige `probe_evidence_refs` não vazio (ArtifactRefs). Verificar a sondagem e o prazo contra o evento anterior permanece no 7E.
- `subscription_rules` opcional registra as seis categorias/ordens aceitas e revisão por família diferente. O Router atual não interpreta essa política; mapeamento de tarefas e execução ficam no 7E. Perfis restringem CLI/família e rejeitam configuração de credenciais/web com `subscription_policy_violation`; evidências V01–V12 ficam no 7C.
- `validate_instance` confere hashes, datas e invariantes temporais, inclusive payloads. `validate_quota_consumption_record(record, usage_record=...)` resolve digest e run/tentativa/assinatura sem I/O; sem o uso, não afirma resolver a referência. Sem banco, adaptador, busca, MCP ou dependência nova.

Verificação: **378 testes aprovados, nenhum ignorado**, em `tests/test_phase7_contracts.py`, `tests/test_phase6_schemas.py` e `tests/test_schemas.py`; 17 fixtures válidas com `PINNED_DIGESTS` e dez inválidas, cada uma reparável por uma única correção. Suíte integral verificada pelo orquestrador: 1280 aprovados e 4 ignorados. Revisão independente (Gemini 4 Argon Medium) sem problemas.

| Cenários | Cobertura executada no 7A |
| --- | --- |
| Q02 | Rejeição de configuração de credenciais/web e CLI/família incompatível; sem despacho real |
| Q07 | Forma e vínculos de erro/bloqueio, reset em UTC e término confirmado; sem detector de CLI |
| Q08, Q12 | Decisões serializadas de fallback/esgotamento total e invariantes locais; sem roteamento/admissão executáveis por cota |
| Q09, Q10 | Contratos de reset/cooldown, default/override e exigência de evidência de sondagem; sem relógio/runtime de recuperação |
| Q13 | Consumo, desconhecidos, referência ao uso e custo indisponível; agregação de RunSummary permanece no 7E |
| Q14 | Rejeição de `"0"`/`"0.00"` no uso e na reserva de assinatura, com `invalid_subscription_cost_zero` |

Audiência tem testes estruturais dos dois eixos, listas vazias/duplicadas e incorporação no catálogo. Isso não executa Q15/Q16 (RLS), nem os demais cenários Q de runtime, busca, MCP ou experimento. Próximo passo: revisar 7A e iniciar **7B**, conforme seu gate de isolamento/paridade/offline.

## Notas de implementação 7B

- **Pacote `orq_contracts.memory_store`:** `config` (backend e escopo), `ddl` + `schema.sql` (DDL versionado), `sync` (plano puro) e `postgres` (único módulo que importa o driver). Importar `orq_contracts` ou `memory_store` não carrega o psycopg.
- **Configuração:** `ORQ_MEMORY_DSN` liga o backend `postgres`; sem DSN vale `offline_markdown_bm25`. `ORQ_KNOWLEDGE_BACKEND=offline_markdown_bm25` força o offline mesmo com DSN; `=postgres` sem DSN falha com `memory_dsn_missing`. O DSN fica fora do `repr` e das mensagens de erro. As variáveis `ORQ_PG_*` do rascunho em [family-memory.md](family-memory.md) foram trocadas por um DSN único.
- **DDL (versão 1):** schema `orq_memory` com `knowledge_catalogs`, `knowledge_sources` e `knowledge_units` (audiência do 7A desnormalizada em cada linha: `area_scope`, `target_families`, `target_roles`, mais escopo de projeto e status). `ENABLE` e `FORCE ROW LEVEL SECURITY` nas três. Roles sem login, sem `SUPERUSER` e sem `BYPASSRLS`: `orq_memory_owner` (dono, sem política, portanto não lê nada), `orq_memory_writer` (só `SELECT`/`INSERT`, usado pela sincronização) e `orq_memory_reader` (só `SELECT`, política por escopo). A versão e o SHA-256 do arquivo ficam em `schema_migrations`; outro digest para a mesma versão é recusado (`memory_schema_version_conflict`). Índices GIN `tsvector` (português) e `pg_trgm` já existem, sem API de busca (7D). Um `CHECK` recalcula no banco o SHA-256 dos bytes UTF-8 de cada unidade.
- **Política de leitura:** exige as quatro variáveis (`app.catalog_id`, `app.current_project_id`, `app.current_family_id`, `app.current_role_id`); escopo ausente ou incompleto não lê nada. Só status ativo; projeto exato ou operacional global; área comum ou família compatível nos dois eixos (eixo vazio não restringe; dois eixos exigem interseção). Fontes com escopo de coleção ficam gravadas, mas invisíveis no 7B, porque não há variável de coleções.
- **Audiência:** a da unidade prevalece sobre a da fonte; sem nenhuma, a unidade entra na área comum (mesma visibilidade das Fases 2–6). Toda audiência passa pelo schema do 7A.
- **Sincronização:** `build_sync_plan(vault_root, snapshot)` confere o `catalog_hash`, reingere cada fonte com `ingest_markdown_source`, exige unidades idênticas às do snapshot, lê o conteúdo com `materialize_unit` e grava `canonical_digest` da metadata e de cada unidade. `sync_catalog` insere tudo numa transação como `orq_memory_writer`. O snapshot é imutável por `catalog_id`: repetir devolve `unchanged` sem duplicar; conteúdo diferente aborta com `catalog_sync_integrity_error`. CLI: `orq-contracts vault sync-db --root <vault> --catalog <snapshot.json>` (aplica o DDL e sincroniza).
- **Acesso com escopo:** `MemoryScope` (validado, imutável) é montado pelo host; `scoped_transaction`/`ScopedMemoryReader` abrem `READ ONLY`, fazem `SET LOCAL ROLE orq_memory_reader` e fixam o escopo com `set_config(..., true)` (o `SET LOCAL` parametrizado), recusando a transação se a role efetiva puder ignorar RLS. Os métodos de leitura não recebem família, papel nem projeto.
- **Dependência nova:** `psycopg[binary]==3.3.9` no `pyproject.toml`; `psycopg`, `psycopg-binary` e `tzdata` (dependência do psycopg no Windows) fixados no `requirements.lock`.
- **Docker e CI:** `docker-compose.memory.yml` opcional (`postgres:16-alpine`, só em `127.0.0.1`, `pg_trgm` no init, senha de teste `orq_test`). O job `testes` do CI ganhou um service container `postgres:16` com healthcheck e exporta `ORQ_MEMORY_DSN`; nome do job e matriz 3.12/3.13 mantidos. A migração exige login superusuário (cria roles do cluster).

Verificação: `tests/test_memory_store.py` (41 testes, sem banco: configuração, escopo, DDL, plano, paridade de digests do plano, rejeições e modo offline) e `tests/test_memory_store_postgres.py` (33 testes contra Postgres: isolamento entre famílias, área comum, eixo de papel, escopo ausente/incompleto, sem bypass, `FORCE` para o dono, somente leitura, paridade de digests no banco e idempotência). Os testes de banco rodam no CI quando não há `ORQ_MEMORY_DSN`. Suíte local: 1321 aprovados e 37 ignorados (os 4 anteriores e os 33 de banco).

| Cenários | Cobertura executada no 7B |
| --- | --- |
| Q15, Q16 | RLS entre famílias e interseção família × papel, com testes negativos |
| Q17 | Escopo validado e valores sempre como parâmetros; a busca em si fica no 7D |
| Q18 | Mesmos digests/ordinais da Fase 2 no banco e segunda sincronização sem duplicar |
| Q22 | Parcial: cadeia offline intacta sem DSN; o filtro de audiência em memória e o `backend` no `KnowledgeSelectionRecord` ficam para o 7D |

## Documentos da Fase 7

- [ADR 0008 — Hub de assinaturas pessoais, roteamento por cota e memória por família em Postgres](../adr/0008-phase-7-subscription-hub-and-family-memory.md)
- [Adaptadores de assinatura por CLI e checklist V01–V12](subscription-adapters.md)
- [Roteamento ciente de cota e fallback entre famílias](quota-aware-routing.md)
- [Memória por família em Postgres, RLS e entrega via MCP](family-memory.md)
- [Cenários de aceitação da Fase 7 (Q01–Q30)](acceptance-scenarios.md)
- [Desenho experimental 7F: memória particionada e roteamento por cota](experiment.md)

## Critério de saída da fase

A Fase 7 estará concluída quando:

1. os quatro adaptadores de CLI (Claude Code, Codex CLI, Gemini CLI/`agy` e Kiro CLI) tiverem seus controles verificados pelo checklist V01–V12 em versões fixadas, operando exclusivamente em modo não interativo sem extração de tokens ou automação web;
2. um workflow executado pelo runner detectar o esgotamento de cota de uma assinatura, marcar o destino indisponível até o horário de reset, fazer fallback para outra família elegível, re-materializar o `ContextManifest` sob o escopo da nova família e concluir o run registrando o consumo de cota com custo monetário `unavailable` (`amount: null`);
3. os testes do PostgreSQL comprovarem que o Row-Level Security impede qualquer leitura de unidades exclusivas de outra família, enquanto o modo offline em Markdown (`bm25-v1`) continua operacional sem Docker;
4. os cenários `Q01`–`Q30` da [matriz de aceitação](acceptance-scenarios.md) passarem como testes automatizados e o pacote experimental do incremento `7F` estiver completo e auditável por digests para submissão do artigo.
