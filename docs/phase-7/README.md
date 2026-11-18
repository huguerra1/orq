# Fase 7 — Hub de assinaturas pessoais, roteamento por cota e memória por família

Estado: aceita em 2026-11-14 ([ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md), seção "Decisões do dono"); **7A–7D na dev**; **7E implementado e revisado**, com MCP escopado, auditoria por tentativa e integração de cota/contexto no runner ([Notas 7E](#notas-de-implementação-7e)). A [cobertura Q01–Q27](acceptance-scenarios.md#cobertura-automatizada-do-7e) distingue testes locais, Postgres e a ressalva temporal de Q25. Próximo incremento: **7F**, após CI e merge do `7E`. O smoke V01–V12 das CLIs reais continua pendente; seus controles permanecem unknown.

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

## Notas de implementação 7C

- Quatro adaptadores em `src/orq_contracts/adapters/`, sem alterar o protocolo 6D,
  banco, memória ou CI. Supervisor stdlib com argv explícito, stdin fechado,
  timeout autônomo, cancelamento da árvore e estado/recibo duráveis antes do spawn.
  Kiro usa WSL somente no Windows, com workspace `C:\dev` → `/mnt/c/dev` e
  supervisor Python dentro do Linux. Reconciliação incerta nunca redespacha.
- Detector por CLI, com versão no perfil 7A, usa somente diagnósticos em invocações
  falhas; fixture real do Codex resolve 22:05 no fuso local e rollover de dia.
  Login real do Kiro é indisponibilidade de autenticação, sem bloquear cota.
  Sem horário válido: reset null, cooldown de 60 minutos e sondagem exigida no 7E.
- Coleta valida contratos locais e paths antes de publicar artefatos; custo sempre
  unavailable/null; tokens só quando reportados. `quota_consumption(intent)` expõe
  o registro 7A sem estender `ExecutorResult`. `permission_or_empty_output` permanece
  motivo de `execution_failed`, pois não integra o enum de códigos do 7A.
- Verificação: **90 aprovados, um ignorado** nos dois arquivos novos (symlink no
  Windows); subprocessos Python falsos, sem CLI real/rede. Todos os controles
  permanecem unknown e a cobertura de contexto também. [Tabela V01–V12 e decisões](subscription-adapters.md#cumprimento-do-checklist-v01v12-por-adaptador)
  distinguem testes do host das evidências ainda necessárias por versão real.
  Próximo: validar a revisão do 7C na suíte integral e executar smoke real V01–V12.
  Fallback, reativação e sondagem permanecem no 7E; nenhum cenário Q ponta a ponta
  foi declarado concluído por estes testes.

### Revisão independente do 7C — 2026-11-14

Os oito achados foram corrigidos sem alterar interfaces públicas, schemas,
dependências, memória, banco ou CI. Mantida a substituição atômica dos registros,
com temporário exclusivo no mesmo diretório, flush/fsync antes de `os.replace` e
sincronização do diretório. Criação, substituição, limpeza e leitura repetem apenas
`PermissionError` no Windows, até 100 tentativas com intervalo de 10 ms; falha
persistente continua explícita. Preferiu-se esse mecanismo a um lock compartilhado
entre host e supervisor: evita coordenação adicional e mantém leitores vendo um
registro completo. Temporários são removidos em `finally`.

| Achado | Correção e regressão |
| --- | --- |
| 1 — corrida nos arquivos e observação | Leitura tolerante e escrita com temporários únicos; `launched` com supervisor vivo conta como running. Após sua saída, `observe` espera e relê o recibo. Testes injetam acessos negados transitórios/persistentes, verificam fsync/limpeza, exercitam dois escritores concorrentes e repetem 25 despachos Kiro `noise_only` com leitor concorrente. |
| 2 — origem dos eventos e resposta final | AGY/Kiro tratam stdout como texto; Codex não promove mensagem intermediária a final válida nem recupera `-o` após erro estruturado. JSON com sufixo na mesma linha permanece texto inteiro. Regressões cobrem mensagem intermediária, final seguida de erro, sucesso após recuperação de erro, JSON de projeto e literais com sufixo de cota. |
| 3 — diagnóstico de cota | Diagnósticos multilinha são separados; login/rede aceitam prefixos com fronteiras de palavra; cota fatal tem precedência sobre avisos anteriores. Testes incluem ENOTFOUND com contexto, login Kiro prefixado e cota após rede/autenticação. |
| 4 — limpeza no Windows | Fechar o Job não dispensa matar/recolher o filho direto que ficou suspenso fora dele. `TimeoutExpired` produz recibo unknown, sem espera ilimitada. Testes simulam falha de associação e timeout de término. |
| 5 — árvore POSIX/WSL | Após SIGKILL e espera do filho, sonda o grupo até desaparecer; prazo esgotado preserva unknown. Testes simulam netos ainda presentes, desaparecimento posterior e expiração, inclusive quando o pai já saiu. |
| 6 — preparação do despacho | Bundle UTF-8, caminhos WSL, argv e prazo são preparados antes de criar diretórios. `state.json` é escrito antes do workspace; falha inicial sem estado publicado remove o diretório vazio. Regressões provam ausência de resíduos, correção da entrada e novo despacho válido. |
| 7 — evidência da reconciliação | Status, sessão e ArtifactRef usam o mesmo snapshot de recibo. Teste alterna recibos running/terminal e exige uma única leitura. |
| 8 — fidelidade dos testes | Montagem WSL simulada independe do basetemp; CLI falsa registra apenas nomes das variáveis recebidas, comprovando nas quatro famílias a exclusão das seis variáveis sensíveis injetadas juntas. Casos de rede/login incluem prefixos realistas. O teste de árvore dá margem para iniciar o neto e aguarda o supervisor com prazo limitado, sem `observe`, em vez de depender de um sleep fixo. |

Os testes POSIX/WSL com processos simulados comprovam a lógica do supervisor;
enforcement com os binários reais continua pendente no gate V01–V12. Custo e
consumo remoto desconhecidos continuam indisponíveis/unknown.

Verificação após a revisão: **118 aprovados, um ignorado** (symlink sem privilégio
no Windows), em `tests/test_cli_adapters.py` e `tests/test_quota_detector.py`, com
`PYTHONPATH=src`, `-q -p no:cacheprovider` e basetemp dedicado. `git diff --check`
sem erros. Suíte integral verificada pelo orquestrador: 1439 aprovados e 38 ignorados; testes dos adaptadores repetidos 3 vezes sem intermitência. Nenhum
commit ou push nesta correção.

## Notas de implementação 7D

- **Módulos:** `knowledge_backend` (interface `KnowledgeBackend`, `KnowledgeCandidate`, `EligibilityPolicy`, `OfflineMarkdownBackend`, `create_knowledge_backend`), `knowledge_choice` (`KnowledgeChoiceSession`, `materialize_chosen_context`) e, em `memory_store`, `search` (SQL, pesagem e normalização da query, sem driver), `knowledge` (`PostgresKnowledgeBackend`) e `ScopedMemoryReader.search_units`/`get_unit_contents`. Nenhuma dependência nova, DDL do 7B intocado (versão 1) e nada em `adapters/`. As Fases 2–6 não mudam: `select_knowledge` segue igual e `materialize_knowledge_context` só ganhou o argumento opcional `unit_loader` (sem ele lê o Vault como antes).
- **Busca sob RLS:** só roda dentro de `scoped_transaction` (somente leitura, role `orq_memory_reader`). Recebe `requirement_id`, `query` e `limit` (1 a 20; fora disso `invalid_search_limit`); família, papel, projeto e catálogo nunca vêm da chamada. O host passa ao SQL as unidades que o catálogo admite para o requisito (autoridade, tipo, classificação, vigência, frescor, aplicabilidade, via `_requirement_reasons` da Fase 2); o RLS decide o que é visível. Todo valor é parâmetro nomeado (`%(nome)s`); nenhum texto vira SQL.
- **Query saneada:** o FTS recebe só as palavras (`[^\W_]+`, sem duplicatas, até 16, unidas por ` or `), de modo que aspas, `-` e `or` do `websearch_to_tsquery` não têm efeito; os trigramas recebem pedaços separados por espaço com ao menos 3 caracteres alfanuméricos (preserva `--trust-tools=fs_read` e `QuotaStateRecord`). Query vazia, com mais de 512 caracteres ou com NUL é rejeitada (`invalid_search_query`), nunca truncada em silêncio.
- **Ranking `pg-fts-trgm-v1`** (constante `RANKING_VERSION`; mudar pesos, limiar ou tokenização exige nova versão):

  ```
  fts   = ts_rank_cd(search_tsv, websearch_to_tsquery('portuguese', palavras), 32)   em [0, 1)
  trgm  = max sobre os termos da query de word_similarity(termo, título + cabeçalhos + conteúdo)
  score = round(0.70 * fts + 0.30 * trgm, 6)
  ```

  A normalização 32 do `ts_rank_cd` divide o rank por `rank + 1`. Entra como candidato quem casa no FTS ou tem `trgm >= 0.5`. Ordem: `(score DESC, source_id, revision, ordinal, unit_id)`, a mesma do Q21 e da Fase 2 (o enunciado do 7D pedia só score e `unit_id`; a chave completa é um refinamento determinístico). O candidato traz metadados, `area_scope`, `estimated_tokens`, digest, score e excerto de até 240 caracteres.
- **Escolha pelo agente:** `KnowledgeChoiceSession.search` registra o que foi devolvido por requisito e confere cada candidato contra o catálogo do host (elegibilidade, digests e tokens). `choose({requisito: [unit_ref]})` só aceita unidades devolvidas pela busca *daquele* requisito (`unit_not_in_search_results`), exige cobertura dos obrigatórios (`mandatory_requirement_uncovered`; `all_source_refs` exige todas as fontes) e confere os orçamentos global (com margem) e por requisito (`context_budget_exceeded`). Nada é cortado: qualquer violação é erro e `session.selection` continua vazio. Requisito opcional descoberto deixa o registro `partial` (`gap_reason: not_selected_by_agent`). A ordem das unidades independe da ordem em que o agente as listou: obrigatórios primeiro e, em cada requisito, pelo rank da busca.
- **Registro e schema:** `KnowledgeSelectionRecord` válido, com `candidates` = tudo que as buscas devolveram. O schema ganhou, de forma compatível, três campos opcionais que só valem juntos (`dependentRequired`): `backend` (`postgres_fts_trgm` ou `offline_markdown_bm25`; o rascunho da seção de memória dizia `postgres_rls_fts_trgm`, valendo o nome do 7D), `selection_mode` (`agent_mcp_choice` ou `host_bm25`) e `ranking_version`. Registros da Fase 2 sem os campos continuam válidos e com o mesmo digest.
- **Consolidação:** `materialize_chosen_context` relê as unidades pelo backend (Postgres: sob o RLS da família, comparando `source_digest` e o SHA-256 do conteúdo com o catálogo; offline: `materialize_unit`), falha antes de qualquer manifesto com `unit_integrity_mismatch`/`selected_unit_missing`/`integrity_mismatch` e então chama `materialize_knowledge_context`: mesma ordem, digests JCS, procedência e bundle canônico da Fase 2. Com a mesma seleção, o manifesto e o bundle saem idênticos byte a byte do Postgres, do Vault e de linhas simuladas. Uma seleção só é materializada pelo backend que a produziu (`backend_mismatch`); numa troca de família, o conteúdo exclusivo da anterior fica invisível e a nova tentativa faz nova busca e novo manifesto (Q26).
- **Modo offline:** `OfflineMarkdownBackend` aplica em memória o mesmo filtro do RLS (status ativo, projeto, audiência nos dois eixos; coleções ficam fora, como no 7B) e ranqueia com `bm25-v1` sobre as unidades elegíveis, com o mesmo formato de candidato, a mesma escolha e `backend: offline_markdown_bm25`. `create_knowledge_backend(config, ...)` escolhe o backend pela configuração do 7B (sem `ORQ_MEMORY_DSN`, offline; `ORQ_KNOWLEDGE_BACKEND` força).
- **Fora do 7D:** servidor/tools MCP (`search_family_knowledge`, `select_family_knowledge`), log de auditoria da trilha e integração com o runner ficam no 7E.

Verificação: `tests/test_knowledge_choice.py` (sem banco: validação da escolha, orçamento, reprodutibilidade, manifesto a partir de linhas simuladas, integridade, query/SQL/ranking, modo offline e schema) e `tests/test_knowledge_search_postgres.py` (relevância em português e por trigramas, fórmula do score, ordem, isolamento entre famílias, injeção de SQL, escolha inválida, orçamento, manifesto reproduzível a partir do Postgres, troca de família e integridade). Os testes de banco rodam no CI quando não há `ORQ_MEMORY_DSN`.

| Cenários | Cobertura executada no 7D |
| --- | --- |
| Q17 | Query hostil tratada como literal: saneada em Python, parametrizada no SQL; escopo e RLS inalterados (banco) |
| Q20 | FTS português (flexões) e `pg_trgm` (flag e identificador parcial), unidade no topo (banco) |
| Q21 | Desempate `(score, source_id, revision, ordinal, unit_id)` na ordenação do SQL e no Python |
| Q22 | Modo offline com filtro de audiência em memória e `backend` no registro (sem banco) |
| Q24, Q25 | Escolha pelo agente, `KnowledgeSelectionRecord` com `agent_mcp_choice`, manifesto/bundle e erros estruturados; a tool MCP em si fica no 7E |
| Q26 | Sem reaproveitar contexto exclusivo na troca de família, no nível da biblioteca; o runner fica no 7E |
| Q27 | Bytes alterados no banco bloqueiam a materialização (`unit_integrity_mismatch`); o check `context_integrity` do runner fica no 7E |

O 7D está na dev. A integração do **7E** está descrita abaixo; próximo incremento: **7F**.

## Notas de implementação 7E

- **Fronteira e alternativas:** pré-selecionar pelo host contrariaria a decisão 4 do dono; reescrever a intenção após a execução quebraria a imutabilidade do journal. Implementado um contexto inicial vazio, válido e identificado como a referência original do despacho, mais uma sessão MCP fixada pelo runner. O contexto efetivo é uma nova publicação no evento **`attempt_context_sealed`**, após término confirmado e antes de `completed`. O evento contém configuração, trilha, seleção e manifesto; `AttemptProjection.effective_context_manifest_ref` aponta para ele. Cabeçalho, DispatchIntent e bundle original nunca mudam. O caminho sem `FamilyRuntime` mantém as Fases 1–6 e S01–S63.
- **API:** `SequentialRunner.create/open(..., family_runtime=FamilyRuntime(...))`. `FamilyRuntime` recebe perfis de assinatura, `executor_factory(runner)`, `backend_factory(scope, policy)` opcional, configuração de memória e `probe(target, request)` opcional. O backend padrão é o do 7B/7D: sem DSN, Markdown; com DSN, Postgres sob RLS. Catálogo com assinaturas exige esse runtime; perfil inválido é rejeitado antes de construir o executor. Snapshots de perfis/backend/hub entram no digest da configuração do run e são conferidos na retomada, sem DSN ou credenciais.
- **Conexão de execução:** o executor recebe `bind_knowledge_session(intent, session)` depois da intenção durável e antes de `dispatch`. `SubscriptionHub` delega por destino e reconcilia pela intenção já gravada, preservando a identidade de cada adaptador; `target_executor_refs` no RunManifest lista os executores autorizados. A aplicação conecta a sessão pelo conector nativo verificado da CLI; `session.stdio_launch(vault_root, python_executable=..., path_mapper=...)` fornece comando/argv sem shell, catálogo fixo e digest esperado da configuração. O mapeamento suporta o caminho WSL do Kiro. Sem conector, o hub recusa com término confirmado, sem despachar CLI. Não se presumem flags/controles comprovados nas versões reais: V01–V12 e a conexão nativa continuam gates para uso real. O agente dos testes usa o cliente MCP oficial em memória **durante `dispatch`**, nunca uma escolha feita pelo runner.
- **Tools:** `search_family_knowledge(requirement_id, query, limit=20)` e `select_family_knowledge(selections, rationale=None)`. Run/tarefa/tentativa/família/papel/projeto/catálogo vêm exclusivamente da configuração do host; argumentos extras são recusados e auditados. Busca limitada a 20 candidatos, query a 512 caracteres, justificativa a 1024 e trilha a 100 chamadas. O backend faz RLS/filtro offline; a escolha reutiliza o 7D e só entrega conteúdo após validar cobertura, orçamento e digests. A sessão aceita **uma seleção final bem-sucedida** (buscas e correções anteriores podem ser repetidas); isso evita acumular entregas fora de um orçamento único. Reinício restaura candidatos e seleção da trilha, sem repetir consultas. Resources são filtrados pelo escopo/requisitos; leitura de unidade exige seleção prévia e devolve os mesmos bytes já entregues.
- **Auditoria:** configuração e arquivos `intent-N`/`call-N` são publicados exclusivamente, com fsync e encadeamento SHA-256, fora do workspace. Cada resposta completa é persistida antes de ser devolvida. Intenção sem resultado é indeterminada e bloqueia selagem; não se repete uma chamada nem se inventa uma entrega. A selagem confere encadeamento, candidatos consultados, seleção e bytes do bundle/unidades, e copia a trilha para o journal. Não relê conhecimento mutável no fim: lacra os bytes efetivamente disponibilizados. Cada nova admissão tem sessão própria; admissões abandonadas não reutilizam configurações de outra RoutingDecision.
- **Integridade antes do despacho:** `preflight()` relê e verifica os bytes elegíveis à família/requisitos, sem escolher unidades nem entregar conteúdo. Divergência gera `integrity_mismatch`, rejeição e zero tentativas consumidas (Q27), tanto no Markdown quanto no Postgres. Isso também evita que o bundle inicial vazio esconda alterações anteriores ao despacho. Falta de seleção válida durante a execução impede `completed`; ver a ressalva temporal de Q25 na matriz.
- **Roteamento:** `route_and_finalize(..., quota_snapshot=QuotaStateSnapshot, implementation_families=...)` conserva o comportamento anterior sem argumentos novos. `subscription_rules` aplica as seis ordens do dono; tipos explícitos prevalecem, e os tipos genéricos são resolvidos por papel (`software_architect`, `tester`, revisores) ou `generation` → implementação. Demais tipos mantêm prioridade/ID do catálogo. Revisão exclui famílias que implementaram dependências, inclusive transitivas. Capacidade, controles e classificação continuam filtros obrigatórios; a prioridade não promove unknown. Pools bloqueados recebem motivo `quota_exhausted` com referência ao registro. `rules` e `fixed` com fallback configurado só fazem fallback de cota para outra família; esgotamento total produz `no_eligible_target` com `all_eligible_families_quota_exhausted`, sem novo despacho.
- **Coleta e retry:** o ErrorRecord confirmado/retryable `quota_limit_reached` produz `quota_exhausted_recorded`; a tentativa falha e segue as políticas/limites existentes (`retry_wait` → nova admissão). A recusa **consome uma tentativa**, sem exceção silenciosa a `max_attempts`. O retry tem nova decisão, servidor/escopo, seleção e manifesto; o contexto exclusivo anterior permanece só no histórico. Rede/login usam o motivo do 7C em `execution_failed.message`, bloqueado apenas no run corrente e sem inventar reset/cooldown.
- **Reset e sondagem:** horário explícito vencido produz `quota_reset_observed` antes da decisão seguinte. Cooldown usa 60 minutos (ou override explícito do perfil). Vencimento de cooldown exige `quota_probe_requested` durável antes do callback leve e `quota_probe_observed` com artefato de evidência. Só `available: true` reativa; `false` renova o cooldown e `null` mantém o bloqueio. Intenção de sondagem sem resultado nunca é repetida na retomada. Ausência de porta de sondagem mantém o pool bloqueado. Replay recusa reset prematuro ou sem prova positiva do cooldown.
- **Custo e contratos:** reservas de assinatura e UsageRecords mantêm `unavailable`, `amount: null`, `currency: null`. O consumo vem exclusivamente de `quota_consumption(intent)` do adaptador e é ligado ao UsageRecord pelo digest integral exigido no 7A. Projeções conferem esses vínculos e preservam `unavailable_count`/`total_cost_exact: false`; subtotais de custos conhecidos não viram um custo total zero. Teto financeiro obrigatório sem cobertura monetária de assinatura rejeita a admissão. Extensões aditivas: `knowledge_session_ref` no cabeçalho/intenção, `target_executor_refs` no manifesto e três eventos novos (`attempt_context_sealed`, `quota_probe_requested`, `quota_probe_observed`). Nenhum schema novo, fixture antiga ou dependência nova.
- **Ajustes de fidelidade:** AGY com diagnóstico explícito `no output produced` usa o novo código `permission_or_empty_output` (ruído `google.Init` continua filtrado). Campos de audiência no frontmatter recebem `forbidden_frontmatter_field`; os erros anteriores do parser permanecem para os outros campos. Os testes de vocabulário do 6A reconhecem somente as extensões da Fase 7.

Verificação nova: `tests/test_phase7_scenarios.py`, `test_family_mcp.py`, `test_phase7_boundaries.py` e `test_phase7_postgres.py`. Os três testes Postgres novos exigem `ORQ_MEMORY_DSN` (fallback completo sob RLS, alteração de bytes antes do despacho e CLI `vault sync-db` repetida com fontes operacionais/de projeto). As verificações de RLS/busca do 7B/7D e dos adaptadores do 7C continuam compondo a matriz; nenhuma CLI real, API paga ou rede externa é chamada pelos testes locais.

### Revisão independente do 7E

Os seis achados da revisão cruzada foram corrigidos com regressões, preservando a compatibilidade com as Fases 1–6:

1. **Indisponibilidade não-cota (`network_unavailable`, `authentication_unavailable`):** erros confirmados pelo detector são marcados como `retryable` sem fabricar `quota_state` ou `reset_at`, permitindo fallback para outro destino sem classificar erroneamente como `quota_fallback` quando não há cota bloqueada acima do selecionado.
2. **Retomada após `attempt_context_sealed` vazio:** quando a tentativa falha por contexto inválido e o processo cai entre a selagem e o erro, a retomada repõe `context_rejected` antes de `AT07` sem redespachar nem duplicar eventos.
3. **Guarda compartilhada `AT07`/`AT13` (`_finished_error_allowed`):** reconciliação `found_finished` em tentativas com `knowledge_session_ref` aceita os mesmos códigos de falha da Fase 7.
4. **Escopo/revogação no replay (`_sealed_scope`):** `project_journal(..., knowledge_catalog=...)` valida no replay que todas as unidades buscadas e selecionadas pertencem ao escopo da sessão, sem revogação nem adulteração de catálogo/digests.
5. **Unicidade e vínculo de sondagem (`quota_probe_observed`):** exige resposta vinculada ao `blocked_record_ref` do bloqueio ativo, sem duplicar `probe_id` nem anteceder `requested_at`.
6. **Fallback e desbloqueio em `all_eligible_families_quota_exhausted`:** o Router considera toda a lista de alternativas na ordem da regra e cita os eventos bloqueantes junto ao menor `blocked_until`.

Além disso, entrou a gramática `codex-quota-v2` (com a fixture real `fixtures/v0.1/cli-output/codex/usage-limit-2026-11-14.txt` para datas absolutas locais) e foram reforçados os cenários `Q03`, `Q09`, `Q10`, `Q11` e `Q12`.

## Notas de implementação 7F

- **Regime `shared_global` isolado em DDL v2 (`schema_v2.sql`):** mantém intocado o schema v1 (`schema.sql` / `orq_memory_reader`) e cria a role sem login `orq_memory_global_reader` (`NOSUPERUSER`, `NOBYPASSRLS`) com políticas fail-closed que continuam exigindo as 4 variáveis de sessão, `catalog_id`, `resolved_status = 'active'` e escopo de projeto/operacional global, omitindo apenas `area_scope`/`target_families`/`target_roles` para o braço de controle experimental. Em `OfflineMarkdownBackend`, `scope_allows` espelha o mesmo comportamento via `MemoryScope.memory_regime`.
- **Harness `orq_contracts.experiment_7f` e CLI `experiment-7f`:** executa verificação criptográfica do pacote (`verify-package`), Camada A pareada com MCP real por tentativa (`simulate`, cobrindo `Q28`), comparação de estresse de cota (`quota-stress`, cobrindo `Q29`), execução em lotes de Camada B (`run_live_batch` para chamadores com CLIs reais) e auditoria criptográfica/estatística (`audit`, cobrindo `Q30` + mediana, IQR, Wilcoxon bilateral exato, McNemar exato, odds ratio e delta de Cliff em stdlib). Cobertura automatizada em `tests/test_phase7_experiment_7f.py` e `tests/test_experiment_7f_benchmark.py`. O diretório `experiments/7f/` não é modificado em runtime.
- **Compatibilidade retroativa do `SequentialRunner` e `journal-event.schema.json`:** adicionados `contracts` opcional em `SequentialRunner.create`/`open` e `SimulationScript.validate_against_workflow` (para contratos de resposta específicos das 10 tarefas 7F), `experiment_metadata` em `policy` e propriedade opcional `memory_regime` em `attempt_context_sealed.session_config.scope`. Nenhum digest fixado anterior foi alterado.

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
