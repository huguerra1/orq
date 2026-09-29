# ADR 0005 — Interface do planner e baseline fixo

Estado: aceito em 2026-09-29.

## Contexto

Um planner adaptativo é desejável, mas implementá-lo primeiro impediria separar erros do contrato, variância do modelo e benefício real da decomposição. Também daria à saída do modelo autoridade indevida para se autoaprovar.

## Decisão

- Definir interface de planner independente de provedor.
- Implementar primeiro `FixedTemplatePlanner`, que resolve somente templates versionados e íntegros.
- Exigir coincidência exata de projeto e objetivo; o baseline não faz interpolação.
- Aplicar validação estrutural e semântica no wrapper central após a proposta.
- Separar WorkflowSpec candidato, workflow aceito e PlanningRecord.
- Preservar falhas e uso conhecido; ausência de chamada externa gera lista vazia, não UsageRecord inventado.

## Consequências

- Há um baseline determinístico para comparar regras e planner por modelo.
- O primeiro planner tem cobertura limitada por desenho.
- Templates precisam ser publicados com versão e digest.
- Um futuro adaptador de modelo só propõe; não aceita plano, não escolhe destino e não executa tarefa.

## Próxima decisão

Após este baseline, avaliar `rules` versus planner por modelo usando os mesmos requests, validadores, limites e métricas. TypeSafe/Jev pertence ao Model Router, não ao planner inicial.
