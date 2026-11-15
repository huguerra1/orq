# Experimento 7F — Memória compartilhada × particionada por família e roteamento ciente de cota

Estado: aceita em 2026-11-14 ([ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md)). Especifica o desenho experimental do incremento `7F`, visando a produção de um artigo científico empírico sobre orquestração multiagente sob assinaturas pessoais.

## Motivação e lacuna científica

Estudos recentes sobre sistemas multiagentes baseados em LLMs costumam pressupor duas condições que raramente se sustentam no desenvolvimento prático individual ou em pequenas equipes:

1. **Acesso ilimitado a APIs tarifadas por token**, tratando limites operacionais apenas como orçamento financeiro linear, sem modelar janelas discretas de bloqueio e reset típicas de assinaturas de ferramentas de codificação (Claude Code, Codex CLI, Gemini CLI/`agy` e Kiro CLI).
2. **Base de conhecimento (RAG) monolítica e compartilhada**, na qual todos os agentes — independentemente do runtime/CLI e do papel — consultam o mesmo espaço global de documentos. Quando múltiplas famílias de agentes coexistem, instruções operacionais específicas de um ambiente (por exemplo, contornos de sandbox do Codex no Windows, sintaxe de permissões do `agy` ou restrições de ferramentas do Kiro) entram como ruído no contexto de outra família, inflando o consumo de tokens e podendo degradar a taxa de acerto.

O incremento `7F` investiga empiricamente o efeito conjunto do **particionamento de memória por família de agentes via Row-Level Security (RLS)** (combinado com busca textual em português + `pg_trgm` e escolha final pelo agente) e do **roteamento ciente de cota com fallback entre famílias**, mantendo avaliação determinística estrita e rastreabilidade criptográfica por digests (RFC 8785/JCS + SHA-256).

## Questões de pesquisa e hipóteses

### RQ1 — Memória compartilhada vs. memória particionada por família

> *Qual é o impacto de particionar a base de conhecimento em área comum + área exclusiva por família/papel (isolada por RLS no PostgreSQL), em comparação a uma memória global compartilhada, sobre o consumo de contexto, a precisão da seleção e a taxa de aprovação na primeira tentativa (`pass@1`)?*

- **Hipótese nula ($H_{1,0}$):** não há diferença estatisticamente significativa na taxa de aprovação determinística (`pass@1`) nem na quantidade de tokens de contexto materializados (`materialized_input_tokens`) entre o regime de memória global compartilhada (`shared_global`) e o regime particionado por família com RLS (`family_partitioned_rls`).
- **Hipótese alternativa ($H_{1,1}$):** o regime `family_partitioned_rls` reduz significativamente `materialized_input_tokens` e a inclusão de unidades distratoras de outras famílias (taxa de contaminação cruzada igual a zero por construção do RLS), mantendo ou elevando `pass@1` nas tarefas avaliadas pelo `DeterministicEvaluator`.

### RQ2 — Seleção estática pelo host vs. escolha final pelo agente via MCP

> *Permitir que o próprio agente faça a escolha final das unidades via ferramenta MCP escopada (sobre candidatos filtrados por RLS e ranqueados por FTS português + `pg_trgm`) melhora a cobertura relevante em comparação ao corte estático top-$k$ puramente lexical?*

- **Hipótese nula ($H_{2,0}$):** a precisão e o F1 de seleção em relação ao gabarito de unidades relevantes da tarefa são equivalentes entre a seleção estática (`static_top_k`) e a seleção final pelo agente (`agent_mcp_choice`).
- **Hipótese alternativa ($H_{2,1}$):** `agent_mcp_choice` apresenta maior precisão (menos unidades irrelevantes incluídas no `ContextManifest`) para o mesmo teto de tokens, sem violar a cobertura obrigatória.

### RQ3 — Roteamento ciente de cota e continuidade sob exaustão de janela

> *Qual é a eficácia e a sobrecarga (tempo, tentativas e re-materialização de contexto) do roteamento ciente de cota com fallback entre assinaturas quando uma família primária atinge seu limite de uso?*

- **Hipótese nula ($H_{3,0}$):** sob eventos de limite de uso, o roteamento ciente de cota (`quota_aware_fallback`) não altera a taxa de conclusão de workflows (`run succeeded`) nem reduz tentativas desperdiçadas em relação ao roteamento fixo na mesma família (`single_family_retry`).
- **Hipótese alternativa ($H_{3,1}$):** `quota_aware_fallback` evita 100% dos despachos repetidos contra assinaturas bloqueadas antes de `reset_at`, preserva a conclusão do workflow migrando para outra família elegível e garante zero vazamento de unidades exclusivas da família original no novo `ContextManifest`.

## Fatores experimentais (variáveis independentes)

O experimento adota um desenho fatorial controlado sobre os seguintes fatores:

| Fator | Níveis | Descrição |
| --- | --- | --- |
| **Regime de memória** | `shared_global` | Todas as unidades ativas do projeto e operacionais ficam visíveis a qualquer família (política RLS relaxada para fins de controle experimental) |
| | `family_partitioned_rls` | RLS estrito ativo: cada tentativa enxerga apenas `area_scope = 'common'` + unidades compatíveis com seu `(family_id, role_id)` |
| | `offline_markdown_bm25` | Baseline offline da Fase 2 sobre arquivos Markdown com filtro de família em memória |
| **Modo de seleção** | `static_top_k` | O seletor do host preenche o orçamento automaticamente pela ordem `pg-fts-trgm-v1` / `bm25-v1` |
| | `agent_mcp_choice` | O agente consulta `search_family_knowledge` e confirma a escolha final em `select_family_knowledge` |
| **Regime de cota/roteamento** | `single_family_no_fallback` | Tarefa restrita a uma única família; esgotamento de cota causa falha das tentativas subsequentes antes do reset |
| | `quota_aware_multi_family` | Detecção de limite na saída da CLI, bloqueio até `reset_at`/cooldown e fallback ordenado entre `claude`, `codex`, `gemini` e `kiro` |

## Métricas (variáveis dependentes)

Todas as métricas são extraídas deterministicamente dos artefatos imutáveis do run (`RunJournal`, `RoutingDecision`, `KnowledgeSelectionRecord`, `ContextManifest`, `UsageRecord`, `QuotaConsumptionRecord` e `EvaluationReport`):

| Categoria | Métrica | Fonte exata no ORQ | Unidade / Tipo |
| --- | --- | --- | --- |
| Eficácia técnica | `pass_at_1` e `run_success_rate` | `EvaluationReport.verdict == "pass"` e `RunManifest`/`run_closed` | Proporção $[0, 1]$ |
| Qualidade de contexto | `selection_precision`, `selection_recall`, `cross_family_contamination_rate` | `ContextManifest.items` comparado ao gabarito da tarefa e aos metadados `target_families` | Proporção $[0, 1]$ |
| Eficiência de contexto | `materialized_input_tokens` | `ContextManifest.budget.materialized_input_tokens` | Inteiro (tokens estimados `char4-v1` e tokens reportados pela CLI) |
| Resiliência de cota | `wasted_dispatches_on_exhausted_quota` | Contagem de `dispatch_requested` para pools já marcados `quota_exhausted` | Inteiro |
| Sobrecarga de fallback | `fallback_rematerialization_latency_ms` e `attempts_per_task` | Diferença de timestamps no `RunJournal` entre `quota_exhausted_recorded` e o `dispatch_requested` substituto | Milissegundos / Inteiro |
| Consumo de cota | `messages_consumed`, `reported_tokens`, `credit_multiplier` | `QuotaConsumptionRecord` | Inteiro / Decimal |
| Honestidade financeira | `unavailable_cost_integrity` | Verificação de que 100% dos `UsageRecord.cost` de assinaturas têm `status: "unavailable"` e `amount: null` | Booleano (invariante = `true`) |

## Desenho experimental reproduzível

**Configuração inicial aprovada pelo dono (2026-11-14):** 10 tarefas × 3 repetições × 2 condições (memória compartilhada × memória por família), totalizando 60 runs, distribuídos em alguns dias para caber nas janelas de cota das assinaturas sem atrapalhar o uso diário. A condição de roteamento ciente de cota é observada nesses mesmos runs quando um limite aparece; ela não ganha runs próprios nesta configuração inicial.

### 1. Corpus de conhecimento e distratores controlados

O catálogo do experimento conterá um snapshot fixo (`catalog_hash` cravado) composto por:

- **Unidades comuns (`common`):** especificações de contratos do ORQ, regras de validação JSON Schema Draft 2020-12, canonicalização RFC 8785 (JCS) e invariantes de negócio.
- **Unidades específicas por família (`family`):** instruções reais de operação e contorno de ambiente para `family_claude`, `family_codex`, `family_gemini` e `family_kiro`, além de guias por papel (`software_architect`, `backend_engineer`, `tester`, `code_reviewer`, `security_reviewer`).
- **Pares contrastivos (distratores realistas):** tópicos em que duas famílias possuem regras opostas ou flags incompatíveis (por exemplo, flags de execução de testes no sandbox Windows do Codex versus permissões do `agy` ou `--trust-tools` do Kiro). Isso permite medir diretamente se a exposição a instruções de outra família no regime `shared_global` induz erro de invocação ou contaminação de resposta.

### 2. Benchmark de tarefas e avaliação determinística

- Serão definidas tarefas estruturadas cobrindo quatro papéis (`software_architect`, `backend_engineer`, `tester`, `security_reviewer`), organizadas em workflows sequenciais reproduzíveis.
- Cada tarefa possui gabarito fechado de unidades de conhecimento esperadas e critérios de aceitação verificados exclusivamente pelo `DeterministicEvaluator` (`output_binding_present`, `digest_matches`, `content_contract_valid` e testes determinísticos sobre os artefatos gerados). Nenhum juiz baseado em LLM ("LLM-as-a-judge") será usado na métrica primária para não introduzir viés de família nem consumir cota extra. Qualquer auditoria qualitativa por modelo/rubrica prevista na proposta da Fase 8 ([ADR 0009](../adr/0009-phase-8-model-and-rubric-evaluator.md)) sobre os artefatos já encerrados do `7F` é opcional, posterior (executada fora dos runs do `7F` apenas se o dono desejar) e não altera a métrica primária determinística (`pass_at_1` e `run_success_rate`).

### 3. Protocolo de execução em duas camadas

Para respeitar os limites das assinaturas pessoais do autor e garantir reprodutibilidade:

1. **Camada A — Simulação determinística exaustiva (sem consumo de assinatura):** usa o `SimulatedExecutor` combinado com o banco PostgreSQL real (RLS + FTS português + `pg_trgm`) e fixtures reais de mensagens de erro/cota das quatro CLIs (incluindo `"You've hit your usage limit ... try again at 10:05 PM"`). Permite validar em larga escala e com semente fixa todas as transições de cota, fallback e isolamento RLS.
2. **Camada B — Execução real pareada nas quatro CLIs de assinatura:** executa o conjunto de workflows nos quatro adaptadores reais (`claude`, `codex`, `agy`, `kiro-cli`) em blocos pareados (`shared_global` vs. `family_partitioned_rls`), alternando a ordem de execução (counterbalancing) para neutralizar variações de latência dos provedores ao longo do dia.

### 4. Pacote de reprodutibilidade e análise estatística

- Todos os runs experimentais preservam o diretório `runs/<run_id>/` com `journal.jsonl`, artefatos endereçados por SHA-256, `ContextManifest`, `KnowledgeSelectionRecord` e snapshots do catálogo.
- Para comparações pareadas por tarefa entre `shared_global` e `family_partitioned_rls`, serão reportados: mediana e intervalo interquartil (IQR) de tokens e latência, teste de postos sinalizados de Wilcoxon para variáveis contínuas, teste exato de McNemar para proporções binárias (`pass`/`fail`) e tamanho de efeito (delta de Cliff / odds ratio).

## Benchmark 7F

Pacote de dados em [`experiments/7f/`](../../experiments/7f/README.md), validado por `tests/test_experiment_7f_benchmark.py` (sem executar CLIs nem usar rede). Contém 50 unidades de conhecimento (12 comuns, 4 por papel, 12 por família, 6 por família e papel, 16 distratores sintéticos fictícios), o snapshot do catálogo com `catalog_hash` fixado, 10 tarefas e o `manifest.json` com digests e o plano de lotes.

Cada tarefa pede uma resposta JSON curta, com contrato de saída JSON Schema (sem o gabarito), classificação fechada das unidades de conhecimento e checagens determinísticas (`equals`, `set_equals`, `contains_all`, `matches`, `no_forbidden_text`). O `pass` do run exige veredito `pass` do `DeterministicEvaluator` (três critérios) e todas as checagens. Cada tarefa tem uma tentativa (`max_attempts: 1`).

| # | Tarefa | Papel | Família-alvo | Tema do gabarito |
| --- | --- | --- | --- | --- |
| 1 | `exp7f-t01-architect-claude-tools` | `software_architect` | `family_claude` | `--disallowedTools`, ferramentas negadas e `total_cost_usd` |
| 2 | `exp7f-t02-architect-codex-readonly` | `software_architect` | `family_codex` | `-s read-only`, `-o`, `--json` e cobertura `platform_only` |
| 3 | `exp7f-t03-architect-gemini-workdir` | `software_architect` | `family_gemini` | `cwd` no lugar de `-C`, `--model`, `--mode plan` e unidade de comparação |
| 4 | `exp7f-t04-backend-codex-windows-tests` | `backend_engineer` | `family_codex` | `spawn EPERM`, `--pool threads`/`--runInBand` e `--skip-git-repo-check` |
| 5 | `exp7f-t05-backend-gemini-headless` | `backend_engineer` | `family_gemini` | `no output produced`, `permissions.allow` e `permission_or_empty_output` |
| 6 | `exp7f-t06-backend-kiro-wsl` | `backend_engineer` | `family_kiro` | `wsl.exe`, `/mnt/c/`, `--trust-tools=fs_read,fs_write` |
| 7 | `exp7f-t07-tester-claude-digest` | `tester` | `family_claude` | digest JCS: `^[a-f0-9]{64}$`, faixa segura e números rejeitados |
| 8 | `exp7f-t08-tester-kiro-quota` | `tester` | `family_kiro` | cooldown de 3600000 ms, `cooldown_policy` e `no_eligible_target` |
| 9 | `exp7f-t09-security-claude-policy` | `security_reviewer` | `family_claude` | política de assinaturas, variáveis e diretórios proibidos, `shell=False` |
| 10 | `exp7f-t10-security-kiro-trust-tools` | `security_reviewer` | `family_kiro` | `--trust-all-tools` proibida, `--trust-tools=fs_read` e alcance de `execute_bash` |

**Classes de conhecimento por tarefa.** Toda unidade elegível aos `knowledge_requirements` da tarefa no regime `shared_global` cai em exatamente uma classe: `gold` (esperadas, visíveis no particionado), `acceptable_alternatives` (conteúdo correto de outra família ou papel; escolher é neutro), `distractors` (conteúdo inadequado ou contraditório, inclusive todas as regras sintéticas fictícias) ou `irrelevant` (elegível por tópico, sem resposta nem conflito). Unidades sintéticas nunca ficam visíveis à própria família da tarefa. `answer_sources` declara, por checagem de resposta, as fontes que a sustentam, e nenhum distrator pode constar nela. Definições para a análise:

- `selection_precision` = |selecionadas ∩ gold| / |selecionadas − alternativas|; `selection_recall` = |selecionadas ∩ gold| / |gold|.
- `distractor_contamination_rate` = |selecionadas ∩ distratores| / |selecionadas|; é a contaminação por conteúdo, e vale zero no particionado por construção.
- `cross_family_exposure_rate` (descritiva) = fração das selecionadas cuja audiência de família não inclui a família do run, contando também alternativas.

O plano de lotes tem 6 lotes de 10 runs (5 pares tarefa × repetição por lote, as duas condições em sequência na mesma janela). A atribuição é determinística (semente `20261114`): as três repetições de uma tarefa caem em dias distintos, a condição que roda primeiro é contrabalanceada por tarefa e por família-alvo (diferença máxima de 1 par; 15 de cada no total) e a carga por família-alvo é distribuída entre os lotes. O `package_digest` do manifesto cobre arquivos de dados, scripts de `tools/`, sementes e plano.

## Ameaças à validade e mitigações

| Tipo | Ameaça | Mitigação no desenho da Fase 7 |
| --- | --- | --- |
| **Validade de construto** | Atribuir diferenças de desempenho apenas ao modelo subjacente, ignorando que cada CLI tem ferramentas e prompt de sistema próprios | Seguir a decisão da Fase 1: a unidade de comparação declarada no artigo é a **família/runtime completo** (CLI + modelo + ferramentas), registrando `executor_ref`, `runtime_version`, `requested_model_id` e `resolved_model_id` |
| **Validade interna** | Atualização silenciosa de pesos do modelo no servidor do provedor ou variação de horário durante a coleta | Fixar a versão das CLIs (V01), executar os pares (`shared_global` × `family_partitioned_rls`) na mesma janela temporal em ordem balanceada e registrar timestamps UTC exatos |
| **Validade interna** | Vazamento residual de estado no workspace entre tentativas ou entre braços experimentais | Cada tentativa roda em diretório de workspace limpo e recém-materializado; o banco Postgres opera em transações `READ ONLY` sobre um snapshot imutável |
| **Validade externa** | Resultados obtidos sobre quatro assinaturas específicas e corpus em português/Python podem não generalizar para qualquer domínio | Documentar explicitamente o escopo no artigo e disponibilizar os schemas, o corpus anonimizado e os scripts de sincronização para replicação em outros idiomas e projetos |
| **Validade de conclusão** | Tamanho amostral da Camada B limitado pelas próprias cotas das assinaturas pessoais | Combinar a Camada A (alta potência estatística para invariantes de sistema, RLS e roteamento) com tarefas pareadas de alto poder discriminativo na Camada B, usando testes não paramétricos adequados a amostras moderadas |
