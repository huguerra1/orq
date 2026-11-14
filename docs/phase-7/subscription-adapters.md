# Adaptadores de assinatura por CLI

Estado: Proposta em 2026-11-14 ([ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md)). Estende a interface `ExecutorAdapter`, as observações de uso real de 10/11/2026 e as Notas 6D registradas em [executor-choice.md](../phase-6/executor-choice.md).

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
  No Linux nativo, executa diretamente `kiro-cli`; quando o runner roda no Windows, invoca via `wsl.exe` sem shell interativo:
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

| ID | Controle verificado | Claude Code (`family_claude`) | Codex CLI (`family_codex`) | Gemini CLI / `agy` (`family_gemini`) | Kiro CLI (`family_kiro`) |
| --- | --- | --- | --- | --- | --- |
| **V01** | Versão identificável e fixada | `claude --version` gravado na evidência e fixado no `ExecutorProfile` | Observado `0.167.0` via `codex --version` | Observado `1.5.3` via `agy --version` | Observado `3.1.0` via `kiro-cli --version` no WSL |
| **V02** | Execução sem terminal (`stdin` fechado) | `-p` com `stdin=DEVNULL` termina sem prompt | Observado com `codex exec` e `stdin=DEVNULL` | Observado com `agy -p` e `stdin=DEVNULL` | Observado com `kiro-cli chat --no-interactive` |
| **V03** | Saída estruturada parseável | `--output-format json` validado contra schema do adaptador + arquivos de output | `--json` (eventos JSONL) + `-o <arquivo>` validados por schema | Artefato gravado no workspace + parser que descarta ruído `google.Init` no `stderr` | Artefato gravado no workspace + parser que separa linhas `[tool] ...` da saída final |
| **V04** | Escrita fora do workspace negada | Restrição de ferramentas ao `cwd` + verificação pós-execução de paths, symlinks e `.git` | Sandbox `-s read-only` / `-s workspace-write` + verificação do host sobre `.git` e symlinks | Modo `--mode plan` ou regras `permissions.allow` restritas ao path do workspace | `--trust-tools=fs_read` (sem `fs_write`) ou workspace isolado com checagem de integridade |
| **V05** | Rede fora da lista negada | `WebFetch`/`WebSearch` em `--disallowedTools`; tráfego restrito ao endpoint de inferência da assinatura | Sandbox bloqueia rede arbitrária de ferramentas; comunicação apenas com o backend do Codex | Sem permissão de ferramentas de busca web ou comandos de rede em `permissions.allow` | Sem `execute_bash` e sem ferramentas MCP de rede externas |
| **V06** | Ferramenta não autorizada negada sem prompt | Ferramenta fora de `--allowedTools` é recusada imediatamente em `-p` | Comandos fora da política de sandbox falham sem abrir prompt interativo | Observado: ferramenta sem regra em `settings.json` é negada automaticamente em modo headless | Observado: ferramenta fora de `--trust-tools` (ex.: omitir `execute_bash`) não é executada |
| **V07** | Timeout imposto pelo host mata processo e filhos | Host aplica `attempt_timeout_ms` via Job Object / process group e verifica ausência de órfãos | Mesmo mecanismo do host sobre `codex exec` e processos filhos | Mesmo mecanismo do host sobre `agy` (além de `--print-timeout` como defesa secundária) | Host encerra o processo no WSL e confirma término da árvore Linux |
| **V08** | Cancelamento confirmável | Encerramento da árvore local confirmado por inspeção de PID/process group após `cancel()` | Mesma verificação de árvore de processos após `cancel()` | Mesma verificação de árvore de processos após `cancel()` | Confirmação de término dos processos dentro da instância WSL |
| **V09** | Uso de tokens e custo (`reported`/`estimated`/`unavailable`) | Tokens `reported` no JSON; custo monetário da assinatura fixado como `unavailable` (`amount: null`) | Observado: contagem de tokens no fim do log (`reported`); custo monetário `unavailable` (`amount: null`) | Tokens `unavailable` (se não impressos em `text`); custo monetário `unavailable` (`amount: null`) | Multiplicador de créditos e consumo registrados em cota; custo monetário `unavailable` (`amount: null`) |
| **V10** | Modelo resolvido reportado | Lido do JSON de saída quando presente; caso contrário `resolved_model_id: null` | Lido dos eventos JSONL quando presente; caso contrário `null` | `resolved_model_id: null` se a saída texto confirmar apenas o solicitado | `resolved_model_id: null` se a saída não explicitar revisão interna |
| **V11** | ID de chamada/sessão e reconciliação | `session_id` capturado quando emitido; antes do spawn, reconciliação usa o estado durável do adaptador | ID de sessão dos eventos JSONL + estado durável local do adaptador | Estado durável local do adaptador (`state_hash` JCS antes e depois do spawn) | Estado durável local do adaptador (`state_hash` JCS antes e depois do spawn) |
| **V12** | Contexto adicional do runtime identificável | Inventário de `CLAUDE.md` e configurações locais; cobertura declarada como `platform_only` | Inventário de `AGENTS.md` e `~/.codex/config.toml`; cobertura `platform_only` | Inventário de `~/.gemini/antigravity-cli/settings.json`; cobertura `platform_only` | Inventário de arquivos de workspace lidos pelo Kiro; cobertura `platform_only` |

Nenhum adaptador declarará `provider_context_coverage: "complete"`, pois todas as CLIs de assinatura injetam prompt de sistema próprio do provedor não auditável byte a byte pelo ORQ. O valor máximo honesto é `platform_only` quando os arquivos locais de instrução estão sob controle do workspace limpo, ou `unknown` caso contrário.
