# ADR 0006 — Fronteira do Router e decisão tipada

Estado: aceito em 2026-09-29.

## Contexto

O ORQ precisa comparar roteamento fixo, regras e decisão probabilística sem permitir que um modelo amplie permissões, invente destinos ou despache uma tarefa. O contrato v0.1 também exige separar papel, modelo, provedor, runtime e executor.

O SDK Python oficial da TypeSafe AI expõe `Choice` e `TypeSafeClient.system_one`. Em 2026-09-29, a versão publicada no repositório oficial é 0.7.2 e o exemplo lê o resultado por `response.choices[id]`. Essa superfície é externa e pode evoluir; ela não deve contaminar o contrato central do ORQ.

## Decisão

- O Router recebe um snapshot fechado do catálogo e uma solicitação identificada.
- Elegibilidade é sempre calculada localmente antes do ranking.
- Os baselines `fixed` e `rules` são implementados primeiro e não fazem chamadas externas.
- `DecisionEngine` recebe somente o resumo autorizado e a lista ordenada de IDs elegíveis.
- O resultado do motor é uma recomendação tipada, nunca uma autorização ou um despacho.
- O Orchestrator valida alvo, confiança e política de fallback e finaliza a `RoutingDecision` somente após os checks de preflight.
- TypeSafe/Jev entra por um adaptador opcional. A credencial vem do ambiente do processo e nunca é persistida.
- A versão do SDK, o modelo solicitado/resolvido e a chamada externa devem ser registrados quando conhecidos; valores ausentes permanecem desconhecidos.

## Contratos do primeiro incremento

- `RoutingTargetCatalog`: destinos compostos, perfis referenciados, capacidades, contratos de saída, classificação e limites declarados.
- `RoutingPolicy`: modo, regras estáveis, limiar e ações de fallback.
- `RoutingRequest`: snapshot da tarefa, referências imutáveis, orçamento preliminar de contexto e permissões/controles requeridos.
- `RoutingDecision`: candidatos considerados, recomendação bruta, seleção efetiva, preflight, uso e hash canônico.

O catálogo não substitui os perfis detalhados futuros; ele é um snapshot de roteamento que preserva suas referências e declarações necessárias à decisão.

Descrição e tags de especialização são sinais editoriais permitidos somente no ranking. Não substituem declaração de capacidade, evidência ou métrica observada.

## Elegibilidade e ordem

Um destino só é elegível quando está ativo e atende ao papel, tipo de tarefa, capacidades, permissões, controles, contratos de saída, classificação e limites conhecidos. `unknown` não satisfaz requisito obrigatório.

No modo `fixed`, o alvo configurado é usado apenas se elegível. No modo `rules`, a ordenação usa prioridade inteira crescente e `target_id` como desempate estável. Métricas históricas e custo ficam fora desta primeira regra para não transformar ausência de evidência em pontuação favorável.

## Falhas e fallback

Resposta ausente, alvo fora do conjunto, confiança ausente/baixa e erro do provedor são estados distintos. O fallback é aplicado pelo núcleo e precisa continuar dentro do conjunto elegível. Nenhuma falha do Router consome tentativa da tarefa antes da admissão.

## Consequências

- É possível medir o ganho do Jev contra dois baselines reproduzíveis.
- O adaptador externo pode mudar sem alterar a semântica da decisão.
- O primeiro catálogo repete apenas os fatos necessários ao roteamento; validadores futuros poderão resolvê-los diretamente dos perfis completos.
- Uma decisão selecionada ainda não autoriza execução: preflight e admissão permanecem gates separados.

## Referência externa

- [SDK Python oficial da TypeSafe AI](https://github.com/typesafe-ai/typesafe-sdk-python), consultado em 2026-09-29.
