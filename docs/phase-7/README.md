# Fase 7 — Hub de assinaturas pessoais, roteamento por cota e memória por família

Estado: Proposta em 2026-11-14 (aguardando revisão do dono na [ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md); início somente após a conclusão do incremento 6E). Nenhum código, schema executável, container ou fixture da Fase 7 foi implementado.

## Problema

As Fases 1 a 6 definem os contratos da plataforma, o Vault Markdown local com busca lexical BM25, o servidor MCP somente leitura, o Planner, o Router e o runner sequencial com executor simulado (incrementos 6A a 6D implementados; 6E pendente para fechar a simulação ponta a ponta). Contudo, a plataforma ainda não executa tarefas em agentes reais nem particiona o conhecimento entre diferentes famílias de agentes.

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
| 6 | Journal JCS encadeado (`6B`), projeções e máquina de estados (`6C`), protocolo `ExecutorAdapter`, checklist V01–V12 e `DeterministicEvaluator` (`6D`), e runner sequencial ponta a ponta (`6E`) | `6A`–`6D` implementados; **`6E` é pré-requisito obrigatório antes de iniciar `7A`** |

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
