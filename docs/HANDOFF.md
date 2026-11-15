# Retomada do projeto ORQ

Atualizado em 2026-11-14. Este é um resumo operacional da conversa e do repositório, não uma transcrição integral do chat. Atualizar após cada entrega; em caso de divergência, conferir arquivos e histórico Git.

## Leitura para retomar

Leia [AGENTS.md](../AGENTS.md), este resumo e o [índice da Fase 1](phase-1/README.md). O usuário prefere respostas curtas em português.

## Objetivo e método

Criar uma plataforma experimental de orquestração de agentes baseada em MCP, independente de provedores. Integrações de execução desejadas: Codex, Claude e Antigravity/AGY, ainda sem SDK ou versão concreta escolhidos; o SDK TypeSafe 0.7.2 foi escolhido apenas para o Router opcional.

O usuário pediu arquitetura antes de implementação: problema, alternativas, recomendação, interfaces, schemas, riscos e testes. Apoiou o plano incremental. A implementação começa por schemas, validadores e testes após fechar a Fase 1; não construir o sistema inteiro antecipadamente.

Papéis previstos incluem orchestrator, software_architect, backend_engineer, frontend_engineer, researcher, tester, code_reviewer e security_reviewer. São responsabilidades, não processos permanentes nem vínculos fixos com modelos.

## Estado atual

- **7E implementado em 2026-11-14, para revisão, sem commit/push:** tools MCP `search_family_knowledge`/`select_family_knowledge` com escopo exclusivo do runner, trilha durável por tentativa e selagem do contexto efetivamente entregue. `FamilyRuntime`/`SubscriptionHub` integram o runner com destinos de assinatura; `QuotaStateSnapshot` aplica prioridades do dono, bloqueio por pool, retry em outra família, reset e sondagem de cooldown com intenção anterior ao efeito. O teste ponta a ponta termina **succeeded** depois de Codex esgotar cota e Claude fazer nova busca/seleção/manifesto, sem contexto exclusivo anterior, com custo `unavailable/null`, consumo vinculado e journal relido/reprojetado. Retomada não redespacha intenções nem repete sondagem indeterminada. Extensões opcionais nos schemas preservam as fixtures/Fases 1–6; sem dependência nova. [Decisões/API](phase-7/README.md#notas-de-implementação-7e) e [cobertura Q01–Q27](phase-7/acceptance-scenarios.md#cobertura-automatizada-do-7e). **Q25 tem ressalva temporal explícita:** a escolha durante a execução, decidida pelo dono, ocorre após o despacho; os dois erros da tool são testados e contexto inválido impede completed, sem simular pré-seleção. Os três testes Postgres novos e o smoke stdio exigem a verificação do CI. Verificação ampliada dos arquivos afetados: **1051 aprovados, 66 ignorados e 2 desmarcados**, incluindo S01–S63; os 66 são 64 testes Postgres sem DSN e dois de symlink. Os dois smokes stdio aguardam CI. Após o ajuste de borda de fallback na mesma família, **22 testes de roteamento aprovados**. `git diff --check` sem erros. Essa verificação não substitui a suíte integral do orquestrador. Próximo incremento: **7F**, após revisão, CI e gates V01–V12/conector nativo das CLIs (controles reais continuam unknown).
- **7D implementado em 2026-11-14**: busca híbrida no Postgres (FTS em português `ts_rank_cd` + `pg_trgm` `word_similarity`, ranking versionado `pg-fts-trgm-v1`: `0,70·fts + 0,30·trgm`, desempate `(score, source_id, revision, ordinal, unit_id)`) sob a transação com escopo/RLS do 7B, com `limit` de até 20 e query saneada e parametrizada; escolha final pelo agente validada (só IDs da busca do mesmo requisito/escopo/catálogo, cobertura obrigatória e orçamento sem truncar) gerando `KnowledgeSelectionRecord` com `backend`, `selection_mode` e `ranking_version` (campos opcionais novos no schema, registros da Fase 2 intactos); `materialize_chosen_context` reaproveita `materialize_knowledge_context` (novo argumento opcional `unit_loader`) e confere `content_digest` antes de materializar, com manifesto e bundle idênticos byte a byte ao do Vault; interface `KnowledgeBackend` com `PostgresKnowledgeBackend` e `OfflineMarkdownBackend` (BM25 com o filtro de audiência em memória). Sem dependências novas. Suíte local: **1388 aprovados e 65 ignorados** (os 28 testes de banco novos rodam no CI quando não há `ORQ_MEMORY_DSN`). Decisões nas [Notas de implementação 7D](phase-7/README.md#notas-de-implementação-7d). Próximo: **7E** (tools MCP escopadas, trilha auditável e integração com o runner); o 7C segue em paralelo.
- **Revisão independente do 7C (Gemini) corrigida em 2026-11-14** (suíte integral 1439 aprovados e 38 ignorados; adaptadores repetidos 3 vezes sem intermitência), achados 1–8: temporários únicos com fsync e substituição atômica, retries limitados de PermissionError no Windows também na leitura, releitura após saída do supervisor, parsing conservador de stdout/final e precedência da cota fatal, limpeza de filho suspenso fora do Job, espera limitada e confirmação do grupo POSIX, validação antes dos diretórios e snapshot único da reconciliação. Testes verificam ambiente recebido pelas quatro CLIs falsas e montagem WSL sem dependência do basetemp; estresse repete 25 saídas rápidas Kiro com leitor concorrente. **118 aprovados e um ignorado** nos dois arquivos de adaptadores/cota; `git diff --check` sem erros. [Notas e regressões por achado](phase-7/README.md#revisão-independente-do-7c--2026-11-14). Worktree `claude/fase7-7c`, sobre dev com 7B; alterações sem commit/push. Sem mudanças em memória, banco, CI, schemas ou dependências. Próximo: orquestrador repetir a suíte integral, revisar as correções e executar smoke V01–V12; controles seguem unknown.
- **7C implementado em 2026-11-14, para revisão; smoke real pendente**: quatro adaptadores `ExecutorAdapter`, supervisor stdlib com estado durável antes do spawn, stdin fechado, timeout/cancelamento da árvore, parsers e detector de cota versionado. Usa perfis/erros/estado/consumo do 7A e mantém custo unavailable/null. **90 aprovados, um ignorado** nos arquivos novos (symlink no Windows), sem CLI real/rede; essa verificação não substitui a suíte integral. Todos os controles continuam unknown até evidências por versão; configuração local AGY e execução do supervisor Python no WSL precisam de smoke. Decisões, API e cobertura na [tabela V01–V12](phase-7/subscription-adapters.md#cumprimento-do-checklist-v01v12-por-adaptador) e nas [Notas 7C](phase-7/README.md#notas-de-implementação-7c). Sem dependências novas, commit ou push. Nenhuma alteração em memória, banco, DDL ou CI; **7B segue em paralelo em outra branch**. Próximo: revisão do 7C, suíte integral e smoke V01–V12; integração/sondagem/fallback ficam no 7E.
- **7B implementado em 2026-11-14** (revisado pelo Gemini 4 Argon High sem problemas; CI com Postgres 16 de serviço: 1355 aprovados e 3 ignorados, incluindo os 33 testes de banco/RLS): pacote `orq_contracts.memory_store` com DDL versionado (`schema.sql`, versão 1), RLS habilitado e forçado em catálogo/fontes/unidades, roles sem bypass (`orq_memory_owner`, `orq_memory_writer`, `orq_memory_reader`) e políticas pelas quatro variáveis `app.*`; sincronização determinística Vault → Postgres reaproveitando ingestão, chunker, `materialize_unit` e `canonical_digest`; leitura com escopo fixado pelo host (`MemoryScope`, `SET LOCAL ROLE` + `set_config(..., true)`, transação somente leitura); `ORQ_MEMORY_DSN` liga o Postgres e, sem ele, o modo `offline_markdown_bm25` segue sem mudança. CLI `vault sync-db`, `docker-compose.memory.yml` opcional e service container `postgres:16` no CI. Dependência nova: `psycopg[binary]==3.3.9`. Suíte local: **1321 aprovados e 37 ignorados** (os 4 anteriores e 33 testes de banco, que rodam no CI quando não há `ORQ_MEMORY_DSN`). Decisões nas [Notas de implementação 7B](phase-7/README.md#notas-de-implementação-7b). Próximo: **7C**.
- **7A implementado e revisado em 2026-11-14** (Gemini, sem problemas; suíte integral 1280 aprovados e 4 ignorados): cinco schemas fechados (família, audiência, assinatura, estado e consumo de cota), extensões compatíveis de catálogo/roteamento/uso/erro/journal, validação semântica e fixtures JCS fixadas. **378 testes aprovados, nenhum ignorado**, nos arquivos de contratos 7A, schemas 6A e inventário de schemas. Decisões, API e cobertura parcial de Q02/Q07–Q10/Q12–Q13 e contratual de Q14 nas [Notas de implementação 7A](phase-7/README.md#notas-de-implementação-7a). Nenhuma fixture anterior alterada; sem dependências novas, banco, adaptadores, busca ou MCP.
- Baseline documental v0.1 e Fases 2–5 concluídos. **6E implementado**: runner sequencial e CLI `orq run simulate`, sem chamada real de modelo. A revisão do 6E foi corrigida e a Fase 6 está concluída: suíte integral verificada pelo orquestrador em 2026-11-14: **1180 aprovados e 4 ignorados** (symlinks sem privilégio no Windows).
- **Entrega 6E em 2026-11-14:** `SequentialRunner.create/open/run/cancel/parar`, journal validado antes da escrita, admissão completa, tentativas, retry limpo, avaliação local/global, reconciliação, parada e cutoff. Retomada usa `recovery_actions` e checkpoints duráveis do simulado; intenção existente nunca é redespachada. Template T1–T5 e roteiro próprios fecham o achado 11 do 6A; fixtures anteriores intactas. S02 termina succeeded e S03 comprova journal idêntico byte a byte. Cobertura fiel: **63/63**, auditada contra todos os “Então”. S04 foi revisado pelo orquestrador em 2026-11-14: checkpoint TK05; ambos os testes foram mantidos e o corte após output_bound confere T1 running e T2 pending. [Tabela completa](phase-6/acceptance-scenarios.md#cobertura-executada-no-6e) e [decisões/API do 6E](phase-6/runner-state-machine.md#notas-de-implementação-6e). Sem dependências novas, rede, commit ou push nesta entrega.
- **Revisão independente do 6E em 2026-11-14:** problemas 1–9 corrigidos com regressões: recuperação em laço antes da parada, RN03/RN04 após crash, reaplicação de reconciliação conclusiva, liquidação antes do término, gates financeiros, cancelamento indexado, AT13 sem cancelamento prévio, reservas de avaliação, indeterminação global, relógio da auditoria e relatório obrigatório em TK24. Reforçados os 19 cenários da revisão e outros 27; os demais 17 foram auditados e mantidos. S10 usa conhecimento real e rejeita revisão fora do catálogo no retry. Decisões nas [Notas 6E](phase-6/runner-state-machine.md#revisão-independente-do-6e--2026-11-14). Nenhum schema, dependência ou fixture alterado nesta revisão; sem commit/push. **364 testes aprovados, nenhum ignorado**, nos quatro arquivos de runner/CLI/projeções/máquina de estados; git diff --check sem erros. Essa verificação não substitui a suíte integral do orquestrador.
- Fase 1 concluiu contratos; Fase 2 entregou contratos/Vault; Fase 3 expôs conhecimento por MCP; Fase 4 entregou planner fixo e admissão central do WorkflowSpec.
- Aceitos após revisão cruzada: TaskSpec, WorkflowSpec, ciclo, perfis, RoutingDecision, ContextManifest, ExecutionPolicy, artefatos, registros de execução/avaliação e metadata de conhecimento do Vault.
- Especificados 83 cenários gerais, oito casos de perfis, dez de roteamento/contexto, 12 de política, 14 de artefatos, 16 de registros e 17 de conhecimento. Não são testes executados.
- TypeSafe AI/Jev foi incorporado como adaptador opcional de `DecisionEngine`, atrás de uma interface independente de provedor. O SDK oficial 0.7.2 está fixado; nenhuma credencial foi adicionada nem houve chamada real.
- ADR 0001 escolheu Python 3.12, JSON Schema Draft 2020-12, jsonschema/pytest e RFC 8785/JCS.
- Implementados schemas comuns, TaskSpec/WorkflowSpec e conhecimento, validação semântica, CLI, fixtures e digest SHA-256 canônico.
- Implementados parsing seguro de frontmatter, paths confinados, ingestão por digest, chunking por headings e snapshot com revogação append-only.
- Implementados filtros de projeto/autoridade/classificação/validade/applicability, BM25 determinístico por requisito, cobertura obrigatória antes da opcional, orçamento global/local e materialização com nova verificação de digest.
- Implementados ContextManifest fechado e bundle JSON canônico separado, com ordem, procedência, digests cruzados e orçamento do destino.
- Implementadas fixtures reais e CLI atômica de catálogo, seleção e contexto, sem sobrescrita implícita.
- Histórico da Fase 2: 52 testes locais aprovados, dependências íntegras, links locais, formatação Git, sequência dos IDs dos cenários e soma do exemplo financeiro fictício.
- Fase 3 implementada com MCP Python SDK 2.2.0, `stdio`, JSON canônico, testes em memória e resources concretos somente leitura.
- Histórico da Fase 3: 58 testes aprovados; o smoke `stdio` precisou rodar fora da sandbox porque o pool de threads do AnyIO é bloqueado nela, e passou no ambiente local irrestrito.
- Fase 4 implementada com schemas, interface `Planner`, `FixedTemplatePlanner`, PlanningRecord e gate central de WorkflowSpec.
- Fase 5 implementou schemas de roteamento, filtro obrigatório, baselines `fixed`/`rules`, interface tipada, transporte simulado e adaptador opcional Jev.
- Histórico da Fase 5: 91 testes aprovados, inclusive smoke MCP fora da sandbox; dependências consistentes. O resultado atual está abaixo, no fechamento do 6A.
- Fase 6 especificada e aceita em 2026-11-10 ([phase-6/README.md](phase-6/README.md), [ADR 0007](adr/0007-phase-6-runner-and-simulated-executor.md)): contratos, máquina de estados e 63 cenários S01–S63. O 6A entrega oito schemas, 14 fixtures válidas, oito inválidas e testes estruturais/digests. O 6B implementa journal e armazenamento de artefatos; runner, executor e CLI permanecem sem implementação. Os cenários do runner ainda não são testes executados.
- A especificação recomenda executor simulado primeiro e só a interface do adaptador real. Os controles das CLIs candidatas (Claude Code, Codex CLI, Antigravity CLI, Kiro CLI) estão todos marcados a verificar; a web estava indisponível na redação e nenhuma CLI foi executada.
- A lacuna entre `route_and_finalize` e `materialize_knowledge_context` tem solução aceita na ADR 0007: checks dependentes do bundle ficam no DispatchIntent. O 6A fecha `admission_checks`, incluindo os seis controles mínimos, todos `passed`.
- Escolhas do 6A e da revisão independente estão em [Notas de implementação 6A](phase-6/run-contracts.md#notas-de-implementação-6a). Corrigidos os achados 1–10 e 12–16: reservas mantêm `amount: null`/`status: unavailable` sem estimativa, agregados de artefatos/erros correspondem à origem, criação é implícita nos eventos iniciais, resumo contém só tarefas terminais e não inventa moeda. Fixtures têm referências e hashes consistentes; códigos/motivos são enums e textos livres do 6A têm limite de 4096 caracteres. Execução de avaliação não concluída só admite `inconclusive`/`not_run`.
- Achado 10: o baseline exige `pass_rule` não vazio. Mantida a forma existente; `validate_instance` passou a exigir igualdade de `output_ids` do critério global e pass rule, após validação estrutural, em EvaluationReport e payloads de `evaluation_requested`/`evaluation_report_recorded`. Consumidores do journal em 6B/6C precisam usar essa API; `validator_for` continua estrutural. Outros vínculos e agregação completa seguem no 6C. Não há novo serviço/dependência.
- Achados 14–15: chave do despacho só é reutilizada no payload de reconciliação, com chave própria por requisição no envelope; a reserva da avaliação local é liberada após término confirmado, sem UsageRecord fictício. Término indeterminado mantém a reserva pendente.
- 6A: 41 pares agregado/evento permitidos e 79 proibidos cobertos por testes.
- **6B implementado e achados 1–19 corrigidos**, somente armazenamento: `RunJournal.create/open/append/close`, lock com PID/host/horário/UUID e remoção condicionada ao token; escrita JCS seguida de flush/fsync; validação, cadeia de hashes/sequências/causas e idempotência por payload/tipo/agregado. `ArtifactStore(journal_aberto)` exige a posse do lock e publica bytes por SHA-256, com temporário no mesmo diretório, fsync, link + unlink no POSIX ou rename no Windows e conferência do destino. Destino existente e journal reaberto são sincronizados antes do uso. Limites de 1 MiB incluindo LF e 8 MiB, inclusive na leitura; erros tipados também para ausência, confinamento e colisão por caixa. `.pending-*` é reportado para inspeção manual. Sem mudanças nos schemas, dependências ou testes antigos. Decisões e API nas [notas do 6B](phase-6/run-contracts.md#notas-de-implementação-6b). ErrorRecord/stopping, projeções e máquina de estados continuam fora do incremento.
- Decisão do orquestrador em **2026-11-10**: só a cauda **sem LF** é descartável; linha inválida com LF e journal só com cauda são `journal_integrity_error`. O último evento de escrita com falha pode reaparecer confirmado após reabertura; o 6C deve tratar isso como intenção sem efeito comprovado, encaminhando despacho à reconciliação. Colisão de run IDs apenas por caixa é recusada com `run_id_case_collision`.
- Histórico integral verificado pelo orquestrador (2026-11-10), comando `PYTHONPATH=src python -m pytest -p no:cacheprovider --basetemp=<pasta gravável>`: **6A = 366 aprovados e 1 ignorado**, CI verde no PR #8; **6B antes da revisão = 432 aprovados e 1 ignorado**; **6B após a correção dos achados 1–19 = 485 aprovados e 4 ignorados** (symlinks sem privilégio no Windows; junctions cobertas).
- Após as correções: **119 aprovados e 3 ignorados** nos 122 casos de `tests/test_journal.py`, `tests/test_artifact_store.py` e `tests/test_storage.py`. Os três skips são symlinks sem privilégio no Windows; os três casos de junction passaram.
- **6C implementado** em `state_machine.py` e `projections.py`: tabelas das 49 linhas AT/TK/RN, guardas com evidências anteriores, replay puro dos dicionários do journal, avaliações/bindings, erros/usos, reservas e custo por cadeia de correções, cutoff publicado e ações de retomada sem I/O. `project_journal(events, workflow=...)` recebe o WorkflowSpec e confere sua referência/digest, pois seus bytes não estão no journal. Criações continuam implícitas; `unknown` e intenção reaparecida após fsync falho nunca autorizam novo despacho. `not_found` só é conclusivo com controle verificado explicitamente informado e evidência no evento; AT04 exige prova volátil do runner e nunca é recomendado após crash. Decisões nas [Notas 6C](phase-6/runner-state-machine.md#notas-de-implementação-6c).
- Verificação inicial do 6C: **140 testes próprios aprovados**; suíte integral anterior verificada pelo orquestrador: **625 aprovados e 4 ignorados**. A revisão independente (Kiro, Claude Opus) gerou as correções dos **achados 1–13**, registradas nas [Notas 6C](phase-6/runner-state-machine.md#notas-de-implementação-6c): evidência de falha posterior/confirmada e causa do retry preservada; prazo inicial de reconciliação fixo; dependentes falhos bloqueados; avaliação terminal sem relatório/liberação tardios; orçamento opcional com `budget_committed: None` e `budget_gaps`; AT04 sem prova financeira de não execução; confirmação compatível com AT03/AT07; causa de parada fixada em RN07; rejeição de admissão com erros e reservas liberadas.
- Verificação pós-revisão do 6C pelo orquestrador: suíte integral com **702 aprovados e 4 ignorados** (symlinks sem privilégio no Windows).
- Após a revisão: **217 aprovados, nenhum ignorado**, em `tests/test_state_machine.py` e `tests/test_projections.py`, com `PYTHONPATH=src /c/dev/orq/.venv/Scripts/python -m pytest -q -p no:cacheprovider --basetemp=.pytest-tmp tests/test_state_machine.py tests/test_projections.py`. Cobrem linhas AT/TK/RN, todos os saltos proibidos entre estados, regressões negativas, S31 completo, tentativa 2 em S32 e integração com `RunJournal`/falha de fsync. Suíte integral após as correções: 702 aprovados e 4 ignorados; os 63 cenários não foram executados pelo runner. Sem mudanças nos schemas, dependências ou testes anteriores ao 6C.
- **6D implementado**, somente interface/executor simulado/avaliador: `executor.py`, `simulated_executor.py`, `evaluator.py`, schema fechado de roteiro e fixture versionada. Roteiro validado por estrutura/digest JCS; seis operações sem rede/journal; artefatos somente por `ArtifactStore`; despacho/coleta idempotentes. Avaliador suporta os quatro métodos, produz EvaluationReport/ErrorRecord válidos e nenhum UsageRecord; `unknown_termination` não fabrica relatório. Verificação inicial: 148 testes próprios aprovados. Decisões/API nas [Notas 6D](phase-6/executor-choice.md#notas-de-implementação-6d).
- **Revisão do 6D, achados 1–14 corrigidos** (revisão em `.revisao-6d.md`), com decisões do orquestrador de **2026-11-10** registradas no [README da Fase 6](phase-6/README.md#decisão-do-orquestrador-de-2026-11-10-gate-do-6d) e nas Notas 6D:
  - Reconciliação: chave desconhecida devolve `unknown` não autoritativo. `not_found` autoritativo só com estado durável (`state` + `persist`, registro JSON com `state_hash` reconstruível por instância nova) e controle declarado. A versão anterior negava com autoridade execuções desconhecidas.
  - Avaliador relê e confere todos os `subject_bindings` antes dos critérios; divergência ou ausência gera `fail` e `integrity_mismatch` em todos os métodos suportados. `ArtifactEvidence` é o payload `artifact_ingested`; `format` é só anotação.
  - Hooks com escopo explícito (`run`, `task_attempt`, `workflow`) e disparo único por `HookController`, persistido pelo harness.
  - Envelope `from_output_spec` com schema versionado `simulated-output.schema.json` e referência com digest (`simulated_output_contract()`); o simulado recusa outros contratos.
  - Roteiro contraditório recusado na carga; `validate_against_workflow` antes da admissão. Controles no formato do RoutingTargetCatalog; `observe` com prazo vencido não falha; cancelamento tardio registra o término roteirizado; status `stopped` explícito; relógio injetado obrigatório e igual ao do journal (`RunJournal.clock`).
  - Gate do 6D redefinido pelo orquestrador: interface, simulado e avaliador completos e roteiros representativos validados (`simulation-script.json`, `simulation-script-recovery.json`). Os cenários S01–S63 como testes passam ao gate do 6E.
  - Verificação pelo orquestrador após a correção: digests de `PINNED_DIGESTS` fixados e suíte integral com **932 aprovados e 4 ignorados** (symlinks sem privilégio no Windows). Sem dependências novas.
- **Segunda revisão independente do 6D corrigida em 2026-11-14** (`.revisao-6d-2.md`, achados 1–6 e cinco itens de testes/documentação): reconciliação respeita o roteiro após a duração; construtor persiste o estado inicial antes de declarar autoridade; falhas de persistência revertem mutações de observação/cancelamento/reconciliação/coleta e retiram a durabilidade. Restauração valida invariantes sem reler artefatos; o avaliador pode registrar `integrity_mismatch` após retomada com bytes ausentes/corrompidos. Conferidos tamanho/produtor e IDs finais em `simulated_verdict`; preflight usa validação semântica e `min(tarefa, workflow)`, com lista global de tamanho >= 1. Hooks validam eventos e `fired`. Decisões detalhadas nas [Notas 6D](phase-6/executor-choice.md#segunda-revisão-independente-do-6d-2026-11-14).
- Verificação desta segunda revisão: **332 aprovados, nenhum ignorado**, em `tests/test_simulated_executor.py`, `tests/test_evaluator.py` e `tests/test_schemas.py`. Nenhuma fixture, schema ou valor de `PINNED_DIGESTS` mudou nesta correção. Suíte integral pós-correção verificada pelo orquestrador: **1032 aprovados e 4 ignorados**.
- Publicações anteriores confirmadas em main. Sempre conferir o remoto novamente ao retomar.

## Próximo passo concreto

A especificação da Fase 6 foi aceita em 2026-11-10 ([ADR 0007](adr/0007-phase-6-runner-and-simulated-executor.md), "Decisões do dono"; o dono delegou o aceite ao Claude).

1. Fase 6 concluída em 2026-11-14 (S04 revisado para o checkpoint TK05; achado 11 do 6A fechado pelo template de referência).
2. 7A–7D na dev; **7E implementado para revisão**. Conferir a matriz Q01–Q27 (incluindo a ressalva temporal de Q25), rodar CI com Postgres/stdio e revisar as extensões do runner/journal. Próximo incremento: **7F** e seu desenho experimental já especificado.

O uso dos adaptadores reais pelo runner continua condicionado à sessão de verificação com versão fixada (checklist V01–V12 em [phase-6/executor-choice.md](phase-6/executor-choice.md)) e ao conector MCP nativo verificado. Sondagem/fallback já estão integrados no 7E, com callbacks do host e testes simulados; um resultado inconclusivo não reativa a assinatura.

### Fase 7 — 7A–7D na dev; 7E em revisão; próximo 7F

- Especificação aceita em 2026-11-14 ([ADR 0008](adr/0008-phase-7-subscription-hub-and-family-memory.md) e [phase-7/README.md](phase-7/README.md)); contratos no 7A; Postgres/RLS e sincronização no 7B; adaptadores/detector no 7C; busca/escolha/materialização no 7D; MCP escopado, auditoria e integração cota/contexto no 7E. **7F** permanece pendente, além dos gates reais V01–V12.
- Escopo: hub das quatro assinaturas pessoais via CLIs oficiais em modo não interativo (Claude Code, Codex CLI, Gemini CLI/`agy`, Kiro CLI; sem API paga, sem LLM local, sem extrair tokens nem automatizar web), roteamento ciente de cota com fallback entre famílias e custo monetário `unavailable` (`amount: null`), memória por família em Postgres configurável via Docker com Row-Level Security (FTS em português + `pg_trgm`, escolha final pelo agente via MCP, consolidando `ContextManifest` com digests; BM25 em Markdown preservado como modo offline) e experimento reproduzível 7F (cenários Q01–Q30).
- ADR 0008 aceita em 2026-11-14 com as seis decisões do dono ([Decisões do dono](adr/0008-phase-7-subscription-hub-and-family-memory.md#decisões-do-dono-aceitas-em-2026-11-14)): famílias em dois eixos (assinatura e papel), cooldown de 60 minutos com sondagem leve, prioridades do baseline `rules` por tipo de tarefa, escolha da memória pelo agente durante a execução com auditoria do runner, workspaces do Kiro em `C:\dev` (vistos como `/mnt/c`) e experimento 7F com 10 tarefas × 3 repetições × 2 condições. O 7A só começa depois que o 6E estiver na `main`.

Não é necessário pedir novamente autorização para a documentação ou para seu envio ao repositório já autorizado.

## Fechamento da Fase 1

A revisão cruzada está em [phase-1/cross-review.md](phase-1/cross-review.md). Todos os contratos centrais foram aceitos como baseline v0.1; decisões tecnológicas e valores operacionais permanecem gates explícitos para as etapas que dependem deles.

Não iniciar MCP ou SDKs de agentes antes do Vault local e de seus testes determinísticos.

## Decisões aceitas no baseline v0.1

- Aplicação modular local, sem microserviços no MVP.
- Separação entre papel, modelo, provedor, runtime e executor.
- Comparação inicial de runtimes completos, identificando modelo e ferramentas; não atribuir toda diferença ao modelo.
- MCP como camada de ferramentas/conhecimento; Orchestrator determinístico controla o workflow.
- DAG estático e versionado desde os schemas; concorrência inicial igual a um.
- Todas as tarefas do workflow inicial são obrigatórias. Dependências em TaskSpec são a única fonte das arestas.
- max_attempts inclui a primeira tentativa. Falha definitiva interrompe novas admissões.
- completed não implica pass. Timeout sem término confirmado exige reconciliação antes de retry.
- Retry parte de workspace limpo e só usa artefatos aceitos de dependências, além de feedback identificado.
- Política central no workflow; tarefa só restringe os limites herdados.
- Model Profile contém declarações com procedência, não médias históricas editadas manualmente.
- Capacidades, permissões e qualidade medida são conceitos diferentes.
- Fonte de conhecimento e contexto precisam de identidade, revisão e hash; custo desconhecido não é zero.
- O catálogo de conhecimento é fixo durante o run; revogação append-only pode bloquear despacho, mas revisão nova exige novo run.
- JSON Schema é o formato canônico proposto, com validação semântica complementar.
- Stack, primeiro runtime, valores operacionais dos limites e projeto de benchmark ainda precisam ser escolhidos.

## Roadmap acordado

| Fase | Entrega |
| --- | --- |
| 1 | Contratos, estados e cenários de aceitação |
| 2 | Schemas/fixtures v0.1 e Vault Markdown com recuperação inicial por metadata/busca lexical |
| 3 | MCP para acesso ao conhecimento |
| 4 | Task Planner e validação do plano |
| 5 | Model Router por regras |
| 6 | Primeiro executor e runner sequencial, já com persistência e avaliação mínimas |
| 7 | Segundo provedor/runtime |
| 8 | Evaluator ampliado |
| 9 | Execution Memory ampliada |
| 10 | Métricas e relatórios |
| 11 | DAG concorrente e integração de artefatos |
| 12 | Scheduling experimental |

O objetivo científico é comparar Single-Agent, Multi-Agent com roteamento fixo, LLM Router, regras, métricas e scheduling otimizado. Registrar sobrecarga de planejamento, roteamento, avaliação e retries; preservar condições experimentais. Algoritmos avançados ficam para depois de baselines e evidência suficiente.

## Arquivos e marcos

- [TaskSpec](phase-1/task-contract.md).
- [WorkflowSpec](phase-1/workflow-contract.md).
- [Estados e limites](phase-1/execution-lifecycle.md).
- [Cenários gerais](phase-1/acceptance-cases.md).
- [Perfis](phase-1/agent-profiles.md).
- [Roteamento e contexto](phase-1/routing-context-contract.md).
- [Política de execução](phase-1/execution-policy-contract.md).
- [Artefatos, patches e relatórios](phase-1/artifact-contract.md).
- [Registros de execução e avaliação](phase-1/execution-records-contract.md).
- [Metadata de conhecimento do Vault](phase-1/knowledge-metadata-contract.md).
- [Revisão cruzada e baseline v0.1](phase-1/cross-review.md).
- [Andamento da Fase 2](phase-2/README.md).
- [ADR 0001 — tooling e JSON canônico](adr/0001-contract-tooling-and-canonical-json.md).
- [ADR 0002 — Vault e parsing Markdown](adr/0002-vault-layout-and-markdown-parsing.md).
- [ADR 0003 — materialização do bundle de contexto](adr/0003-context-bundle-materialization.md).
- [ADR 0004 — fronteira MCP do conhecimento](adr/0004-mcp-knowledge-boundary.md).
- [Andamento da Fase 3](phase-3/README.md).
- [ADR 0005 — interface do planner e baseline fixo](adr/0005-planner-interface-and-fixed-baseline.md).
- [Andamento da Fase 4](phase-4/README.md).
- [ADR 0006 — fronteira do Router e decisão tipada](adr/0006-router-boundary-and-typed-decision.md).
- [Andamento da Fase 5](phase-5/README.md).
- [Fase 6 — runner sequencial e executor simulado](phase-6/README.md), 6A–6E implementados; concluída; 63 cenários automatizados.
- [ADR 0007 — runner sequencial e executor simulado](adr/0007-phase-6-runner-and-simulated-executor.md), aceita.
- [Fase 7 — hub de assinaturas pessoais, roteamento por cota e memória por família](phase-7/README.md), 7A–7D na dev; 7E em revisão; próximo 7F.
- [ADR 0008 — hub de assinaturas pessoais, roteamento por cota e memória por família em Postgres](adr/0008-phase-7-subscription-hub-and-family-memory.md), aceita.
- [Fase 8 — avaliador por modelo e rubrica](phase-8/README.md) e [ADR 0009](adr/0009-phase-8-model-and-rubric-evaluator.md), proposta aguardando as decisões do dono.
- 69f7360: documentos iniciais.
- bd5904e: workflow, ciclo e cenários.
- 05050eb: perfis e oito casos de aceitação.
- 8bab500: instruções e contexto para retomada.
- 5961293: roteamento, contexto e encaixe documental de TypeSafe AI/Jev.
- 2cde742: política de execução, permissões, orçamento e evidências.
- 921c06d: ArtifactRef e contratos de patch/relatório.
- 8959bf3: registros de execução, uso, aprovação e avaliação.
- 4720205: metadata de conhecimento do Vault.
- Consultar git log para trabalhos posteriores.

## Git e ambiente

Diretório utilizado: /var/www/orq. Remoto: git@github.com:huguerra1/orq.git; branch main. Identidade pessoal local: huguerra1 <hugofmourao@gmail.com>. A configuração global é de trabalho e não deve ser alterada. Os dois primeiros commits mantêm a identidade anterior; o usuário não autorizou reescrever esse histórico.

A autenticação SSH funcionou após cadastro da chave pelo usuário. O ambiente usou temporariamente /tmp/orq-github-known-hosts, validado contra a chave oficial do GitHub, em core.sshCommand somente por comando. Esse arquivo pode desaparecer; verificar o ambiente atual e a identidade do servidor ao retomar, sem desabilitar verificação de host.

A sandbox apresentou falha bwrap antes de executar comandos e patches. Nesta sessão foram necessárias execuções com aprovação fora da sandbox. Não assumir que essa limitação persiste nem que aprovações são transferidas. Nenhuma credencial foi incluída na documentação.

## Como começar outra conversa

Abrir este repositório no ambiente de trabalho e enviar:

> Leia AGENTS.md e docs/HANDOFF.md. Continue pelo próximo passo registrado. Responda de forma curta.

Para outros ambientes sem leitura automática dessas instruções, fornecer explicitamente esses dois arquivos. Em outro clone, conferir/configurar a identidade Git local: .git/config não é transportado pelo Git.

O uso de AGENTS.md como instruções do projeto segue a [documentação oficial do Codex](https://developers.openai.com/pt-BR/docs/agent-configuration/agents-md). Isso preserva orientações e referências no repositório, sem prometer transferência automática de toda a conversa.
