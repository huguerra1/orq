---
title: "Custo desconhecido nunca vira zero"
language: pt-BR
topics: [custo, uso, assinaturas]
---
# Custo desconhecido nunca vira zero

Em assinaturas, o `UsageRecord` mantém `cost.status = "unavailable"` e `cost.amount = null`; o valor nunca é substituído por `"0"`.

O campo `total_cost_usd` do envelope JSON do Claude Code é uma equivalência de API calculada pela CLI, não uma cobrança real. Ele pode ser guardado só como anotação informativa do adaptador.
