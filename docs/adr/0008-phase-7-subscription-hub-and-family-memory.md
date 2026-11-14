# ADR 0008 — Hub de assinaturas pessoais, roteamento por cota e memória por família em Postgres

Estado: Aceita em 2026-11-14 (proposta e aceite no mesmo dia; ver "Decisões do dono"). Implementação somente depois que o incremento 6E estiver na `main`.

Substitui parcialmente a [ADR 0002](0002-vault-layout-and-markdown-parsing.md) (somente no trecho em que excluía banco de dados e embeddings do projeto), mantendo o parser Markdown seguro e o ranking BM25 local (`bm25-v1`) como **modo offline**. Estende a fronteira de conhecimento da [ADR 0004](0004-mcp-knowledge-boundary.md) e o roteamento da [ADR 0006](0006-router-boundary-and-typed-decision.md).

## Contexto

As Fases 1 a 5 fecharam contratos, Vault Markdown local, servidor MCP somente leitura, Planner e Router. A Fase 6 especificou o runner sequencial e implementou os incrementos 6A a 6D (schemas, journal JCS encadeado, máquina de estados e `ExecutorAdapter` com executor simulado e avaliador determinístico), restando o incremento 6E para fechar a execução simulada ponta a ponta.

O roadmap original previa na Fase 7 a conexão com provedores reais. Na prática do projeto, o autor dispõe de quatro **assinaturas pessoais** ativas — **Kiro Power** (Kiro CLI), **ChatGPT Plus** (Codex CLI), **Claude Enterprise** (Claude Code) e **Gemini** (Gemini CLI/`agy`) — todas confirmadas pelo dono como permitidas para uso em projetos pessoais. Usar APIs cobradas por token criaria custo recorrente desnecessário, e exigir LLMs locais inviabilizaria a execução na máquina de trabalho ou reduziria a capacidade dos agentes.

Operar sobre assinaturas pessoais por meio de suas CLIs oficiais introduz três requisitos arquiteturais que não existiam no executor simulado:

1. **Conformidade estrita de acesso:** o ORQ só pode invocar os binários oficiais das CLIs em modo não interativo, aproveitando o login oficial mantido pelo próprio usuário. Extrair tokens de sessão/OAuth ou automatizar interfaces web violaria os termos de uso e as regras de segurança da plataforma.
2. **Limites de uso por janela (cota):** assinaturas não cobram por chamada em moeda, mas interrompem a execução quando a janela de uso se esgota (por exemplo, mensagens na saída como `"You've hit your usage limit ... try again at 10:05 PM"` no Codex CLI). O Orchestrator precisa detectar esse estado na saída da CLI, bloquear temporariamente o destino até o horário de reset, fazer fallback para outra família elegível e registrar o consumo de cota sem jamais converter o custo monetário desconhecido em zero.
3. **Memória direcionada por família de agentes:** diferentes famílias (definidas por modelo/assinatura e/ou por papel) precisam de conhecimentos operacionais e de projeto específicos (convenções de ferramentas, restrições de sandbox, padrões por linguagem/papel), além de uma área comum. Na [ADR 0002](0002-vault-layout-and-markdown-parsing.md), o MVP foi restrito a arquivos Markdown sem banco de dados e, na [ADR 0004](0004-mcp-knowledge-boundary.md), o MCP expunha apenas `resources` estáticos escolhidos antes da execução. Com múltiplas famílias reais, o isolamento no nível de armazenamento e a busca interativa pelo próprio agente dentro do seu escopo tornam-se necessários, além de permitirem investigar cientificamente o efeito de memória compartilhada versus particionada.

## Alternativas

| Dimensão | Alternativa | Vantagens | Limitações |
| --- | --- | --- | --- |
| Integração de provedores | SDKs e APIs pagas por token | Telemetria de custo direta e APIs HTTP uniformes | Gera gasto variável em dinheiro; ignora as quatro assinaturas pessoais já contratadas |
| Integração de provedores | Extrair tokens OAuth/sessão das CLIs ou automatizar interface web | Evita peculiaridades de flags das CLIs | Viola regras de segurança e termos de uso; frágil; proibido pelas diretrizes do projeto |
| Integração de provedores | **CLIs oficiais em modo não interativo sobre `ExecutorAdapter`** | Sem cobrança variável por chamada (o custo monetário por execução continua `unavailable`, nunca zero); usa autenticação oficial de cada CLI; mantém a fronteira de segurança do 6D | Exige parser tolerante a ruído, timeout imposto pelo host e verificação V01–V12 por versão |
| Tratamento de limite de cota | Tratar limite de uso como falha técnica comum (`retryable`) na mesma CLI | Nenhuma mudança no Router | Repete chamadas fadadas ao fracasso na mesma assinatura já esgotada |
| Tratamento de limite de cota | **Roteamento ciente de cota com bloqueio até o reset e fallback entre famílias** | Desvia imediatamente para outra assinatura elegível e reabilita o destino após o reset | Exige extrair o estado/horário da saída da CLI e re-materializar o contexto para a família substituta |
| Armazenamento e busca de memória | Manter apenas arquivos Markdown e BM25 em memória | Zero infraestrutura adicional | Não oferece isolamento declarativo no banco nem políticas nativas por sessão/família em escala |
| Armazenamento e busca de memória | Banco vetorial ou Postgres exigindo embeddings já no MVP da Fase 7 | Busca por similaridade densa desde o início | Geração de embeddings exige API paga ou modelo local, ambos vedados nesta fase |
| Armazenamento e busca de memória | **Postgres configurável via Docker com RLS + FTS português e `pg_trgm`, mantendo BM25 em Markdown como modo offline** | Isolamento forte por Row-Level Security; busca lexical + trigramas sem modelo externo; preserva execução offline e evolução futura para embeddings | Adiciona container Docker opcional e pipeline de sincronização a partir do Vault Markdown |
| Entrega de conhecimento via MCP | Manter somente `resources` estáticos pré-selecionados pelo host (Fase 3) | Controle total no host antes do despacho | O agente não consegue refinar a consulta nem decidir a escolha final diante da tarefa |
| Entrega de conhecimento via MCP | **Estender o MCP da Fase 3 com ferramenta de busca escopada pela família e escolha final pelo agente, consolidando `ContextManifest`** | Combina isolamento duro (RLS fixado pelo host) com escolha final pelo agente; preserva digests e auditoria | Exige registrar as unidades escolhidas pela tool antes de fechar o manifesto da tentativa |

## Decisão proposta

1. **Substituição parcial da ADR 0002 e preservação do modo offline:** esta ADR substitui a restrição da [ADR 0002](0002-vault-layout-and-markdown-parsing.md) que excluía banco de dados e embeddings do horizonte do projeto. O PostgreSQL configurável via Docker passa a ser o backend principal de memória/RAG particionada na Fase 7. O Vault em arquivos Markdown e o seletor determinístico `bm25-v1` da Fase 2 **permanecem ativos como modo offline** (`offline_markdown_bm25`) e como fonte editorial de ingestão. Embeddings continuam previstos apenas como evolução futura, pois dependem de um modelo de geração de vetores.
2. **Hub de assinaturas pessoais via CLIs oficiais:** os adaptadores reais da Fase 7 conectam exclusivamente **Claude Code** (Claude Enterprise), **Codex CLI** (ChatGPT Plus), **Gemini CLI / `agy`** (Gemini) e **Kiro CLI** (Kiro Power) em modo não interativo (`stdin` fechado), implementando o protocolo `ExecutorAdapter` definido no incremento 6D. É estritamente proibido extrair tokens, ler credenciais internas, automatizar navegadores/interfaces web, chamar APIs pagas ou depender de LLMs locais. Cada adaptador só declara controles `supported` após cumprir o checklist V01–V12 com versão fixada.
3. **Roteamento ciente de cota:** os adaptadores inspecionam `stdout`, `stderr` e códigos de saída para detectar bloqueios de limite de uso (como `"You've hit your usage limit ... try again at 10:05 PM"` no Codex CLI). Ao detectar esgotamento:
   - o destino (e os demais alvos que compartilham a mesma janela de assinatura/pool) é marcado como indisponível (`quota_exhausted`) até o instante `reset_at` extraído da mensagem ou, na ausência de horário explícito, por uma janela conservadora de cooldown;
   - o Orchestrator registra um evento e um registro de consumo/bloqueio de cota (`QuotaConsumptionRecord` / `QuotaStateRecord`);
   - o Router executa fallback determinístico para outra família de agentes elegível que cumpra todos os controles obrigatórios da tarefa;
   - o custo monetário no `UsageRecord` e nas reservas **continua `status: "unavailable"` com `amount: null`, nunca zero**, preservando os invariantes da Fase 1 e da Fase 6.
4. **Famílias de agentes e áreas de conhecimento:** o contrato introduz a entidade `AgentFamily`, definida pela combinação de assinatura/modelo (`claude`, `codex`, `gemini`, `kiro`) e/ou papel (`AgentRoleProfile`). Cada unidade de conhecimento é direcionada a uma audiência composta por:
   - **área comum (`common`):** acessível a qualquer família autorizada no projeto/escopo operacional;
   - **área por família (`family`):** restrita a uma ou mais famílias e/ou papéis explícitos.
5. **Isolamento por Row-Level Security (RLS) no PostgreSQL:** o banco aplica políticas RLS (`ENABLE` e `FORCE ROW LEVEL SECURITY`) baseadas em variáveis de contexto da transação (`app.current_family_id`, `app.current_role_id`, `app.current_project_id`, `app.catalog_id`) definidas pelo host em conexão sem privilégio de bypass. Nenhuma consulta de uma família pode ler registros exclusivos de outra família.
6. **Busca em português + `pg_trgm` com escolha final pelo agente:** a recuperação no Postgres combina Full-Text Search nativo (`tsvector`/`tsquery` com dicionário `portuguese`) e similaridade de trigramas (`pg_trgm`) para capturar tanto variações morfológicas em português quanto identificadores de código e flags de CLI. A ferramenta retorna candidatos elegíveis dentro do escopo RLS e cabe ao **próprio agente fazer a escolha final** das unidades que serão incorporadas, respeitando cobertura obrigatória e orçamento.
7. **Entrega por ferramenta MCP escopada e `ContextManifest` com digests:** o servidor MCP da Fase 3 (`orq-knowledge-mcp`) ganha ferramentas de busca e seleção cujo escopo de família, papel, projeto e snapshot de catálogo é injetado pelo runner na inicialização da sessão (fora do controle dos argumentos enviados pelo modelo). O resultado final escolhido continua sendo materializado como um `ContextManifest` fechado e um bundle canônico com digests SHA-256 (JCS), mantendo a rastreabilidade da [ADR 0003](0003-context-bundle-materialization.md).
8. **Jev/TypeSafe AI opcional:** o adaptador `typed_decision` (Jev/TypeSafe AI) permanece opcional e fora do fluxo principal, que opera com os baselines determinísticos (`fixed` e `rules`) acrescidos do filtro de disponibilidade de cota.
9. **Plano incremental 7A–7F:** a implementação só começa após o término do 6E e o aceite desta proposta, dividida em seis incrementos pequenos e verificáveis (`7A` a `7F`), encerrando com um experimento reproduzível voltado à publicação de um artigo.

## Consequências

- O ORQ passa a executar tarefas reais usando as quatro assinaturas pessoais do autor sem custo variável de API e sem violar fronteiras de autenticação das ferramentas.
- Limites de uso de uma assinatura deixam de abortar o workflow quando houver outra família elegível capaz de assumir a tarefa ou tentativa seguinte.
- A troca de família em um fallback exige gerar nova `RoutingDecision` e novo `ContextManifest`, pois o conhecimento visível na área exclusiva da família substituta é diferente do da família original.
- O ambiente de desenvolvimento passa a contar com um container PostgreSQL opcional gerenciado via Docker para os modos com banco, enquanto o modo `offline_markdown_bm25` garante que testes locais e ambientes sem Docker continuem funcionando.
- A fronteira da [ADR 0004](0004-mcp-knowledge-boundary.md) evolui de "apenas resources estáticos" para "resources + tools somente leitura com escopo cravado pelo host e consolidados em `ContextManifest`".

## Riscos e testes

| Risco | Mitigação | Cenários / Verificações |
| --- | --- | --- |
| Mudança de formato nas mensagens de limite das CLIs | Gramáticas de detecção versionadas por adaptador + fallback para cooldown conservador quando o horário exato não puder ser lido | Q07, Q08, Q10 |
| Vazamento de conhecimento exclusivo de uma família para outra | Row-Level Security com `FORCE ROW LEVEL SECURITY` em role restrita no Postgres e testes negativos entre famílias | Q15, Q16, Q17 |
| Agente tentar trocar seu próprio `family_id` via argumento de tool MCP | `family_id`, `role_id`, `project_id` e `catalog_id` são fixados pelo runner no contexto do servidor MCP, nunca lidos dos parâmetros da tool | Q23, Q24 |
| Troca de família em fallback reutilizar contexto exclusivo da família anterior | Invariante de admissão: novo destino em outra família obriga nova seleção sob o RLS da nova família e novo `ContextManifest` | Q12, Q25 |
| Custo de assinatura ser registrado como `"0"` ou estimativa da CLI virar cobrança real | Validador recusa `amount: "0"` com `status: "unavailable"`; consumo de cota fica em estrutura própria (`QuotaConsumptionRecord`) | Q13, Q14 |
| Incompatibilidade de execução do Kiro CLI no Windows | Adaptador do Kiro encapsula invocação via `wsl.exe` com mapeamento explícito e confinado do workspace | V01–V12, Q05 |
| Indisponibilidade que não é cota (rede, DNS ou login expirado). Observado em 14/11/2026: com o DNS do WSL quebrado, o Kiro CLI respondeu "Not logged in" | Classificar separadamente de `quota_exhausted` (motivo próprio, sem `reset_at`), marcar o destino indisponível só para o run atual e nunca confundir com limite de assinatura | Q07, Q10 |
| Divergência entre Vault Markdown e banco Postgres | Ingestão no Postgres usa o mesmo chunker/digest JCS da Fase 2 e o preflight confere `content_digest` antes do despacho | Q18, Q26, Q27 |

## Decisões do dono (aceitas em 2026-11-14)

O dono aceitou as seis decisões abertas ("Aceito todas"), com estas respostas:

1. **Famílias em dois eixos independentes:** assinatura (`claude`, `codex`, `gemini`, `kiro`) e papel (`AgentRoleProfile`). Uma unidade de conhecimento declara a audiência em cada eixo, e a visibilidade combina os dois. Não existe chave composta `assinatura:papel`.
2. **Cooldown padrão de 60 minutos** quando a CLI confirma o limite de uso sem informar o horário de reset (`reset_source: "cooldown_policy"`). Depois do cooldown, o destino volta como candidato por meio de uma nova tentativa leve (sondagem barata da CLI) antes de receber tarefa real.
3. **Prioridade no baseline `rules`, por tipo de tarefa:**
   - arquitetura e especificação: `claude` → `kiro`;
   - implementação e testes: `codex` → `claude`;
   - revisão: uma família diferente da que implementou (`kiro` ou `gemini`);
   - documentação: `gemini`.
4. **Escolha da memória durante a execução:** o agente chama as tools MCP durante a própria execução da CLI. O runner audita o que foi consultado e escolhido (log do servidor MCP escopado) e lacra o `ContextManifest` efetivo da tentativa.
5. **Workspaces do Kiro:** enquanto o Kiro CLI rodar no WSL, os workspaces ficam em `C:\dev\...`, vistos pelo WSL como `/mnt/c/...`. Se o Kiro passar a rodar nativo no Windows (instalador MSI, ainda em avaliação), o adaptador deixa de usar `wsl.exe`.
6. **Experimento 7F inicial:** 10 tarefas × 3 repetições × 2 condições (memória compartilhada × memória por família), distribuídas em alguns dias para caber nas janelas de cota.
