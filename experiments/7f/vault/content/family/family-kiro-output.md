---
title: "Kiro CLI: saída e créditos"
language: pt-BR
topics: [saida, creditos, custo, ruido]
---
# Kiro CLI: saída e créditos

O stdout do Kiro traz linhas de progresso `[tool] ...` antes da resposta final; o parser separa a trilha de ferramentas da resposta.

- `kiro-cli chat --list-models` mostra o multiplicador de créditos de cada modelo, registrado no `QuotaConsumptionRecord`.
- O custo monetário no `UsageRecord` permanece `unavailable` com `amount: null`.
