# Fase 4 — Task Planner e admissão do plano

Estado: especificação inicial aceita; implementação ainda não iniciada.

## Problema

O ORQ precisa transformar uma intenção de run em um WorkflowSpec candidato sem confundir geração com aceitação. Um planner pode devolver texto, estrutura inválida, referências inexistentes ou um DAG cíclico; nenhum desses resultados autoriza execução.

## Alternativas

| Modo | Vantagens | Limitações |
| --- | --- | --- |
| `fixed_template` | Reproduzível, local e sem chamada de modelo | Só cobre objetivos previamente preparados |
| `rules` | Expansão determinística e parametrizável | Exige linguagem/regras próprias |
| `model` | Adapta estrutura ao objetivo | Custo, latência, variância e correções |

O baseline inicial será `fixed_template`, atrás da mesma interface que futuramente receberá `rules` e `model`.

## PlanningRequest

| Campo | Semântica |
| --- | --- |
| `planning_request_id` | Identidade da operação no run |
| `run_id` | Run já registrado por RunIntent |
| `project_id` | Projeto autorizado |
| `objective` | Objetivo exato a planejar |
| `workflow_template_ref` | Template imutável solicitado |
| `planner_policy_ref` | Política de modo, limites e correções |
| `created_at` | Instante UTC da solicitação |

No baseline fixo, projeto e objetivo precisam coincidir exatamente com o template. Não há substituição textual, merge ou preenchimento implícito.

## PlanningRecord

O registro imutável contém request e hash, planner/mode, template considerado, referência/hash do WorkflowSpec proposto quando houver, relatório de validação, usage refs, status, horários e hash canônico.

Status iniciais:

- `accepted`: proposta existe e passou em todos os gates;
- `rejected`: proposta foi produzida, mas é estrutural ou semanticamente inválida;
- `not_applicable`: template não corresponde ao projeto/objetivo;
- `error`: template ausente, hash divergente ou falha interna identificada.

O WorkflowSpec aceito é retornado separadamente do registro. Alterá-lo cria outra versão e outro planejamento; PlanningRecord não é editado.

## Interface

```text
Planner.propose(PlanningRequest) -> WorkflowSpec candidato ou erro identificado
plan_and_validate(planner, request) -> PlanningResult(record, workflow | null)
```

O wrapper, não o adaptador, aplica `validate_workflow`, calcula hashes e finaliza status. Assim um planner por modelo não pode declarar a própria saída válida.

## Limites e custos

- `fixed_template` não produz UsageRecord de modelo e registra lista vazia de usage refs.
- Ausência de chamada não deve ser confundida com custo reportado igual a zero.
- Chamadas futuras obedecem `max_planner_calls_per_run`, orçamento, timeout e política de rede.
- Correção de plano é outra chamada/registro e nunca reescreve a proposta anterior.

## Testes exigidos

1. template válido e correspondente é aceito com hash reproduzível;
2. template com ciclo ou referência inválida é rejeitado;
3. digest divergente produz erro, não nova versão silenciosa;
4. projeto ou objetivo diferente produz `not_applicable`;
5. template ausente produz erro identificado;
6. mesmo request e catálogo de templates geram o mesmo conteúdo normativo do registro;
7. adaptador não consegue ignorar o relatório do validador central.

## Fora desta fase

- geração por LLM e reparo automático;
- escolha de agente/modelo;
- mutação do DAG durante execução;
- decomposição concorrente ou scheduling adaptativo.
