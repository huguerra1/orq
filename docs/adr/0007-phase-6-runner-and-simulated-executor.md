# ADR 0007 — Runner sequencial e executor simulado

Estado: Proposta em 2026-11-10. Aguarda revisão do dono; nada desta decisão foi implementado.

## Contexto

As Fases 4 e 5 entregam WorkflowSpec aceito e RoutingDecision finalizada, mas nenhum componente admite tentativas, persiste intenções, observa término ou avalia artefatos. Os gates G5 (persistência local) e G6 (primeiro executor, sandbox e enforcement) da [revisão cruzada](../phase-1/cross-review.md) continuam abertos.

Nenhuma CLI de agente candidata (Claude Code, Codex CLI, Antigravity CLI, Kiro CLI) teve seus controles verificados pelo ORQ. Pelas regras dos [perfis](../phase-1/agent-profiles.md), todos começam como `unknown` e seriam excluídos pelo Router em qualquer tarefa que exija esses controles. Detalhes em [executor-choice.md](../phase-6/executor-choice.md).

O código atual também tem uma tensão de ordem: `route_and_finalize` exige `preflight_checks` antes de finalizar a decisão, e `materialize_knowledge_context` exige a referência com digest da decisão já finalizada.

## Alternativas

| Alternativa | Vantagens | Limitações |
| --- | --- | --- |
| Adaptador real primeiro | Observa um runtime real cedo | Controles `unknown`; exige credencial, rede e custo antes de o runner existir; erros do runner se misturam com variabilidade do provedor |
| Executor simulado primeiro, com interface de adaptador definida | Reproduzível, sem rede; permite forçar timeout sem confirmação, ack perdido, custo indisponível e divergência de digest | Não prova nada sobre runtimes reais |
| Simulado e adaptador real juntos | Interface validada por dois clientes | Incremento grande; depende de credencial |

Para persistência: JSONL encadeado por digest, SQLite da biblioteca padrão, ou serviço de event store. Para a ordem roteamento/contexto: checks dependentes do bundle no DispatchIntent, finalização da decisão em duas etapas, ou regravação do manifesto.

## Decisão proposta

1. O primeiro executor é um **executor simulado determinístico**, roteirizado por fixture versionada e com relógio injetado.
2. A Fase 6 define a interface `ExecutorAdapter` (`describe`, `dispatch`, `observe`, `cancel`, `reconcile`, `collect`) e o checklist V01–V12 de verificação de controles. Nenhum adaptador real é implementado nesta fase.
3. O armazenamento é um **journal JSONL por run**, uma linha JCS por evento, encadeado por `previous_event_hash`/`event_hash`, com `fsync` antes de cada efeito e artefatos endereçados por SHA-256.
4. O cabeçalho da tentativa e o DispatchIntent são gravados no mesmo evento `dispatch_requested`, o que dá a atomicidade exigida pela Fase 1 sem banco transacional.
5. Os checks que dependem do bundle materializado ficam em `DispatchIntent.admission_checks`. A RoutingDecision mantém os checks disponíveis antes da materialização. A autoridade de despacho é o DispatchIntent.
6. O runner é síncrono, com concorrência `1` e desempate pela ordem de `WorkflowSpec.tasks`.
7. O avaliador é determinístico e local, com métodos fechados (`output_binding_present`, `digest_matches`, `content_contract_valid`, `simulated_verdict`); método desconhecido produz `not_run`.
8. Os limites usados pelo runner ficam em `RunManifest.resolved_limits`, incluindo o novo `retry_policy`. O schema completo de ExecutionPolicy fica fora da fase.
9. O journal ganha o agregado `task`.

## Consequências

- O fluxo `planner → router → contexto → tentativa → avaliação` pode ser provado com testes reproduzíveis, inclusive digest final idêntico entre execuções.
- Todos os caminhos de falha do ciclo da Fase 1 ficam testáveis sem provedor.
- A escolha do primeiro adaptador real é adiada até existir runner estável e uma sessão de verificação com versão fixada.
- O texto da Fase 1 sobre `preflight_checks` de contexto na RoutingDecision passa a ter uma realização diferente; a mudança precisa ser registrada no contrato se aprovada.
- Um escritor por run. Concorrência real (Fase 11) provavelmente exigirá outro backend, como SQLite.
- O simulado não exercita sandbox de filesystem nem rede; esses controles continuam `unknown` para qualquer destino real.

## Riscos e testes

| Risco | Mitigação | Cenários |
| --- | --- | --- |
| Duplo despacho após crash | Intenção sincronizada antes do efeito; reconciliação pela mesma chave | S28, S31, S34 |
| Journal corrompido | Cadeia de digests; cauda truncada descartada com registro; corrupção interna bloqueia | S16, S36 |
| Custo desconhecido virar zero | `amount: null` obrigatório para `unavailable`; agregação com `total_cost_exact` | S41–S46 |
| Simulado divergir de runtimes reais | Interface única e checklist de verificação antes do primeiro adaptador | Checklist V01–V12 |
| Avaliador aprovar sem evidência | Métodos fechados; `not_run` para método desconhecido; digests relidos | S14, S25 |

## Decisões que dependem do dono

- Aceitar o executor simulado como primeiro executor e adiar o adaptador real.
- Aceitar JSONL encadeado em vez de SQLite.
- Aceitar `admission_checks` no DispatchIntent (alternativa A em [runner-state-machine.md](../phase-6/runner-state-machine.md#divergência-em-relação-à-fase-1)).
- Aceitar `retry_policy` e o agregado `task`.
- Aceitar que rejeição de admissão seja definitiva na Fase 6.
- Aprovar ou recusar o subcomando de CLI para execução simulada.
- Definir a ordem do primeiro adaptador real após verificação.
