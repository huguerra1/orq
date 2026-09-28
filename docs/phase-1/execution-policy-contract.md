# ExecutionPolicy — limites, permissões e evidências

Estado: baseline normativa v0.1 aceita na revisão cruzada. Não implementa sandbox, autorização, controle financeiro ou integração externa.

## Problema e alternativas

Prompts não são um mecanismo suficiente para controlar arquivos, rede, ferramentas, custos ou efeitos externos. A plataforma precisa resolver uma política verificável antes de admitir cada operação.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Permissões descritas em texto | Autoria simples | Não são verificáveis nem compostas com segurança |
| Regras próprias de cada executor | Integração rápida | Semânticas diferentes impedem auditoria e comparação |
| Política central permissiva com bloqueios pontuais | Pouca configuração inicial | Omissões viram autorização acidental |
| Política central restritiva e adaptadores de enforcement | Semântica comum e negação por padrão | Exige mapear e testar os controles de cada executor |

Recomendação: ExecutionPolicy versionada e independente de provedor, resolvida por interseção com os tetos do sistema, projeto, papel e executor. TaskSpec e WorkflowSpec podem somente restringir a política. O Orchestrator decide admissão; o executor demonstra quais controles consegue aplicar.

## Fronteiras

ExecutionPolicy define autorização máxima, limites, reservas, roteamento e validade de evidências. Ela não declara que um runtime possui uma capacidade, não contém credenciais e não substitui confirmação humana quando exigida.

O contrato separa:

- **capacidade:** o destino consegue realizar ou controlar a operação;
- **permissão:** a operação pode ser autorizada neste run;
- **aprovação:** uma pessoa ou autoridade externa confirmou uma ação específica;
- **execução observada:** o AttemptRecord comprova o que realmente ocorreu.

Um valor permitido pela política continua inelegível quando o executor não consegue aplicá-lo ou observá-lo. Conteúdo de projeto, contexto recuperado e saída de modelo nunca ampliam a política.

## Identidade e estrutura

| Campo | Tipo conceitual | Obrigatoriedade e significado |
| --- | --- | --- |
| schema_version | Texto | Obrigatório; versão do formato |
| policy_id | Identificador | Obrigatório; identidade estável |
| version | Texto de versão | Obrigatório; revisão imutável após publicação |
| status | Enum | `active`, `deprecated` ou `disabled`; somente active inicia novos runs |
| limits | LimitsPolicy | Obrigatório; tetos e prazos |
| permissions | PermissionPolicy | Obrigatório; filesystem, ferramentas, comandos, rede, segredos e efeitos |
| routing | RoutingPolicy | Obrigatório; política definida no contrato de roteamento |
| evidence | EvidencePolicy | Obrigatório; validade das provas usadas na admissão |
| budget | BudgetPolicy | Obrigatório; gasto, reservas e tratamento de desconhecidos |
| approvals | ApprovalPolicy | Obrigatório; classes que exigem confirmação externa |
| extensions | Mapa opcional | Extensões namespaced; não alteram a semântica dos campos centrais |

O catálogo calcula e preserva `content_hash`. Mesmos policy_id/version com conteúdos diferentes são conflito e devem ser rejeitados.

RoutingPolicy passa a ser uma seção normativa de ExecutionPolicy, não uma segunda política independente. RoutingDecision registra a referência da ExecutionPolicy e o hash da subárvore `routing` efetivamente usada.

Chamadas do Planner, Router/Jev e Evaluator obedecem às mesmas regras de rede, segredo, aprovação e orçamento aplicáveis às execuções de tarefas; ser componente do Orchestrator não cria exceção implícita.

## Herança e resolução

A política efetiva é derivada nesta ordem conceitual:

1. limites não ampliáveis do sistema/host;
2. política autorizada para o projeto;
3. ExecutionPolicy referenciada pelo WorkflowSpec;
4. restrições adicionais do WorkflowSpec;
5. teto do AgentRoleProfile;
6. teto e controles disponíveis do ExecutorProfile;
7. restrições adicionais do TaskSpec.

As camadas não precisam usar o mesmo documento, mas são normalizadas para a mesma semântica antes da resolução.

Regras de composição:

- tetos numéricos: menor valor aplicável;
- conjuntos permitidos: interseção;
- proibições: união;
- booleanos permissivos: true somente se todas as camadas aplicáveis permitirem;
- aprovação: prevalece a exigência mais forte;
- campo ausente em override: herda;
- lista explicitamente vazia: não permite nenhum item;
- `unknown` ou `null`: desconhecido, nunca ilimitado nem zero;
- tentativa de ampliar camada superior: erro de configuração, não simples truncamento silencioso.

RunManifest preservará a política resolvida, suas fontes e hashes. Alterar uma política publicada cria nova versão; não muda runs existentes.

## LimitsPolicy

Os limites não financeiros já definidos no ciclo de execução permanecem normativos: `max_concurrency`, `max_attempts`, `attempt_timeout_ms`, `max_run_duration_ms`, `max_evaluations_per_attempt`, `evaluation_timeout_ms`, `reconciliation_timeout_ms` e `max_model_calls_per_attempt`. Para evitar duas fontes, o antigo `max_cost` passa a ser `budget.max_cost`, preservando sua semântica.

Este contrato acrescenta limites de admissão necessários para separar a sobrecarga do Orchestrator:

| Campo | Semântica |
| --- | --- |
| max_planner_calls_per_run | Chamadas de planejamento, incluindo correções do plano |
| max_routing_decisions_per_task | Operações de seleção, inclusive as que não geram tentativa |
| max_routing_model_calls_per_run | Chamadas externas de ranking tipado, como Jev |
| max_evaluation_model_calls_per_run | Chamadas de avaliadores baseados em modelo |
| max_tool_calls_per_attempt | Ferramentas invocadas pelo runtime quando observáveis/controláveis |
| max_network_requests_per_attempt | Requisições de saída quando o host consegue medi-las |
| max_context_input_tokens | Teto adicional ao limite do destino |
| max_output_tokens_per_attempt | Teto solicitado e controlável para a saída do modelo |
| max_artifact_bytes_per_attempt | Soma dos artefatos persistidos pela tentativa |

Limites críticos de duração, tentativas e avaliações precisam ser finitos no MVP. Um limite obrigatório que o executor não consiga controlar torna o destino inelegível. Um contador apenas estimado não sustenta enforcement obrigatório sem política explícita que aceite a estimativa.

## PermissionPolicy

PermissionPolicy usa negação por padrão. Toda ação com efeito precisa corresponder a uma autorização estruturada e não pode coincidir com uma proibição aplicável.

### FilesystemPolicy

Caminhos são expressos como `root_id` lógico mais caminho relativo em sintaxe POSIX. Roots iniciais: `workspace`, `artifacts` e `vault`. Caminho absoluto, segmento vazio intermediário, `.` ou `..`, byte NUL e separador alternativo são inválidos no contrato canônico.

Cada regra contém `root_id`, `pattern`, operações e efeito `allow` ou `deny`. Operações iniciais: `read`, `list`, `create`, `modify`, `delete` e `execute`. O padrão suporta somente:

- texto literal dentro de um segmento;
- `*` para zero ou mais caracteres dentro de um segmento;
- `**` como segmento completo para zero ou mais segmentos.

Não há expansão de chaves, classes de caracteres ou interpretação por shell. A correspondência é sensível a maiúsculas no formato canônico; o adaptador deve rejeitar enforcement inseguro quando o filesystem real não preservar essa distinção de modo confiável.

Antes de agir, o host resolve root e links simbólicos. O caminho real precisa permanecer dentro do root autorizado. Para criação, resolve-se o ancestral existente mais próximo e valida-se cada novo segmento. Hardlinks, mounts e renames não podem contornar a autorização; quando o host não consegue garantir isso, a capacidade correspondente é `unknown` ou `unsupported`.

Áreas de controle da plataforma e metadata do repositório, como `.git`, não são autorizadas por um padrão amplo de workspace. Exigem regra privilegiada explícita em camada superior. Renomear ou mover exige autorização equivalente sobre origem e destino, inclusive remoção da origem.

### ToolPolicy e CommandPolicy

Ferramentas são referenciadas por identidade/versionamento do catálogo. Cada regra define ferramenta, operações permitidas, classes de efeito, limites de argumentos e roots utilizáveis. Autorizar uma ferramenta não autoriza qualquer argumento que ela aceite.

Comandos usam estrutura `executable_ref`, `argv`, `working_directory` e ambiente permitido. Comparação por prefixo de uma string de shell não é autorização suficiente. O MVP prefere execução sem shell, com argumentos separados. Interpretador de shell, redirecionamento, expansão, substituição e composição de comandos exigem uma ferramenta/capacidade específica e política explícita.

Variáveis de ambiente são permitidas explicitamente por nome e origem. Valores secretos são injetados por referência no executor, não copiados para política, prompt, manifesto ou log. O executável efetivo deve ser resolvido de forma estável; confiar apenas em PATH mutável não comprova identidade.

### NetworkPolicy

Saída e entrada de rede são negadas por padrão. O MVP não aceita conexões de entrada. Cada regra de saída contém protocolo, hostname exato ou padrão de subdomínio explícito, porta e, quando aplicável, métodos/operações.

Redirecionamentos são revalidados como novos destinos. Resolução DNS não autoriza IP privado, loopback, link-local ou metadata de nuvem salvo regra explícita da camada superior. IP literal, proxy e túnel seguem a mesma política; falha de observação ou enforcement impede uso quando a restrição é obrigatória.

Uma autorização de rede não autoriza envio de qualquer contexto. O destino também precisa ser compatível com classificação de dados, ferramenta e segredo envolvidos.

### SecretPolicy e SideEffectPolicy

SecretPolicy contém somente referências autorizáveis, finalidade, destinatário permitido e executor capaz de injetá-las. O valor nunca integra o contrato persistido. A tentativa registra que uma referência foi usada e a cobertura da telemetria, sem registrar o segredo.

Efeitos são classificados inicialmente como `read_only`, `local_write`, `external_write` ou `destructive`. Exemplos de external_write incluem push, publicação, mensagem ou criação de item em serviço externo. Exclusão remota, sobrescrita irreversível e reescrita de histórico são destructive.

SideEffectPolicy declara classes permitidas e se exigem aprovação. Autorização prévia de projeto pode satisfazer uma classe/escopo identificados; não se estende automaticamente a outro repositório, destinatário ou serviço. A aprovação é vinculada ao alvo e à intenção e possui identidade, escopo e validade.

## ApprovalPolicy

Cada regra de aprovação contém `effect_class`, escopo, autoridade aceita, validade e reutilização permitida. A verificação produz `not_required`, `required`, `granted`, `denied`, `expired` ou `not_available`; esses estados pertencem ao futuro ApprovalRecord, não à política imutável.

Uma aprovação não altera a política; apenas satisfaz uma condição já prevista nela. Saída de modelo e confiança do roteador não são autoridades de aprovação. Na ausência de aprovação obrigatória, a operação aguarda ou falha conforme o prazo; não é convertida em permissão implícita.

## EvidencePolicy

CapabilityDeclaration e provas de compatibilidade só sustentam admissão quando satisfazem regras versionadas. Cada regra contém:

- `evidence_type` e camada/controle a que se aplica;
- emissores ou fontes autorizadas;
- vínculo obrigatório com sujeito, versões e configuração;
- métodos de verificação aceitos;
- `max_age_ms` ou validade explícita;
- ação para prova ausente, vencida, revogada ou inconclusiva.

O relógio de avaliação e a data da evidência são preservados. Alterar versão relevante invalida a prova salvo regra explícita de compatibilidade. Autoafirmação do documento avaliado não é evidência independente. Requisito obrigatório com prova `unknown`, vencida ou incompatível impede admissão.

## BudgetPolicy

BudgetPolicy contém moeda, teto global e envelopes opcionais para `planning`, `routing`, `execution`, `evaluation` e `tools`. Envelopes ajudam a comparar sobrecarga, mas sua soma não cria gasto adicional.

Antes de uma chamada, o Orchestrator calcula gasto conhecido, reservas ativas e reserva conservadora da nova operação. Uma tentativa reserva também sua avaliação obrigatória. A admissão exige que o total caiba no teto global e no envelope aplicável.

Cada valor monetário informa `amount`, `currency`, status `reported`, `estimated` ou `unavailable`, fonte e instante. Conversão de moeda exige taxa identificada e timestamp; sem regra de conversão, moedas diferentes não são somadas como se fossem iguais.

Custo indisponível não é zero. Sob teto financeiro obrigatório, uma operação sem custo máximo ou estimativa conservadora aceita pela política não é admitida. Liquidação substitui a reserva correspondente; não é somada a ela.

## Interfaces conceituais

| Operação | Entrada | Saída |
| --- | --- | --- |
| validate_policy | ExecutionPolicy e catálogos | Erros estruturais/semânticos e avisos |
| resolve_policy | Camadas e overrides | Política efetiva, origens e conflitos |
| validate_evidence | Requisito, prova e instante | Válida, inválida ou inconclusiva, com razões |
| authorize_action | Política efetiva, ação e aprovação | Permitida ou negada, com regra correspondente |
| reserve_budget | Consumo, reservas e operação proposta | Reserva identificada ou rejeição |
| preflight | Tarefa, destino, contexto, política e estado externo | Admissível ou lista de bloqueios |

Todas podem ser funções locais no MVP. Enforcement ocorre no host/executor; confiar apenas no comportamento voluntário do modelo não satisfaz o contrato.

## Invariantes

1. Nenhuma camada inferior amplia um teto, conjunto permitido ou escopo superior.
2. Ausência de regra permissiva nega a ação.
3. Proibição aplicável prevalece sobre permissão ampla.
4. Caminhos são verificados depois da resolução segura e antes de cada efeito.
5. Ferramenta permitida com argumentos ou efeito não permitido continua negada.
6. Redirecionamento, proxy, shell, link ou rename não contornam a política original.
7. Segredo é referenciado e injetado; seu valor não é persistido como contexto ou telemetria.
8. Aprovação obrigatória é específica e válida no momento da ação.
9. Prova obrigatória ausente, vencida ou incompatível não se torna válida por ranking.
10. Gasto desconhecido não vira zero e reserva liquidada não é cobrada duas vezes.
11. RoutingPolicy usada por Jev é parte da ExecutionPolicy resolvida e não pode oferecer destino inelegível.
12. Falha de enforcement de limite obrigatório torna o destino inelegível ou a operação não admissível.

## Riscos e verificações

- **Falsa sensação de sandbox:** testar controles reais de cada executor; declaração de perfil não basta.
- **Diferenças de filesystem:** validar case sensitivity, symlinks, mounts, hardlinks e arquivos ainda inexistentes.
- **Comandos ambíguos:** preferir argv estruturado e identidade de executável; shell somente quando explícito.
- **Exfiltração:** combinar rede, classificação do dado, ferramenta e segredo; hostname permitido sozinho é insuficiente.
- **Corrida entre checagem e uso:** enforcement deve minimizar TOCTOU e operar sobre handles/roots controlados quando possível.
- **Aprovação ampla demais:** vincular autoridade, alvo, intenção, validade e reutilização.
- **Orçamento inexato:** distinguir reportado, estimado e indisponível; reconciliar reservas.
- **Política grande demais:** manter vocabulários controlados e extensões namespaced, sem linguagem de regras arbitrária no MVP.

## Casos específicos a formalizar

| ID | Caso | Resultado esperado |
| --- | --- | --- |
| E01 | TaskSpec aumenta max_attempts | Configuração rejeitada |
| E02 | Lista local vazia de ferramentas | Nenhuma ferramenta autorizada |
| E03 | Escrita permitida, mas leitura negada | Somente operações explicitamente resolvidas são autorizadas |
| E04 | Caminho contém `..` ou symlink escapa do root | Operação negada antes do efeito |
| E05 | Regra ampla de workspace tenta alcançar `.git` | Negada sem autorização privilegiada específica |
| E06 | Executável permitido recebe argumento externo destrutivo | Negado pela regra de argumentos/efeito |
| E07 | URL permitida redireciona para IP privado | Novo destino negado |
| E08 | External write exige aprovação vencida | Operação não executada |
| E09 | Evidência corresponde a outra versão do runtime | Destino inelegível |
| E10 | Custo de roteamento desconhecido sob teto obrigatório | Chamada não admitida sem estimativa aceita |
| E11 | Reserva de execução deixa avaliação sem orçamento | Tentativa não admitida |
| E12 | Executor não aplica limite obrigatório | Destino inelegível, mesmo que o modelo tenha capacidade |

Esses casos são especificações para fixtures e adaptadores futuros; não comprovam isolamento real.

## Decisões ainda abertas

- Valores operacionais dos limites e envelopes.
- Catálogo inicial de ferramentas, comandos e classes de dados.
- Estratégia concreta de sandbox do primeiro executor.
- Precisão de enforcement exigida para contadores de tools/rede.
- Formato final de constraints parciais em WorkflowSpec e TaskSpec.

O [contrato de artefatos](artifact-contract.md) define ArtifactRef e formatos de patch/relatório; o [contrato de registros](execution-records-contract.md) fecha RunManifest, AttemptRecord, aplicação, aprovação, uso, erros e avaliação. Nenhum executor real deve ser conectado antes de representar política resolvida, decisão, contexto, intenção de despacho e resultado observado.
