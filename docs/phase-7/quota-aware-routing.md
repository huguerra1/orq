# Roteamento ciente de cota e fallback entre famílias

Estado: Proposta em 2026-11-14 ([ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md)). Estende a [decisão de roteamento](../phase-1/routing-context-contract.md), a [fronteira do Router](../adr/0006-router-boundary-and-typed-decision.md) e os [contratos de execução da Fase 6](../phase-6/run-contracts.md).

## Problema

Em APIs tarifadas por token, o limite operacional principal é o orçamento financeiro (`BudgetPolicy`). Já nas quatro assinaturas pessoais utilizadas na Fase 7 (ChatGPT Plus, Claude Enterprise, Gemini e Kiro Power), o pagamento é fixo e o limite operacional real é a **cota de uso por janela de tempo** (mensagens, tokens ou créditos por período de horas, dia ou mês).

Essas CLIs não oferecem um endpoint padrão e uniforme para consultar o saldo exato antes de cada chamada. Na prática, o esgotamento da cota só é descoberto quando a CLI recusa ou interrompe a execução e imprime uma mensagem indicando o bloqueio e, frequentemente, o horário em que o uso será liberado. Se o Orchestrator tratar essa falha como um erro transitório comum na mesma assinatura, esgotará `max_attempts` batendo repetidamente em um destino bloqueado. Se tratar o custo da assinatura como zero, violará o invariante central da Fase 1 (`unknown` nunca é zero).

## Detecção de limite de uso pela saída da CLI

Cada adaptador de assinatura inclui um detector determinístico e versionado (`QuotaLimitDetector`) que inspeciona o código de retorno, o `stdout` e o `stderr` ao final da chamada da CLI.

### Exemplo real observado (Codex CLI)

Em uso real do Codex CLI sob assinatura ChatGPT Plus, o bloqueio de janela emite na saída:

```text
You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/plus) or try again at 10:05 PM.
```

E, em formatos de 24 horas ou mensagens equivalentes de janela:

```text
You've hit your usage limit ... try again at 22:05
```

### Regras de detecção e extração de horário

1. **Gramáticas por adaptador (`detector_version`):** cada adaptador mantém expressões regulares ancoradas e testadas contra fixtures reais da respectiva CLI:
   - **Codex CLI:** padrões `"You've hit your usage limit"` / `"usage limit"` seguidos opcionalmente por `"try again at <HH:MM> [AM|PM]"` ou data/hora explícita;
   - **Claude Code:** padrões de limite de sessão/janela (`"rate limit"`, `"usage limit"`, `"limit reached"`, `"resets at <horário>"`);
   - **Gemini CLI (`agy`):** mensagens de exaustão de quota (`"RESOURCE_EXHAUSTED"`, `"quota exceeded"`, `"usage limit"`);
   - **Kiro CLI:** mensagens de limite de créditos/uso da assinatura Kiro Power (`"usage limit"`, `"credit limit"`, `"monthly limit"`).
2. **Proteção contra falso positivo por conteúdo do projeto:** a detecção só é acionada quando a invocação da CLI falha (código de saída diferente de zero ou ausência de resposta estruturada final de conclusão) **e** o padrão aparece no canal de erro/status da própria CLI, nunca dentro do conteúdo de arquivos lidos pelo agente.
3. **Resolução do instante de reset (`reset_at`):**
   - Quando a mensagem informa um horário de relógio (como `10:05 PM` ou `22:05`), o detector resolve esse horário no fuso horário configurado do ambiente do host (por exemplo, `America/Sao_Paulo`, `-03:00`) relativo ao instante observado `observed_at` do relógio do runner: se `22:05` do mesmo dia for posterior a `observed_at`, usa o mesmo dia; se já tiver passado (por exemplo, chamada às `23:10` com reset à `01:05 AM`), usa o dia seguinte, convertendo o resultado para `utcTimestamp` (`reset_at`).
   - Quando a mensagem confirma o limite de uso mas **não** informa horário de reset, `reset_at` permanece `null` e o Orchestrator aplica `blocked_until = observed_at + default_cooldown_ms` (valor definido na política do run), registrando `reset_source: "cooldown_policy"` em vez de `"cli_reported"`.

## Estado de disponibilidade por destino até o reset

O estado de cota é mantido pelo runner em um registro auditável (`QuotaStateSnapshot`) indexado por `quota_pool_id` (identificando a assinatura ou sub-janela compartilhada entre destinos da mesma família):

| Estado | Significado | Efeito no roteamento e preflight |
| --- | --- | --- |
| `available` | Nenhuma restrição de cota ativa no instante `now` | Destino elegível para seleção e aprovação no check `quota` |
| `quota_exhausted` | Limite reportado pela CLI com `reset_at` conhecido (`now < reset_at`) | Destino **indisponível** até `reset_at`; excluído antes da seleção ou reprovado no check `quota` |
| `cooldown` | Limite reportado pela CLI sem horário exato (`now < blocked_until`) | Destino **indisponível** até `blocked_until`; mesmo efeito de bloqueio temporário |
| `unknown` | Estado inicial antes de qualquer falha de cota, ou quando a observação foi inconclusiva | Permitido quando a política não exigir saldo prévio comprovado; qualquer bloqueio observado transita para `quota_exhausted` ou `cooldown` |

Quando o relógio do runner atinge `now >= blocked_until` (onde `blocked_until` é `reset_at` ou o fim do cooldown), o pool volta ao estado `available`, emitindo o evento `quota_reset_observed` no journal antes da próxima decisão de roteamento.

## Fallback para outra família

O roteamento ciente de cota opera em dois momentos complementares, sempre respeitando a [ADR 0006](../adr/0006-router-boundary-and-typed-decision.md) e a máquina de estados da Fase 6:

### 1. Durante a seleção e o preflight (antes do despacho)

1. Ao preparar uma tentativa, o Router consulta o `QuotaStateSnapshot` vigente no instante `now`.
2. Qualquer `ExecutionTarget` cujo `quota_pool_id` esteja em `quota_exhausted` ou `cooldown` com `blocked_until > now` é marcado como inelegível pelo motivo determinístico `quota_exhausted` (registrando a referência ao evento que bloqueou a cota).
3. O baseline `rules` (ou `fixed` com lista de fallback ordenada) seleciona o próximo destino elegível pertencente a **outra família de assinatura** (por exemplo, se `family_codex` atingiu o limite até `22:05`, seleciona `family_claude`, `family_gemini` ou `family_kiro`).
4. A `RoutingDecision` registra o candidato original bloqueado em `candidates` (com `eligible: false`, `reasons: ["quota_exhausted"]`) e, quando aplicável, preenche `effective_selection` com `used_fallback: true` e razão `quota_fallback`.

### 2. Durante a execução de uma tentativa (após o despacho)

Quando a cota se esgota exatamente na chamada em curso:

1. A CLI encerra sem concluir a tarefa e imprime o aviso de limite (ex.: `"You've hit your usage limit ... try again at 10:05 PM"`).
2. `collect()` devolve um `ErrorRecord` com `error_code: "quota_limit_reached"`, `retryability: "retryable"`, `termination_certainty: "confirmed"` e os metadados extraídos pelo `QuotaLimitDetector` (`quota_pool_id`, `raw_excerpt`, `reset_at`, `blocked_until`).
3. O runner grava no journal o evento `quota_exhausted_recorded`, atualizando o estado do `quota_pool_id` para indisponível até `reset_at` (`blocked_until`).
4. A tentativa atual transita para `failed` (`quota_limit_reached`). Como o término da CLI é confirmado e o erro é `retryable`, a tarefa segue para `retry_wait → ready` (desde que `retry_policy.on_retryable_error` seja `true` e ainda reste saldo em `max_attempts`, ou conforme a política de não contabilizar recusa imediata de cota se o dono assim decidir na revisão).
5. Na nova tentativa, a nova `RoutingDecision` já enxerga o destino anterior bloqueado até `reset_at` e escolhe outra família elegível.
6. **Invariante de contexto no fallback de família:** como a tentativa seguinte mudará de família (por exemplo, de `family_codex` para `family_claude`), o runner **obrigatoriamente executa nova seleção de conhecimento sob o RLS da nova família** e materializa um novo `ContextManifest` e um novo bundle JCS. Unidades exclusivas da família anterior não vazam para a família substituta.

Se todas as famílias elegíveis para a tarefa estiverem simultaneamente em `quota_exhausted`/`cooldown`, o Router retorna `decision_status: "no_eligible_target"` (motivo `all_eligible_families_quota_exhausted`), a admissão é rejeitada sem despacho e o run encerra de forma limpa e auditável.

## Registro de consumo de cota e regra estrita de custo monetário

### Consumo de cota (`QuotaConsumptionRecord`)

Embora assinaturas não gerem fatura por chamada em moeda, elas consomem capacidade finita da janela do usuário. Para cada tentativa despachada em um adaptador de assinatura, o runner registra:

- `messages_consumed`: inteiro não negativo (tipicamente `1` por invocação não interativa enviada ao provedor);
- `input_tokens_reported` e `output_tokens_reported`: inteiros ou `null`, conforme reportado pela CLI (como no fim do log do Codex CLI ou no JSON do Claude Code);
- `credit_multiplier`: decimal em texto ou `null` (usado no Kiro CLI conforme `kiro-cli chat --list-models`);
- `quota_outcome`: `"accepted"`, `"quota_exhausted_on_call"` ou `"unknown"`.

### Custo monetário `unavailable` e nunca zero

O contrato da Fase 1 ([execution-policy-contract.md](../phase-1/execution-policy-contract.md#budgetpolicy)) e da Fase 6 ([run-contracts.md](../phase-6/run-contracts.md)) permanece intacto:

1. **Nunca zero:** o custo monetário de uma chamada feita por assinatura pessoal **não é zero** (`"0"` ou `"0.00"`), porque a assinatura tem preço pago pelo titular e o custo marginal unitário exato por tentativa é desconhecido/não faturado individualmente.
2. **Representação obrigatória:** toda reserva (`budget_reserved`) e todo `UsageRecord` gerados por adaptadores de assinatura trazem obrigatoriamente:
   ```json
   {
     "status": "unavailable",
     "amount": null,
     "currency": null
   }
   ```
3. **Estimativas locais de CLIs:** se uma CLI imprimir uma estimativa em dólares equivalente à sua API comercial, esse número **não** entra em `UsageRecord.cost.amount`. No `RunSummary`, as tentativas por assinatura incrementam `unavailable_count` e mantêm `total_cost_exact: false`. O validador estrutural do 6A já rejeita qualquer combinação de `status: "unavailable"` com `amount: "0"`.

## Contratos propostos (Incremento 7A)

### `SubscriptionProfile`

Descreve uma assinatura pessoal associada a um ou mais `ExecutionTargets`:

| Campo | Tipo | Obrigatório | Semântica |
| --- | --- | --- | --- |
| `schema_version` | `"0.1"` | Sim | Versão do contrato |
| `subscription_id` | identifier | Sim | Ex.: `sub-chatgpt-plus`, `sub-claude-enterprise`, `sub-gemini`, `sub-kiro-power` |
| `family_id` | identifier | Sim | Família associada (`family_codex`, `family_claude`, `family_gemini`, `family_kiro`) |
| `cli_executable` | nonEmptyText | Sim | Binário oficial esperado (`codex`, `claude`, `agy`, `kiro-cli`) |
| `quota_pool_id` | identifier | Sim | Identificador da janela de cota compartilhada |
| `billing_mode` | `"personal_subscription"` | Sim | Fixa que o custo monetário por tentativa é `unavailable` |
| `default_cooldown_ms` | positiveInteger | Sim | Janela de bloqueio quando a mensagem de limite não trouxer horário de reset |
| `local_timezone` | nonEmptyText | Sim | Fuso IANA usado para interpretar horários locais como `10:05 PM` (ex.: `America/Sao_Paulo`) |
| `detector_version` | nonEmptyText | Sim | Versão do `QuotaLimitDetector` do adaptador |
| `record_hash` | digest | Sim | SHA-256 JCS do registro sem `record_hash` |

### `QuotaStateRecord` (payload de `quota_exhausted_recorded` e `quota_reset_observed`)

| Campo | Tipo | Obrigatório | Semântica |
| --- | --- | --- | --- |
| `schema_version` | `"0.1"` | Sim | |
| `quota_event_id` | identifier | Sim | Único no run |
| `run_id` | identifier | Sim | Run em que o estado foi observado |
| `quota_pool_id` | identifier | Sim | Pool de assinatura afetado |
| `subscription_id` | identifier | Sim | Referência à assinatura |
| `family_id` | identifier | Sim | Família afetada |
| `source_attempt_id` | identifier ou `null` | Sim | Tentativa que disparou o bloqueio; `null` em `quota_reset_observed` |
| `status` | enum | Sim | `available`, `quota_exhausted` ou `cooldown` |
| `observed_at` | utcTimestamp | Sim | Instante do relógio do runner |
| `reset_at` | utcTimestamp ou `null` | Sim | Instante UTC calculado a partir da saída da CLI (ex.: `10:05 PM` convertido para UTC) |
| `blocked_until` | utcTimestamp ou `null` | Sim | Igual a `reset_at` quando presente, ou `observed_at + default_cooldown_ms` em `cooldown` |
| `reset_source` | enum | Sim | `cli_reported`, `cooldown_policy` ou `clock_expired` |
| `raw_message_excerpt` | texto (até 512 chars) ou `null` | Sim | Trecho sanitizado da mensagem de limite da CLI |
| `record_hash` | digest | Sim | SHA-256 JCS |

### `QuotaConsumptionRecord`

| Campo | Tipo | Obrigatório | Semântica |
| --- | --- | --- | --- |
| `schema_version` | `"0.1"` | Sim | |
| `quota_consumption_id` | identifier | Sim | Único no run |
| `run_id`, `task_id`, `attempt_id` | identifier | Sim | Vínculo exato com a tentativa |
| `subscription_id`, `quota_pool_id`, `family_id` | identifier | Sim | Identificação da assinatura e família |
| `usage_record_ref` | versionedRef | Sim | Referência ao `UsageRecord` da tentativa (cujo `cost.status` é obrigatoriamente `unavailable`) |
| `messages_consumed` | nonNegativeInteger | Sim | Chamadas de CLI contabilizadas contra a janela |
| `input_tokens`, `output_tokens` | nonNegativeInteger ou `null` | Sim | Tokens reportados pela CLI ou `null` |
| `credit_multiplier` | decimal textual ou `null` | Sim | Multiplicador de crédito quando exposto pela CLI (ex.: Kiro) |
| `quota_outcome` | enum | Sim | `accepted`, `quota_exhausted_on_call` ou `unknown` |
| `recorded_at` | utcTimestamp | Sim | Instante do registro |
| `record_hash` | digest | Sim | SHA-256 JCS |
