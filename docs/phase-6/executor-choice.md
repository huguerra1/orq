# Escolha do primeiro executor/runtime

Estado: proposta da Fase 6, aguardando revisão. Nenhum adaptador foi implementado e nenhuma CLI de agente foi executada pelo ORQ.

## Problema

O gate G6 da [revisão cruzada](../phase-1/cross-review.md) exige escolher o primeiro executor, sua sandbox e os testes reais de enforcement antes de qualquer chamada de agente. O [contrato de perfis](../phase-1/agent-profiles.md) proíbe declarar controles como `supported` sem evidência ligada à versão. A escolha precisa, portanto, separar o que foi verificado do que é apenas afirmado em documentação ou lembrado por quem escreve.

## Fonte e limite desta comparação

- O acesso à web estava indisponível durante a redação. Nenhuma documentação oficial foi consultada nesta sessão e nenhuma CLI foi executada.
- Os detalhes de Claude Code e Codex CLI abaixo vêm do conhecimento prévio do redator sobre documentação pública. Todos estão marcados **a verificar** até que uma versão seja fixada e o comportamento observado.
- Para o Kiro CLI existe uma observação indireta: durante a redação, uma mensagem de erro do próprio Kiro CLI citou as opções `--no-interactive` e `--trust-all-tools`. A versão não foi registrada; a semântica exata continua a verificar.
- Para o Antigravity CLI (`agy`), o redator não tem informação confiável sobre modo não interativo, flags ou formato de saída. Tudo está marcado a verificar, inclusive a existência de modo headless.

Nada desta página pode ser copiado para uma CapabilityDeclaration como `supported`. Cada linha vira `unknown` até produzir evidência pelo [checklist de verificação](#checklist-de-verificação-de-um-adaptador-real).

### Observações de uso real (10/11/2026)

Depois da redação, três dessas CLIs foram usadas de verdade, sem interação, para delegar tarefas de código
nos repositórios do autor (Windows 10, com o Kiro rodando no Ubuntu do WSL). As observações abaixo são
**evidência de uso**, com versão registrada, mas ainda não substituem o checklist V01–V12: faltam timeout
imposto pelo host, cancelamento e reporte de custo.

| CLI e versão | Invocação usada | Observado |
|---|---|---|
| Codex CLI `0.167.0` | `codex exec -C <dir> -s workspace-write -m <modelo> -c model_reasoning_effort="<nível>" -o <arquivo> "<prompt>"` | Modo não interativo com saída final em arquivo (`-o`), eventos em JSONL (`--json`) e `--skip-git-repo-check` fora de repositório. `-s read-only` para revisão. No sandbox `workspace-write` do Windows, alguns runners de teste (vitest, jest) falharam ao criar processos filhos (`spawn EPERM`); funcionaram com `--pool threads` ou `--runInBand`. O log termina com a contagem de tokens. O modelo e o esforço padrão vêm de `~/.codex/config.toml`. |
| Antigravity CLI `agy 1.5.3` | `agy -p "<prompt>" --model <id> --mode accept-edits\|plan --output-format text --print-timeout <duração>` (rodado a partir da pasta de trabalho; não há flag de diretório) | **Existe modo headless** (`-p`). Os modelos são listados por `agy models` (por exemplo `gemini-4-argon-high`, `gemini-3.8-flash-medium`). No modo headless, ferramentas sem regra de permissão são **negadas automaticamente**: a execução termina sem saída útil e com a mensagem `no output produced`. Regras ficam em `~/.gemini/antigravity-cli/settings.json` (`permissions.allow`, como `read_file(<caminho>)` e `command(<prefixo>)`). O stderr traz linhas de ruído `logging before google.Init`. `--dangerously-skip-permissions` existe, mas não foi usado. |
| Kiro CLI `3.1.0` (Linux/WSL) | `kiro-cli chat --no-interactive --trust-workspace --trust-tools=fs_read,fs_write --model <id> "<prompt>"` (o prompt também pode vir pelo stdin) | Modo não interativo funcionou com login normal via IAM Identity Center, sem API key. `--trust-tools` limita as ferramentas: sem `execute_bash`, o agente edita arquivos mas não roda comandos. `kiro-cli chat --list-models` mostra o multiplicador de créditos de cada modelo. O stdout traz linhas de progresso `[tool] ...` antes da resposta final. |

Consequência para o adaptador: os três precisam de **timeout imposto pelo host** (nenhum foi observado
encerrando sozinho uma tarefa longa) e de um **parser tolerante a ruído** no stdout/stderr. O `agy` exige que
as permissões fiquem declaradas antes da execução, o que combina com a negação por padrão do ORQ.

## Controles exigidos pelo ORQ

| Controle | Por que o runner precisa | Consequência se ausente |
| --- | --- | --- |
| Modo não interativo | O runner não pode depender de terminal ou resposta humana | Inelegível |
| Sandbox/permissões | ExecutionPolicy nega por padrão filesystem, rede, ferramentas e comandos | Inelegível para tarefas com escrita ou execução |
| Timeout | `attempt_timeout_ms` é obrigatório e finito | O host precisa matar o processo; sem confirmação de término a tentativa vira `unknown` |
| Cancelamento confirmável | Cancelamento e timeout exigem término confirmado antes de retry | Retry bloqueado; reconciliação até `indeterminate` |
| Saída estruturada | Output bindings, uso e erros precisam ser lidos sem heurística sobre texto livre | Artefatos não vinculáveis; tentativa não chega a `completed` |
| Uso/custo reportado | `unknown` nunca vira zero; teto financeiro exige custo conhecido ou estimativa conservadora | Custo `unavailable`; sob teto obrigatório, admissão negada |
| Identificação de modelo | Experimentos registram modelo solicitado e resolvido | `resolved_model_id` permanece null; comparação experimental enfraquecida |
| Identificação de chamada | Reconciliação usa a mesma intenção e, quando existir, o ID externo | Reconciliação só pelo estado local do processo |
| Contexto observável | ContextManifest declara `provider_context_coverage` | Cobertura `platform_only` ou `unknown`, nunca `complete` sem prova |

## Candidatos

### Executor simulado determinístico

| Controle | Situação | Fonte |
| --- | --- | --- |
| Modo não interativo | Por construção: função local chamada pelo runner | Especificação desta fase |
| Sandbox/permissões | Não executa código, não acessa rede; escreve somente no armazenamento de artefatos do run | Especificação desta fase |
| Timeout | Relógio injetado; o roteiro decide se o término é confirmado | Especificação desta fase |
| Cancelamento | Roteirizável como confirmado ou não confirmado | Especificação desta fase |
| Saída estruturada | Artefatos JSON canônicos derivados do roteiro | Especificação desta fase |
| Uso/custo | Roteirizável como `reported`, `estimated` ou `unavailable` | Especificação desta fase |
| Modelo | O modelo solicitado é `execution_target_snapshot.model_ref` do cabeçalho da tentativa, copiado do destino; `resolved_model_id` vem do roteiro, é gravado em `result_collected` e pode ser null | Especificação desta fase |
| ID de chamada | Derivado deterministicamente da `dispatch_idempotency_key` | Especificação desta fase |

Esses controles serão verificados por testes do próprio ORQ na implementação 6D. Eles não dizem nada sobre runtimes reais.

### Claude Code

| Controle | Situação | Detalhe |
| --- | --- | --- |
| Modo não interativo | A verificar | Modo de impressão (`-p`/`--print`) descrito na documentação pública |
| Sandbox/permissões | A verificar | Modos de permissão (`--permission-mode`) e listas de ferramentas permitidas/negadas (`--allowedTools`, `--disallowedTools`); recurso de sandbox do runtime com comportamento em modo headless a verificar |
| Timeout | A verificar | Nenhum timeout global conhecido; limite de turnos (`--max-turns`) não é timeout. Assumir enforcement pelo host |
| Cancelamento | A verificar | Encerramento do processo pelo host; confirmação do término do trabalho remoto a verificar |
| Saída estruturada | A verificar | `--output-format json` ou `stream-json` |
| Uso/custo | A verificar | O resultado JSON reportaria tokens e custo estimado em USD; se é cobrança final ou estimativa local, a verificar |
| Modelo | A verificar | `--model` para o solicitado; modelo resolvido no resultado a verificar |
| ID de chamada | A verificar | ID de sessão no resultado; retomada de sessão como forma de reconciliação a verificar |

### Codex CLI

| Controle | Situação | Detalhe |
| --- | --- | --- |
| Modo não interativo | A verificar | Subcomando `codex exec` |
| Sandbox/permissões | A verificar | `--sandbox` com modos de somente leitura, escrita no workspace e acesso total; política de aprovação configurável; mecanismo de sandbox por sistema operacional a verificar |
| Timeout | A verificar | Nenhum timeout global conhecido; assumir enforcement pelo host |
| Cancelamento | A verificar | Encerramento do processo pelo host |
| Saída estruturada | A verificar | `--json` com eventos JSONL; opções para gravar a última mensagem e para impor schema de saída citadas na documentação, nomes exatos a verificar |
| Uso/custo | A verificar | Contagem de tokens em eventos; custo monetário provavelmente não reportado |
| Modelo | A verificar | `--model`/`-m` para o solicitado; resolvido a verificar |
| ID de chamada | A verificar | ID de sessão/thread nos eventos a verificar |

### Antigravity CLI (`agy`)

| Controle | Situação | Detalhe |
| --- | --- | --- |
| Modo não interativo | A verificar | Existência e forma de invocação desconhecidas pelo redator |
| Sandbox/permissões | A verificar | Desconhecido |
| Timeout | A verificar | Desconhecido; assumir enforcement pelo host |
| Cancelamento | A verificar | Desconhecido |
| Saída estruturada | A verificar | Desconhecido |
| Uso/custo | A verificar | Desconhecido |
| Modelo | A verificar | Desconhecido |
| ID de chamada | A verificar | Desconhecido |

### Kiro CLI

| Controle | Situação | Detalhe |
| --- | --- | --- |
| Modo não interativo | Observado indiretamente, a verificar | Mensagem do Kiro CLI citou `--no-interactive`; invocação completa (por exemplo, subcomando `chat`) a verificar |
| Sandbox/permissões | Parcialmente observado, a verificar | A mesma mensagem indica que, em modo não interativo, ferramentas sem confiança prévia falham em vez de pedir aprovação, e cita `--trust-all-tools`. Confiança por ferramenta específica a verificar. Confiar em todas as ferramentas é incompatível com negação por padrão |
| Timeout | A verificar | Assumir enforcement pelo host |
| Cancelamento | A verificar | Encerramento do processo pelo host |
| Saída estruturada | A verificar | Desconhecido |
| Uso/custo | A verificar | Desconhecido; unidade de cobrança pode não ser monetária |
| Modelo | A verificar | Seleção e reporte de modelo a verificar |
| ID de chamada | A verificar | Desconhecido |

## Riscos comuns aos adaptadores reais

- **Sandbox declarada não é sandbox verificada.** A sandbox de cada CLI cobre o processo do agente; o ORQ ainda precisa provar confinamento de caminho, symlink, rede e `.git` conforme a [ExecutionPolicy](../phase-1/execution-policy-contract.md). O HANDOFF registra falhas de `bwrap` no ambiente de desenvolvimento, o que indica que isolamento por namespaces pode nem estar disponível.
- **Credenciais do próprio runtime.** As CLIs usam login ou chave próprios. O ORQ não pode persistir esses valores e precisa declarar `credentials` no preflight sem lê-los.
- **Custo estimado pelo cliente.** Um valor de custo exibido pela CLI pode ser estimativa local. Ele entra como `estimated`, não `reported`, até evidência contrária.
- **Contexto oculto.** Arquivos de instrução do runtime (por exemplo AGENTS.md, CLAUDE.md ou equivalentes), memória e prompt de sistema entram no contexto sem passar pelo ContextManifest. A cobertura será `platform_only` ou `unknown`.
- **Versões móveis.** Flags e formatos mudam entre versões. O adaptador fixa a versão e trata saída fora do formato esperado como erro de integração, não como sucesso.
- **Terminação após kill.** Matar o processo local não prova que o trabalho remoto parou. Sem evidência, a tentativa vai para `unknown`.
- **Rede obrigatória.** Todo runtime real precisa falar com o provedor, o que exige NetworkPolicy com destino explícito e torna o primeiro teste real dependente de credencial e quota.

## Alternativas

| Alternativa | Vantagens | Limitações |
| --- | --- | --- |
| Começar por um adaptador real | Observa comportamento real cedo | Mistura erros do runner com variabilidade, custo, credencial e controles não verificados; viola G6 enquanto os controles forem `unknown` |
| Começar pelo executor simulado e definir a interface do adaptador | Prova o runner e todos os caminhos de falha de forma reproduzível, sem rede | Não comprova nada sobre runtimes reais |
| Implementar simulado e um adaptador real ao mesmo tempo | Valida a interface com dois clientes | Aumenta o incremento e exige credencial antes de o runner estar estável |

## Recomendação

1. O primeiro executor é o **simulado determinístico**. Ele é o único capaz de exercitar timeout sem confirmação, perda de acknowledgement, custo indisponível e divergência de digest sob demanda.
2. A Fase 6 define a **interface do adaptador** e o **checklist de verificação**, sem implementar adaptador real.
3. A escolha do primeiro adaptador real fica para depois do runner simulado e de uma sessão de verificação com versão fixada. Ordem provisória sugerida, só pela quantidade de controles documentados que o redator conhece: Claude Code e Codex CLI antes de Kiro CLI e Antigravity CLI. Essa ordem é hipótese, não evidência, e precisa de aprovação do dono.

## Interface do adaptador de executor

A interface é a mesma para o simulado e para os reais. O runner é síncrono e sequencial; as operações abaixo são chamadas em ordem pelo runner, nunca pelo modelo.

```text
ExecutorAdapter
  describe() -> ExecutorDescription
      executor_ref, adapter_version, runtime_ref, runtime_version,
      declarações de controle (support + evidence_refs), telemetria esperada

  dispatch(intent: DispatchIntent, bundle: bytes, workspace: WorkspaceHandle) -> DispatchAck
      precondição: evento dispatch_requested já persistido e sincronizado
      retorna external_call_id ou null; nunca bloqueia além do prazo de despacho

  observe(intent, until: instante UTC) -> ExecutionObservation
      status observado: running | finished | failed | unknown
      termination_certainty: confirmed | not_terminated | unknown

  cancel(intent, reason) -> CancelObservation
      termination_certainty após a tentativa de parada

  reconcile(dispatch_idempotency_key, external_call_id | null) -> ReconciliationObservation
      outcome: found_running | found_finished | found_failed | found_stopped | not_found | unknown
      found_stopped: execução encontrada parada, sem resultado utilizável; o runner
      decide entre timed_out, cancelled e failed pelo pedido de parada anterior
      not_found só é conclusivo se o adaptador declarar o controle
      reconcile_not_found_authoritative com evidência

  collect(intent) -> ExecutorResult
      produced_files (bytes + output_id declarado), usage, errors,
      requested_model_id | null (como o runtime informou), resolved_model_id | null,
      provider_context_coverage; o runner grava os três últimos em result_collected
```

Regras:

- O adaptador não decide retry, não altera estado do run e não grava no journal. Ele devolve observações; o runner as registra como eventos.
- Toda saída do adaptador é dado não confiável: o runner valida tamanho, digest, tipo e contrato antes de criar ArtifactRef ou output binding.
- Erro do adaptador é traduzido em ErrorRecord com `retryability` e `termination_certainty` explícitos. Na dúvida, ambos são `unknown`.
- Credenciais não passam pela interface. O adaptador real as obtém do ambiente do próprio runtime.

## Checklist de verificação de um adaptador real

Cada item produz um ArtifactRef de evidência (comando, versão, saída limitada e classificada) e só então uma CapabilityDeclaration `supported`.

| ID | Verificação | Evidência mínima |
| --- | --- | --- |
| V01 | Versão do runtime identificável e fixada | Saída do comando de versão |
| V02 | Execução sem terminal termina sem pedir entrada | Execução com stdin fechado e código de saída |
| V03 | Saída estruturada parseável na versão fixada | Amostra validada por schema do adaptador |
| V04 | Escrita fora do workspace é negada | Tentativa de escrita em caminho externo, symlink e `.git` |
| V05 | Rede fora da lista é negada | Tentativa contra destino não autorizado |
| V06 | Ferramenta não autorizada é negada sem prompt | Execução com ferramenta fora da lista |
| V07 | Timeout imposto pelo host encerra o processo e seus filhos | Árvore de processos antes e depois |
| V08 | Cancelamento é confirmável | Mesma evidência de V07 após cancel |
| V09 | Uso de tokens e custo: reportado, estimado ou indisponível | Amostra comparada com a fonte de cobrança, quando houver |
| V10 | Modelo resolvido é reportado | Campo na saída ou declaração de ausência |
| V11 | ID de chamada/sessão permite reconciliação | Consulta posterior pelo ID |
| V12 | Contexto adicional do runtime é identificável | Lista de arquivos de instrução e memória carregados |

Itens não verificados permanecem `unknown`; o Router já exclui destinos com controle obrigatório `unknown` (`control_unknown`).
