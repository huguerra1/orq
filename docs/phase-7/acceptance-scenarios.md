# Cenários de aceitação da Fase 7

Estado: aceita em 2026-11-14 ([ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md)); **7E implementado e revisado**. A [tabela de cobertura](#cobertura-automatizada-do-7e) liga Q01–Q27 a testes executáveis e explicita a ressalva temporal de Q25. Os testes de banco exigem `ORQ_MEMORY_DSN`; Q28–Q30 continuam no **7F**. Automação com CLIs falsas não substitui o smoke V01–V12 de versões reais.

## Estratégia e convenções

Os cenários seguem o formato `Dado / Quando / Então / Base` adotado na [Fase 6](../phase-6/acceptance-scenarios.md), conectando cada requisito da Fase 7 aos contratos das Fases 1 a 6:

- O prefixo **`Q`** identifica os cenários da Fase 7 (os prefixos `C`, `P`, `R`, `E`, `A`, `X`, `K` e `S` pertencem às fases anteriores).
- Nos cenários de adaptadores e cota (`Q01`–`Q14`), os testes automatizados de CI usam subprocessos simulados/fixtures das saídas reais das CLIs e relógio injetado (`DeterministicClock`), enquanto a sessão de verificação V01–V12 executa os binários reais instalados nas versões fixadas.
- Nos cenários de memória por família (`Q15`–`Q27`), os testes de isolamento RLS rodam contra o container PostgreSQL configurável via Docker com a role restrita `orq_reader`, além de validar o fallback para o modo offline `bm25-v1`.

A role implementada pelo 7B chama-se **`orq_memory_reader`**. `orq_reader` nos enunciados abaixo é o nome conceitual da role restrita, não uma role adicional.

## Cobertura automatizada do 7E

Os caminhos abaixo são relativos a `tests/`. **Automatizado** significa que as
asserções cobrem os resultados indicados, usando executores simulados ou CLIs
Python falsas; **Postgres** identifica testes reais de banco, pulados sem DSN e
exigidos no CI. Isso não declara os testes de banco executados localmente.

| ID | Cobertura | Testes / evidência e limites |
| --- | --- | --- |
| Q01 | Automatizado | `test_cli_adapters.py::test_protocol_collect_closed_stdin_and_cost` (quatro CLIs falsas) e `test_spawn_without_shell_and_state_fsynced_before_spawn`: EOF, argv sem shell e estado anterior ao spawn. |
| Q02 | Automatizado | `test_phase7_contracts.py` (credenciais/web proibidos) e `test_phase7_scenarios.py::test_Q02_invalid_profile_never_reaches_executor`: erro `subscription_policy_violation` antes da fábrica do executor. |
| Q03 | Automatizado | `test_phase7_scenarios.py::test_Q03_unknown_control_blocks_real_priority` e `test_Q03_unknown_control_excludes_top_priority_and_keeps_order`: `control_unknown`, exclusão da prioridade máxima, decisão sem destino e zero despachos. |
| Q04 | Automatizado | `test_phase7_boundaries.py::test_Q04_agy_permission_error_filters_noise_and_never_completes` e `test_phase7_scenarios.py::test_Q04_agy_collect_error_finishes_attempt_failed`: diagnóstico com os dois ruídos, código específico e tentativa failed. |
| Q05 | Automatizado | `test_cli_adapters.py::test_exact_argv` e `test_command_grants_are_explicit_and_injection_rejected`: sem `execute_bash` quando comandos não são autorizados; lista fechada e nenhuma concessão global. |
| Q06 | Automatizado | `test_cli_adapters.py::test_timeout_and_cancel_kill_tree_even_without_observe` (quatro CLIs falsas/filhos) mais S27 em `test_runner_scenarios.py`: morte/ausência da árvore, certeza e cancelamento pelo runner. |
| Q07 | Automatizado | `test_phase7_scenarios.py::test_Q07_Q08_Q13_Q26_quota_fallback_and_effective_context`: mensagem real, detector 7C, UTC 01:05, evento de bloqueio, erro retryable e tentativa failed. |
| Q08 | Automatizado | Mesmo teste: candidato Codex inelegível com evidência, `quota_fallback`/`used_fallback`, retry e despacho Claude. |
| Q09 | Automatizado | `test_phase7_scenarios.py::test_Q09_explicit_reset_restores_codex_before_new_task`: ausência de reset prematuro antes de `01:05Z`, reset `clock_expired` após `01:06Z`, evento anterior à nova decisão, Codex elegível sem fallback e `T2` concluída em `target-codex`. |
| Q10 | Automatizado | `test_Q10_cooldown_and_family_fallback` e `test_cooldown_requires_durable_probe_and_reblocks_when_refused`: reset nulo, 60 minutos, fallback; prova positiva, renovação e resultado inconclusivo de sondagem. |
| Q11 | Automatizado | `test_phase7_boundaries.py::test_Q11_project_literal_and_valid_final_do_not_exhaust_quota` (quatro adaptadores) e `test_phase7_scenarios.py::test_Q11_runner_completes_attempt_when_tool_reads_quota_literal`: literal em saída de ferramenta nas 4 CLIs, final válido, tentativa `completed`, sem erros/cota e `quota_outcome: accepted`. `test_quota_detector.py` também confere a proteção de código 0. |
| Q12 | Automatizado | `test_phase7_scenarios.py::test_Q12_all_four_pools_blocked_no_fifth_dispatch`: quatro recusas/pools distintos no runner; quinta admissão rejeitada com motivo exato e nenhum quinto despacho. |
| Q13 | Automatizado | Teste ponta a ponta Q07/Q08/Q13/Q26: consumo ligado ao uso, reservas/uso unavailable/null e resumo inexato. `test_cli_adapters.py::test_protocol_collect_closed_stdin_and_cost`: tokens reportados preservados, estimativa financeira ignorada e multiplicador ausente mantido null. |
| Q14 | Automatizado | `test_phase7_contracts.py::test_q14_subscription_zero_is_rejected_with_specific_code`: `0` e `0.00` rejeitados em uso/reserva com `invalid_subscription_cost_zero`. |
| Q15 | Postgres | `test_memory_store_postgres.py::test_codex_sees_common_and_own_units_only`: SELECT sem filtro de família, conjunto exato e zero unidades Claude. |
| Q16 | Postgres | `test_memory_store_postgres.py::test_family_and_role_restriction_requires_both_axes`: backend_engineer não vê a unidade; security_reviewer da família correta vê. |
| Q17 | Postgres | `test_knowledge_search_postgres.py::test_sql_injection_in_query_has_no_effect` e `test_scope_cannot_be_changed_by_the_search_arguments`: query hostil literal e contexto de transação preservado. |
| Q18 | Postgres | `test_phase7_postgres.py::test_Q18_sync_db_cli_twice_has_exact_vault_digests`: invoca a CLI duas vezes, fontes operacionais/de projeto, inserted/unchanged, contagem exata e digests/ordinais do Vault. Paridade/idempotência também no 7B. |
| Q19 | Automatizado | `test_phase7_boundaries.py::test_Q19_catalog_audience_cannot_be_changed_by_frontmatter`: campos de área/família/papel/audiência recebem `forbidden_frontmatter_field`. |
| Q20 | Postgres | `test_knowledge_search_postgres.py::test_mixed_query_puts_the_stem_and_flag_unit_on_top`, `test_fts_matches_portuguese_inflections` e `test_trigram_matches_cli_flag`: ranking real, sem substituir Postgres por BM25. |
| Q21 | Postgres + local | `test_knowledge_search_postgres.py::test_order_is_deterministic_and_follows_the_tie_break` e `test_knowledge_choice.py::test_ties_break_by_source_revision_ordinal_and_unit`: chave completa de desempate. |
| Q22 | Automatizado | `test_family_mcp.py::test_Q22_offline_gemini_scope_and_record`: Markdown real, área comum + Gemini, exclusões, `offline_markdown_bm25` e `bm25-v1` no registro. Fábrica forçada sem DSN também no 7D. |
| Q23 | Automatizado | `test_family_mcp.py::test_Q23_runner_scope_cannot_be_overridden`: servidor da tentativa Kiro, argumento estranho recusado/auditado, escopo intacto e resources filtrados. Smoke `test_scoped_stdio_server_uses_host_config` verifica também o transporte de processo. |
| Q24 | Automatizado | `test_family_mcp.py::test_Q24_six_candidates_agent_chooses_two_and_runner_seals`: cliente MCP oficial, seis candidatos, escolha de dois, cobertura, JCS e todos os digests; runner lacra exatamente a entrega. |
| Q25 | Automatizado | `test_family_mcp.py::test_Q25_tool_refuses_missing_coverage_and_over_budget` cobre os dois erros exatos, sem seleção/manifesto inválido nem corte silencioso. `test_phase7_scenarios.py::test_Q25_invalid_choice_cannot_complete_attempt` comprova `failed`. Alinhado pelo dono à Decisão 4 da ADR 0008 (escolha durante a execução): o despacho inicial usa contexto inicial válido e a selagem/gate impede entrega inválida e `completed`. |
| Q26 | Automatizado + Postgres | Teste ponta a ponta Q07/Q08/Q13/Q26 conserva cm-1 e gera cm-2, sem exclusividade Codex em Claude. `test_phase7_postgres.py::test_Q26_runner_fallback_rematerializes_under_real_rls` repete o fluxo completo sob RLS real. |
| Q27 | Automatizado + Postgres | `test_phase7_scenarios.py::test_Q27_changed_vault_fails_integrity_before_any_attempt` e `test_phase7_postgres.py::test_Q27_database_mutation_blocks_runner_before_dispatch`: check real anterior ao despacho, `integrity_mismatch`, zero tentativas. |

Regressões adicionais: cortes após intenção, bloqueio, consumo, selagem e intenção/
resultado de sondagem; nenhuma intenção existente é redespachada. Auditoria
adulterada/interrompida, reinício do servidor, prioridades do dono, revisão por
família diferente, fallback `fixed`, hub e indisponibilidade sem cota também têm
testes. A Decisão 4 da ADR 0008 prevalece para o momento de seleção de Q25.

## 1. Adaptadores de assinatura e política de uso (Q01–Q06)

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| Q01 | Qualquer um dos quatro adaptadores (`claude`, `codex`, `agy`, `kiro-cli`) configurado para despacho | `dispatch()` inicia a execução da tentativa | O processo é criado sem shell (`shell=False`), em modo não interativo e com `stdin` fechado; nunca aguarda entrada do terminal | V02, C14 |
| Q02 | Configuração ou código tentando fornecer token OAuth extraído, cookie de sessão, chave de API paga ou URL de automação web | O validador do perfil/adaptador é executado antes da admissão | A configuração é rejeitada com `subscription_policy_violation`; nenhuma chamada é despachada | ADR 0008, E01 |
| Q03 | Destino de CLI real com qualquer controle obrigatório da tarefa ainda em `support: "unknown"` (sem evidência V01–V12 para a versão instalada) | O Router filtra candidatos para a tarefa | O destino é excluído com `control_unknown`; nunca é selecionado mesmo com prioridade alta | P03, V01–V12 |
| Q04 | Tentativa no adaptador `agy` em modo headless (`-p`) na qual uma ferramenta exigida não possui regra em `permissions.allow`, produzindo `no output produced` e ruído `logging before google.Init` no `stderr` | `collect()` processa a saída da execução | O ruído `google.Init` é filtrado; a ausência de saída útil gera `ErrorRecord` `permission_or_empty_output` e tentativa `failed`, nunca `completed` | V03, V06, S20 |
| Q05 | Tarefa sem permissão de execução de comandos (`CommandPolicy` vazia) despachada para o adaptador do Kiro CLI | O adaptador monta os argumentos de invocação | A invocação inclui `--no-interactive` e `--trust-tools=fs_read,fs_write` (ou `fs_read`), omitindo `execute_bash` e recusando `--trust-all-tools` | E02, V06 |
| Q06 | Subprocesso de CLI real ultrapassa `attempt_timeout_ms` sem encerrar sozinho | `observe()` detecta o vencimento do prazo e o runner aciona `cancel()` | O host encerra toda a árvore de processos (incluindo processos filhos), verifica a ausência de processos órfãos locais e registra o término conforme a certeza observada | V07, V08, S27 |

## 2. Roteamento ciente de cota e custo `unavailable` (Q07–Q14)

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| Q07 | Tentativa 1 em `family_codex` falha com a saída real `"You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/plus) or try again at 10:05 PM."` às `20:15` locais (`America/Sao_Paulo`) | `collect()` e o runner processam o resultado | `QuotaLimitDetector` extrai o limite; o evento `quota_exhausted_recorded` marca `sub-chatgpt-plus` em `quota_exhausted` até `22:05` locais convertido para UTC (`reset_at`); tentativa 1 termina `failed` (`quota_limit_reached`, `retryable`) | ADR 0008, C18 |
| Q08 | Estado após Q07 (`family_codex` em `quota_exhausted` até `22:05` e relógio atual em `20:16`); `family_claude` está ativa e elegível | O runner executa a admissão da tentativa 2 da mesma tarefa | O Router exclui `family_codex` com motivo `quota_exhausted` e seleciona `family_claude` com `used_fallback: true` (`quota_fallback`); a tentativa 2 é despachada em `family_claude` | R06, S17 |
| Q09 | `family_codex` bloqueada até `reset_at = 2026-11-15T01:05:00Z`; o relógio do runner avança para `2026-11-15T01:06:00Z` antes de uma nova tarefa | O runner avalia a disponibilidade das cotas antes do roteamento | O evento `quota_reset_observed` restaura o pool `sub-chatgpt-plus` para `available`; `family_codex` volta a ser elegível na nova `RoutingDecision` | ADR 0008 |
| Q10 | Uma CLI encerra com mensagem de limite de uso sem informar horário de reset (ex.: `"quota exceeded for current window"`) | O detector processa a saída | `reset_at` fica `null`, o status do pool passa para `cooldown` com `blocked_until = observed_at + default_cooldown_ms` e `reset_source: "cooldown_policy"`, acionando o mesmo fallback para outra família | ADR 0008 |
| Q11 | Um arquivo de código ou teste lido pelo agente contém a string literal `"You've hit your usage limit"`, mas a CLI conclui com código `0` e entrega os artefatos válidos | `collect()` analisa o término da tentativa | O detector não dispara falso positivo; a tentativa termina `completed` normalmente e a cota permanece `available` | ADR 0008 |
| Q12 | Todas as quatro famílias (`claude`, `codex`, `gemini`, `kiro`) estão com seus pools em `quota_exhausted` ou `cooldown` no instante `now` | O runner tenta admitir a próxima tarefa | A `RoutingDecision` termina com `decision_status: "no_eligible_target"` (`all_eligible_families_quota_exhausted`); `admission_rejected` é gravado sem consumir `dispatch_requested` | S07, R10 |
| Q13 | Uma tentativa em assinatura pessoal conclui com sucesso (ou falha por cota), e a CLI imprime tokens consumidos e/ou uma estimativa local em USD (como no Claude Code) | O runner grava `UsageRecord`, `QuotaConsumptionRecord` e fecha o `RunSummary` | `QuotaConsumptionRecord` registra mensagens, tokens e multiplicador de créditos; `UsageRecord.cost` mantém obrigatoriamente `status: "unavailable"` e `amount: null`; `RunSummary` incrementa `unavailable_count` e mantém `total_cost_exact: false` | C27, S41 |
| Q14 | Um adaptador ou fixture tenta registrar `UsageRecord.cost` de uma assinatura com `amount: "0"` ou `"0.00"` | O validador estrutural/semântico executa | A validação rejeita o registro (`invalid_subscription_cost_zero`); custo de assinatura nunca é zero | E10, S46 |

## 3. Memória por família, Postgres e Row-Level Security (Q15–Q22)

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| Q15 | Banco Postgres populado com unidades `common`, unidades exclusivas de `family_codex` e unidades exclusivas de `family_claude`; transação aberta como `orq_reader` com `app.current_family_id = 'family_codex'` | Uma consulta `SELECT` sem cláusula `WHERE` de família é executada na tabela `knowledge_units` | O Row-Level Security retorna somente as unidades `common` e as unidades de `family_codex`; zero linhas exclusivas de `family_claude` são visíveis | ADR 0008, K03 |
| Q16 | Unidade restrita simultaneamente a `target_families = ['family_claude']` e `target_roles = ['security_reviewer']` | Duas sessões consultam o banco: uma como `('family_claude', 'backend_engineer')` e outra como `('family_claude', 'security_reviewer')` | A primeira sessão não vê a unidade; apenas a segunda sessão (`family_claude` + `security_reviewer`) enxerga a unidade pelo RLS | ADR 0008, P01 |
| Q17 | Conteúdo da query de busca contém tentativa de SQL injection ou comando `SET LOCAL app.current_family_id = 'family_claude'` | A busca parametrizada é executada pela role `orq_reader` | A entrada é tratada exclusivamente como literal de busca em `websearch_to_tsquery` e `word_similarity`; variáveis de sessão e política RLS permanecem inalteradas | K15, E06 |
| Q18 | Vault Markdown contendo fontes operacionais e de projeto, com catálogo autorizado definindo `area_scope` e `target_families` | O comando de sincronização `vault sync-db` é executado duas vezes sobre o mesmo snapshot | Todas as unidades são gravadas no Postgres com os mesmos `source_digest`, `content_digest` e `ordinal` calculados na Fase 2, sem duplicação na segunda execução | K05, ADR 0002 |
| Q19 | Arquivo Markdown tenta declarar `area_scope: common` ou `target_families: ["family_claude"]` diretamente no próprio frontmatter, contrariando a `CatalogPolicy` | A ingestão/sincronização roda | O documento é rejeitado com erro de campo sensível no frontmatter (`forbidden_frontmatter_field`) | K01, ADR 0002 |
| Q20 | Unidade em português contendo `"reconciliação de tentativas expiradas"` e a flag técnica `"--trust-tools=fs_read"` | A busca híbrida (`tsvector` português + `pg_trgm`) é chamada com a query `"reconciliar tentativa --trust-tools"` | O FTS em português faz o match morfológico de `"reconciliar tentativa"` e o `pg_trgm` faz o match da flag `"--trust-tools"`, posicionando a unidade no topo do ranking | ADR 0008 |
| Q21 | Dois candidatos empatam na pontuação combinada `pg-fts-trgm-v1` | O seletor ordena os resultados da busca | O desempate é estritamente determinístico por `(score DESC, source_id ASC, revision ASC, ordinal ASC, unit_id ASC)` | ADR 0002 |
| Q22 | Container Docker do Postgres desligado e execução configurada com `ORQ_KNOWLEDGE_BACKEND=offline_markdown_bm25` | O seletor de conhecimento é executado para `family_gemini` | O ORQ opera em modo offline sobre os arquivos Markdown usando `bm25-v1`, filtra em memória apenas `common` + `family_gemini` e grava `backend: "offline_markdown_bm25"` no `KnowledgeSelectionRecord` | ADR 0002, ADR 0008 |

## 4. MCP escopado por família, escolha pelo agente e `ContextManifest` (Q23–Q27)

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| Q23 | Servidor `orq-knowledge-mcp` iniciado pelo runner para uma tentativa de `family_kiro` | O agente invoca `search_family_knowledge` passando argumentos extras tentando forçar `family_id: "family_claude"` | O schema da tool rejeita o argumento estranho (ou o servidor ignora qualquer escopo externo e mantém o `SET LOCAL` cravado pelo host em `family_kiro`) | ADR 0004, ADR 0008 |
| Q24 | `search_family_knowledge` devolve 6 candidatos elegíveis para um requisito obrigatório da tarefa | O agente invoca `select_family_knowledge` escolhendo 2 unidades que cobrem o requisito e cabem no orçamento | O servidor gera `KnowledgeSelectionRecord` com `selection_mode: "agent_mcp_choice"`, consolida o `ContextManifest` e o bundle JCS e verifica todos os digests SHA-256 | ADR 0003, K11 |
| Q25 | O agente chama `select_family_knowledge` omitindo todas as unidades de um `KnowledgeRequirement` marcado como `required: true` (ou excedendo `available_input_tokens`) | O servidor MCP valida a escolha do agente durante a execução (Decisão 4 da ADR 0008) | A seleção não é aceita como `satisfied`; retorna erro estruturado (`mandatory_requirement_uncovered` ou `context_budget_exceeded`), impedindo entrega inválida na selagem e conclusão `completed` da tentativa | R07, K08, K09 |
| Q26 | Tentativa 1 em `family_codex` falha por limite de cota após ter materializado `ContextManifest` `cm-1` (com unidades de `family_codex`); tentativa 2 faz fallback para `family_claude` | O runner prepara o contexto da tentativa 2 | `cm-1` é preservado no histórico da tentativa 1, mas **não** é reutilizado; a tentativa 2 executa nova busca/seleção sob o RLS de `family_claude` e gera um novo `ContextManifest` `cm-2` | R09, K14 |
| Q27 | Os bytes de uma unidade no banco ou arquivo mudam após o snapshot do catálogo antes da materialização do `ContextManifest` | O check `context_integrity` roda antes do despacho | A verificação de digest falha (`integrity_mismatch`), o despacho é bloqueado e nenhuma tentativa é consumida | S11, K13 |

## 5. Experimento científico 7F (Q28–Q30)

| ID | Dado | Quando | Então | Base |
| --- | --- | --- | --- | --- |
| Q28 | Mesma suíte de tarefas executada nos regimes `shared_global` (sem filtro de família) e `family_partitioned_rls` (com RLS por família + área comum), mantendo `WorkflowSpec` e `DeterministicEvaluator` idênticos | O harness experimental do incremento `7F` executa os dois braços | Cada run registra em `experiment_metadata` o regime de memória, produz `ContextManifest` e `EvaluationReport` auditáveis por digest e permite comparar tokens de entrada, cobertura e taxa de aprovação (`pass`) | Fase 1, ADR 0008 |
| Q29 | Cenário experimental de estresse de cota em que a família primária atinge o limite no meio do workflow | O braço `quota_aware_multi_family` é comparado ao braço `single_family_no_fallback` | O braço sem fallback interrompe o run na falha de cota; o braço ciente de cota registra `quota_exhausted_recorded`, redireciona para a família substituta com novo `ContextManifest` e conclui o workflow | Q07, Q08, Q26 |
| Q30 | Auditoria final do pacote experimental `7F` | O verificador de reprodutibilidade inspeciona todos os journals, manifestos e configurações dos runs | Confirma ausência total de chamadas a APIs pagas, ausência de modelos de embeddings, zero custos monetários convertidos em `"0"` e integridade completa das cadeias de `event_hash` | ADR 0001, ADR 0008 |
