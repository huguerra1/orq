# Adaptadores de assinatura por CLI

Estado: aceita em 2026-11-14 ([ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md)). Estende a interface `ExecutorAdapter`, as observações de uso real de 10/11/2026 e as Notas 6D registradas em [executor-choice.md](../phase-6/executor-choice.md).

## Problema

O incremento 6D implementou o protocolo `ExecutorAdapter` e o executor simulado determinístico. Para executar workflows em agentes reais sem custo variável por chamada e sem depender de modelos locais, a Fase 7 conecta as quatro assinaturas pessoais do autor por meio de suas CLIs oficiais. Cada CLI possui sintaxe de linha de comando, modelo de permissão, formato de saída e comportamento de processo distintos, além de emitir ruído em `stdout`/`stderr` e não encerrar sozinha tarefas penduradas sem um timeout imposto pelo host.

## Política de uso de assinaturas e fronteira de segurança

O hub de assinaturas da Fase 7 opera sob quatro restrições inegociáveis:

1. **Somente CLIs oficiais em modo não interativo:** o ORQ invoca apenas os executáveis oficiais instalados no ambiente (`claude`, `codex`, `agy`, `kiro-cli`), sempre sem terminal interativo (`stdin` fechado com ` subprocess.DEVNULL` ou payload estático fechado imediatamente) e sem aguardar confirmação humana no terminal.
2. **Proibição de extrair tokens ou credenciais:** o adaptador nunca lê, copia, descriptografa ou retransmite tokens OAuth, cookies, chaves de sessão ou arquivos internos de autenticação das CLIs (como `~/.codex/`, `~/.claude/`, `~/.gemini/` ou caches do AWS IAM Identity Center). A sessão autenticada pertence exclusivamente à própria CLI; o check de preflight `credentials` verifica apenas se a CLI responde em modo autenticado, sem tocar no segredo.
3. **Proibição de automatizar interface web:** nenhum adaptador pode usar automação de navegador (Playwright, Selenium, Puppeteer), scraping de páginas web ou endpoints HTTP internos não documentados das interfaces de chat.
4. **Sem API paga e sem LLM local:** o adaptador não injeta variáveis de cobrança por token (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) nem recorre a servidores locais de inferência (Ollama, llama.cpp, vLLM). Todas as quatro assinaturas (**Claude Enterprise**, **ChatGPT Plus**, **Gemini** e **Kiro Power**) foram confirmadas pelo dono como autorizadas para uso em projetos pessoais.

## Contrato comum sobre o `ExecutorAdapter` (Notas 6D)

Todos os quatro adaptadores implementam o protocolo síncrono `ExecutorAdapter` definido no incremento 6D ([executor-choice.md](../phase-6/executor-choice.md#interface-do-adaptador-de-executor)):

- `describe() -> ExecutorDescription`: devolve `executor_ref`, `adapter_version`, `runtime_ref`, `runtime_version`, declarações nos nove controles (`non_interactive`, `sandbox`, `timeout`, `cancellation`, `structured_output`, `usage_reporting`, `model_identification`, `call_identification`, `reconcile_not_found_authoritative`) no formato `routing-target-catalog.schema.json#/$defs/declaration` e o mapa `telemetry`. Nenhum controle é declarado `supported` sem o `ArtifactRef` de evidência produzido pelo checklist V01–V12 na versão exata da CLI.
- `dispatch(intent, bundle, workspace) -> DispatchAck`: valida os digests do `DispatchIntent` e do bundle JCS, prepara o diretório de trabalho isolado da tentativa, configura o servidor MCP escopado pela família da tentativa (incremento 7E), persiste o registro local durável do processo antes de iniciar o subprocesso e lança a CLI sem shell (`shell=False`, lista `argv` explícita).
- `observe(intent, until) -> ExecutionObservation`: acompanha o subprocesso até `until` (usando o relógio compartilhado do runner). Se `until` já estiver vencido no instante da chamada, retorna o estado atual sem avançar o relógio e sem levantar exceção (Achado 5 da revisão do 6D).
- `cancel(intent, reason) -> CancelObservation`: encerra toda a árvore de processos da tentativa (process group no POSIX/WSL ou Job Object / `taskkill /T /F` no Windows). Quando a árvore local morre, mas não há prova de que o processamento remoto na nuvem do provedor cessou imediatamente, registra o detalhe na observação; se a morte da árvore local for confirmada e o processo não puder mais escrever no workspace, reporta `stopped` com certeza `confirmed` para efeitos locais, preservando o consumo como incerto.
- `reconcile(dispatch_idempotency_key, external_call_id) -> ReconciliationObservation`: segue estritamente o Achado 1 da revisão do 6D. Chave desconhecida sem estado durável devolve `unknown` não autoritativo. `not_found` autoritativo só é emitido quando o adaptador mantém estado durável reconstruível (`state_hash` JCS sincronizado em disco antes do spawn) comprovando que a intenção registrada nunca chegou a iniciar o processo da CLI.
- `collect(intent) -> ExecutorResult`: higieniza o `stdout`/`stderr` com parser tolerante a ruído, verifica se houve mensagem de limite de uso (encaminhando para `quota_limit_reached` quando aplicável), valida os arquivos declarados nos `OutputSpecs` antes de publicá-los no `ArtifactStore`, extrai tokens quando disponíveis e define o custo monetário como `status: "unavailable"`, `amount: null`.

## Especificação por CLI

### 1. Codex CLI — Assinatura ChatGPT Plus (`family_codex`)

Baseado na observação real da versão `0.167.0` em Windows 10 ([executor-choice.md](../phase-6/executor-choice.md#observações-de-uso-real-10112026)):

- **Invocação não interativa:**
  ```text
  codex exec -C <workspace_dir> -s <workspace-write|read-only> -m <native_model_id> -c model_reasoning_effort="<effort>" --json -o <last_message_file> "<prompt_envelope>"
  ```
  Quando o workspace isolado da tentativa não for um repositório Git completo, acrescenta `--skip-git-repo-check`.
- **Modos de permissão e sandbox:** usa `-s read-only` para tarefas de planejamento, pesquisa ou revisão (`code_reviewer`, `security_reviewer`, `researcher`, `software_architect` sem escrita) e `-s workspace-write` quando a `ExecutionPolicy` autorizar escrita no workspace. Como observado em uso real no Windows, o sandbox `workspace-write` restringe criação de certos subprocessos (`spawn EPERM` em runners como vitest/jest com múltiplos processos); a memória exclusiva da família `codex` registrará a instrução operacional de preferir execução em thread única (`--pool threads`, `--runInBand` ou `pytest` sequencial).
- **Saída e telemetria:** `--json` emite eventos JSONL no `stdout` e `-o` grava a mensagem final estruturada em arquivo dedicado. O adaptador lê a contagem de tokens reportada no evento final de conclusão e ignora linhas de progresso intermediárias. O custo monetário permanece `unavailable` (`amount: null`).

### 2. Gemini CLI / Antigravity CLI (`agy`) — Assinatura Gemini (`family_gemini`)

Baseado na observação real da versão `agy 1.5.3` ([executor-choice.md](../phase-6/executor-choice.md#observações-de-uso-real-10112026)):

- **Invocação não interativa:**
  ```text
  agy -p "<prompt_envelope>" --model <native_model_id> --mode <accept-edits|plan> --output-format text --print-timeout <timeout_s>
  ```
  Como o `agy` não possui flag de diretório de trabalho (`-C`), o adaptador define obrigatoriamente `cwd=workspace_dir` na criação do subprocesso.
- **Modos de permissão e regras prévias:** em modo headless (`-p`), qualquer ferramenta sem regra explícita de permissão é **negada automaticamente** pelo `agy`, encerrando a execução sem saída útil e com a mensagem `no output produced`. A flag `--dangerously-skip-permissions` **nunca** é utilizada pelo ORQ. Antes do despacho, o adaptador projeta a interseção da `ExecutionPolicy` da tentativa em regras estritas de `permissions.allow` (por exemplo, `read_file(<workspace>/**)` e `command(<prefixo autorizado>)` restritos ao workspace da tentativa), restaurando a configuração ao final. Tarefas somente leitura usam `--mode plan`.
- **Saída e tratamento de ruído:** o `stderr` do `agy` emite mensagens de inicialização como `logging before google.Init`. O parser do adaptador filtra esse prefixo conhecido antes de classificar erros e exige que o artefato de saída seja gravado em arquivo determinístico no workspace ou delimitado por marcador canônico validado contra o `OutputSpec`. Se a saída contiver apenas `no output produced`, o adaptador registra `ErrorRecord` com código `permission_or_empty_output` (`failed`), nunca `completed`.

### 3. Kiro CLI — Assinatura Kiro Power (`family_kiro`)

Baseado na observação real da versão `3.1.0` rodando no Ubuntu/WSL ([executor-choice.md](../phase-6/executor-choice.md#observações-de-uso-real-10112026)):

- **Invocação não interativa:**
  No Linux nativo, executa diretamente `kiro-cli`; quando o runner roda no Windows, invoca via `wsl.exe` sem shell interativo. Os workspaces ficam em `C:\dev\...`, vistos pelo WSL como `/mnt/c/...` (decisão do dono de 2026-11-14). Se o Kiro passar a rodar nativo no Windows (instalador MSI, ainda em avaliação), o adaptador deixa de usar `wsl.exe`:
  ```text
  wsl.exe --cd <workspace_dir> -- kiro-cli chat --no-interactive --trust-workspace --trust-tools=<lista_fechada> --model <native_model_id> "<prompt_envelope>"
  ```
- **Modos de permissão e ferramentas:** a flag `--trust-all-tools` é **proibida** porque viola a negação por padrão da `ExecutionPolicy`. O adaptador monta `--trust-tools` exclusivamente com as ferramentas autorizadas pela interseção da política da tentativa:
  - somente leitura: `--trust-tools=fs_read`;
  - leitura e escrita sem execução de comandos: `--trust-tools=fs_read,fs_write`;
  - execução de comandos: `execute_bash` só entra na lista quando `CommandPolicy` autorizar explicitamente na tarefa.
- **Saída, créditos e ruído:** o `stdout` do Kiro emite linhas de progresso `[tool] ...` antes da resposta final. O parser separa a trilha de ferramentas da resposta final e valida os arquivos gerados no workspace. O multiplicador de créditos do modelo (observado em `kiro-cli chat --list-models`) é registrado no `QuotaConsumptionRecord`, enquanto o custo monetário no `UsageRecord` permanece `status: "unavailable"` e `amount: null`.

### 4. Claude Code — Assinatura Claude Enterprise (`family_claude`)

- **Invocação não interativa:**
  ```text
  claude -p "<prompt_envelope>" --output-format json --model <native_model_id> --permission-mode <mode> --allowedTools <lista_fechada> --disallowedTools <lista_negada>
  ```
  Executado com `cwd=workspace_dir` e `stdin` fechado.
- **Modos de permissão e sandbox:** o adaptador traduz a `ExecutionPolicy` em `--allowedTools` (por exemplo, `Read`, `Edit`, `Write` restritos ao workspace) e bloqueia em `--disallowedTools` ferramentas de rede (`WebFetch`, `WebSearch`) ou shell (`Bash`) quando não autorizadas pela tarefa.
- **Saída e tratamento de estimativa financeira:** com `--output-format json`, o Claude Code retorna um envelope JSON contendo `result`, `session_id`, `usage` (tokens) e, em algumas versões, um campo `total_cost_usd` calculado localmente pela CLI como equivalência de API. Como a execução ocorre sob assinatura **Claude Enterprise** (sem cobrança marginal por chamada), esse valor **não é cobrança real da tentativa**: o `UsageRecord` oficial do run mantém `cost.status = "unavailable"` e `cost.amount = null`, preservando a regra do projeto; o valor exibido pela CLI pode ser guardado apenas como anotação informativa na extensão do adaptador, sem entrar como gasto faturado nem ser substituído por `"0"`.

## Cumprimento do checklist V01–V12 por adaptador

Conforme a [ADR 0007](../adr/0007-phase-6-runner-and-simulated-executor.md) e [executor-choice.md](../phase-6/executor-choice.md#checklist-de-verificação-de-um-adaptador-real), cada item abaixo exige a captura de um `ArtifactRef` de evidência na sessão de verificação do incremento `7C` antes de mudar de `unknown` para `supported`:

Implementação 7C revisada em 2026-11-14: **118 testes aprovados, um ignorado** (symlink no Windows),
nos dois arquivos de adaptadores/detector, após corrigir os oito achados da revisão.
Os subprocessos são scripts Python sintéticos; não houve
smoke real. A tabela separa verificações do host de comprovação das CLIs instaladas.
Todos os nove controles de `describe()` continuam **`unknown`**, com `evidence_refs: []`.

| ID | Controle | Verificado por testes no 7C | Pendente no smoke real, nas quatro CLIs |
| --- | --- | --- | --- |
| **V01** | Versão identificável e fixada | Versão explícita no adaptador/estado; restauração exige a mesma versão. Fixtures reais preservam Claude 2.1.319, Codex 0.169.0 e AGY 1.5.9 | Comparar binário instalado com a versão declarada e produzir ArtifactRef por runtime; recuperar versão do Kiro |
| **V02** | Sem terminal | CLI falsa de cada família recebe EOF no stdin; supervisor e CLI usam argv e `shell=False` | Confirmar que o runtime termina sem confirmação humana |
| **V03** | Saída parseável | Claude JSON inclusive multilinha; Codex JSONL, com erro posterior invalidando final e fallback `-o`; AGY/Kiro com marcador canônico e stdout somente textual; JSON com sufixo não cria diagnóstico; contrato local validado antes da publicação | Capturar envelopes reais, arquivo `-o` do Codex e resposta ao protocolo de outputs |
| **V04** | Confinamento de escrita | Workspaces novos por tentativa; paths de coleta confinados; teste de symlink existe, ignorado neste Windows | Provar negação de escrita externa, symlinks e `.git` pelo runtime. Validar configuração local do AGY |
| **V05** | Rede negada | Nomes das variáveis recebidas pelas quatro CLIs falsas comprovam exclusão das seis variáveis sensíveis testadas; listas Claude negam WebFetch/WebSearch e regras AGY não concedem rede | Provar enforcement de rede fora da política; os testes locais não demonstram firewall do runtime |
| **V06** | Ferramentas negadas | Argv exato nos modos de leitura/escrita; comandos só entram com concessão explícita; nenhuma flag proibida | Provar negação sem prompt e precedência das regras perante configurações próprias de cada CLI |
| **V07** | Timeout mata a árvore | Quatro CLIs falsas com filho escrevendo heartbeat: timeout autônomo para ambos; Job Object exercitado no Windows; falha de associação e espera expirada simuladas; grupo POSIX simulado só confirma quando desaparece | Repetir com binários reais; executar também o caminho POSIX e confirmar árvore Linux no WSL |
| **V08** | Cancelamento confirmável | Mesmo teste de árvore após `cancel`; limpeza independente do Job para filho suspenso; recibo unknown se a árvore não foi confirmada e consumo remoto desconhecido | Repetir com as árvores reais, inclusive WSL e filhos das ferramentas |
| **V09** | Uso e custo | Tokens Claude/Codex sintéticos em uso e consumo; custo sempre unavailable/null, inclusive ignorando `total_cost_usd`; registros 7A validados | Conferir telemetria real; multiplicador Kiro permanece null até fonte comprovada |
| **V10** | Modelo resolvido | Campo presente nos envelopes sintéticos é preservado; ausência continua null | Capturar identificação efetiva, sem copiar o modelo solicitado como se fosse resolvido |
| **V11** | Chamada e reconciliação | Sessão Claude/Codex; estado/recibo com hashes, fsync, temporários únicos e retries limitados no Windows; estresse de 25 saídas rápidas Kiro e escritores/leitor concorrentes; validação antes de criar diretórios; reconstrução, corrupção, conflito e não execução comprovada; status/sessão/evidência do mesmo recibo | Conferir IDs reais e reconciliação externa. Reinício com processo ainda incerto continua unknown |
| **V12** | Contexto adicional | Cobertura sempre unknown; nenhum inventário de HOME nem leitura de diretórios de credenciais | Auditar somente instruções/configuração autorizadas do workspace; sem ler `~/.codex`, `~/.claude`, `~/.gemini` ou caches de login |

Nenhum adaptador declarará `provider_context_coverage: "complete"`, pois todas as CLIs de assinatura injetam prompt de sistema próprio do provedor não auditável byte a byte pelo ORQ. O valor máximo honesto é `platform_only` quando os arquivos locais de instrução estão sob controle do workspace limpo, ou `unknown` caso contrário.

## API e decisões de implementação 7C

- `orq_contracts.adapters` exporta `ClaudeCodeAdapter`, `CodexAdapter`, `AgyAdapter`,
  `KiroAdapter`, `CLIPermissions` e `QuotaLimitDetector`. Construtores recebem
  `SubscriptionProfile` validado, `ArtifactStore`, `state_directory`, `workspace_root`,
  `clock` idêntico ao do journal, `runtime_version` e `ContentContractRegistry` local.
  `CLIPermissions` é a interseção efetiva já calculada pelo host; não substitui a
  admissão da ExecutionPolicy. Por padrão só permite leitura e não concede comandos.
- O supervisor stdlib é lançado sem shell, fecha stdin e persiste recibos antes e
  depois do spawn da CLI. Windows: criação suspensa, associação ao Job Object antes
  de retomar, kill-on-close e confirmação de zero processos ativos. POSIX: sessão
  própria, SIGKILL no grupo e sondagem limitada até seu desaparecimento; grupo
  restante ou espera expirada produzem unknown. Falha ao associar o Job também
  encerra e recolhe o filho direto suspenso. O supervisor aplica timeout mesmo sem `observe()`;
  o prazo inclui sua inicialização. O relógio injetado resolve instantes do runner;
  espera e enforcement usam relógios reais do host.
- Kiro no Windows usa `wsl.exe --cd /mnt/c/dev/... -- python3 <supervisor> <estado>`;
  o supervisor lança `kiro-cli chat ...` diretamente, sem shell, dentro do Linux.
  Isso permite encerrar o grupo Linux em vez de matar apenas `wsl.exe`. Exige
  `python3` stdlib no WSL e acesso aos caminhos `/mnt/c`; ambos pendem do smoke.
  Linux nativo usa o Python do runner e `kiro-cli`, sem `wsl.exe`.
- Estado fica fora do workspace, com `state_hash` compatível com JCS, fsync do
  arquivo/diretório e substituição atômica com temporário único no mesmo diretório.
  Leitura, criação/substituição e limpeza toleram `PermissionError` no Windows por
  até 100 tentativas a cada 10 ms. `observe` relê o recibo após a saída do supervisor;
  `reconcile` mantém status, sessão e evidência em um único snapshot.
  `prepared` ou recibo `not_started`
  permitem `not_found` autoritativo com evidência armazenada; `spawning` sem recibo,
  PID perdido ou chave desconhecida retornam unknown e nunca autorizam redespacho.
  O Router ainda exige o controle verificado antes de usar essa autoridade.
- Outputs usam `ORQ_FINAL:{"outputs":{"<output_id>":<JSON>}}` na resposta final,
  ou marcador com outputs vazio e arquivos `.orq-output/<output_id>.json`.
  Todos os contratos são validados antes de publicar bytes no ArtifactStore.
  JSON/JSONL de ferramenta não é fonte de cota. Texto livre de stdout também não;
  somente erros estruturados da CLI e diagnósticos ancorados de stderr entram no
  detector. Essa escolha conservadora evita inferir cota de texto do projeto.
- AGY recebe regras novas em `.gemini/antigravity-cli/settings.json` **do workspace**,
  removidas após término confirmado. O workspace precisa ser novo, sem configuração
  anterior a restaurar. Confirmar no smoke que a versão fixada carrega e prioriza
  esse arquivo; até lá, sandbox e demais controles permanecem unknown. Kiro
  `execute_bash` é uma concessão ampla: a flag não prova restrição por prefixo.
- `collect()` mantém o protocolo 6D; `quota_consumption(intent)` retorna a extensão
  7A persistida após coleta, para o runner 7E registrar no journal. Uma invocação
  conta `messages_consumed: 1`, sem afirmar aceitação ou cobrança pelo provedor;
  interrupções mantêm `quota_outcome: unknown`, tokens/multiplicador desconhecidos
  ficam null. Resultado e consumo são reconstruíveis e a coleta é idempotente.
- Gramáticas `claude-quota-v1`, `codex-quota-v2` (adiciona suporte a data absoluta local observada em `codex/usage-limit-2026-11-14.txt`), `agy-quota-v1` e `kiro-cli-quota-v1`
  resolvem relógio local/data explícita para UTC, com rollover de dia. Sem horário
  válido, reset_at fica null e o cooldown padrão é 60 minutos, com indicação de
  sondagem obrigatória. Login/rede têm motivo próprio e nenhum reset. O 7C produz
  bloqueio e erro 7A; reativação, sondagem leve e fallback são responsabilidade do 7E.

Não há smoke automático habilitado nesta entrega. O gate V01–V12 continua aberto
até a sessão real com versões fixadas e ArtifactRefs; testes sintéticos não o fecham.
