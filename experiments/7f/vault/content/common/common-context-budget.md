---
title: "Orçamento de contexto"
language: pt-BR
topics: [orcamento-de-contexto, tokens]
---
# Orçamento de contexto

Os tokens do contexto são estimados por `char4-v1`: `ceil(caracteres / 4)`.

- Cobertura obrigatória não atendida gera `mandatory_requirement_uncovered`; estouro de orçamento gera `context_budget_exceeded`.
- A regra de cobertura é `at_least_one` ou `all_source_refs`.
- Não há truncamento silencioso.
