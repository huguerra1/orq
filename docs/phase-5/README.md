# Fase 5 — Model Router

Estado: concluída em 2026-09-29 como baseline local de roteamento. O adaptador Jev foi validado sem rede; nenhuma chamada real foi feita sem credencial explícita.

## Objetivo

Escolher um destino compatível para uma tentativa candidata, preservando todos os destinos considerados e separando segurança determinística de preferência.

## Alternativas e recomendação

| Modo | Vantagem | Limitação |
| --- | --- | --- |
| `fixed` | Baseline mínimo e reproduzível | Não se adapta à tarefa |
| `rules` | Auditável e local | A prioridade precisa ser mantida |
| `typed_decision` | Julga contexto não estruturado entre opções tipadas | Tem custo, latência, confiança e falhas externas |

Implementar na ordem `fixed`, `rules`, interface `DecisionEngine` e adaptador Jev. Todos usam o mesmo filtro obrigatório e produzem o mesmo contrato final.

## Fluxo

```text
TaskSpec + política + catálogo
        │
        ▼
filtro local de elegibilidade
        │ somente IDs elegíveis
        ▼
fixed | rules | DecisionEngine/Jev
        │ recomendação tipada
        ▼
limiar + fallback local
        │
        ▼
preflight → RoutingDecision final
```

O motor de decisão não recebe credenciais de executores, não altera política, não cria candidatos e não executa agentes.

## Incrementos

| Incremento | Entrega | Gate |
| --- | --- | --- |
| 5A | Schemas de catálogo, política, request e decisão | Meta-schema, fixtures e hashes válidos |
| 5B | Filtro, `fixed` e `rules` | Casos positivos/negativos e determinismo |
| 5C | `DecisionEngine` e transporte simulado | Alvo/confiança inválidos não atravessam o núcleo |
| 5D | Adaptador opcional TypeSafe/Jev | Testes sem rede e smoke real somente com chave explícita |

## Resultado

- Quatro schemas fechados: catálogo de destinos, política, solicitação e decisão.
- Snapshot e hash do resumo exato enviado ao ranking.
- Filtro local para estado, papel, tipo, capacidade, permissão, controle, saída, classificação e limites.
- Baselines `fixed` e `rules`, com desempate estável por prioridade e ID.
- Interface `DecisionEngine`, transporte simulado e validação central da resposta.
- Fallback aplicado somente pelo núcleo e apenas sobre destinos elegíveis.
- Adaptador `JevDecisionEngine` para `Choice` do SDK oficial TypeSafe 0.7.2.
- SDK carregado opcionalmente; segredo lido pelo próprio SDK do ambiente e nunca persistido pelo ORQ.
- 91 testes totais aprovados, incluindo o smoke MCP fora da sandbox; dependências íntegras.

O teste real do Jev permanece deliberadamente pendente até existir chave e autorização explícitas. Isso não invalida o contrato nem os testes do adaptador, mas também não comprova disponibilidade, quota, latência ou custo do serviço.

## Razões de exclusão iniciais

- `target_inactive`;
- `role_unsupported`;
- `task_type_unsupported`;
- `capability_missing` ou `capability_unknown`;
- `permission_missing`;
- `control_missing` ou `control_unknown`;
- `output_contract_unsupported`;
- `classification_unsupported`;
- `classification_unknown`;
- `context_limit_unknown` ou `context_limit_exceeded`;
- `output_limit_unknown` ou `output_limit_exceeded`.

Cada razão identifica o requisito afetado e preserva referências de evidência quando disponíveis. Pontuação nunca remove uma exclusão.

Após o filtro, o ranking recebe descrição editorial e tags de especialização do destino, além das referências de modelo/executor. Esses atributos ajudam a preferência, mas não provam capacidade e nunca removem uma exclusão.

## Testes exigidos

1. destino ativo e compatível permanece elegível;
2. cada requisito obrigatório ausente/unknown exclui o destino;
3. `fixed` inelegível não é selecionado;
4. `rules` desempata por ID de forma estável;
5. o motor recebe apenas IDs elegíveis;
6. resposta fora do conjunto é `invalid_response`;
7. confiança ausente ou abaixo do limiar segue a ação configurada;
8. erro externo usa somente fallback local elegível;
9. preflight reprovado não produz seleção efetiva;
10. alteração do request ou registro invalida o hash.

Quando há destinos elegíveis, mas o alvo imposto pelo modo `fixed` não pertence a esse conjunto, o estado é `policy_rejected`; `no_eligible_target` fica reservado à ausência real de destinos elegíveis.

## Fora deste incremento

- despacho e consumo de `max_attempts`;
- credenciais reais e smoke Jev no CI;
- métricas históricas e otimização de custo/latência;
- disponibilidade externa, quota e reconciliação;
- runner do DAG, que começa na Fase 6.

## Próxima fase

A Fase 6 deve definir primeiro o executor inicial, o armazenamento append-only e a máquina sequencial de estados. A RoutingDecision selecionada continua sem autoridade de despacho até esses gates existirem.

Decisão completa: [ADR 0006](../adr/0006-router-boundary-and-typed-decision.md).
