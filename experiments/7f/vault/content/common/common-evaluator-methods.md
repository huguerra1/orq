---
title: "Métodos do avaliador determinístico"
language: pt-BR
topics: [avaliacao, metodos-de-avaliacao]
---
# Métodos do avaliador determinístico

O avaliador determinístico conhece quatro métodos: `output_binding_present`, `digest_matches`, `content_contract_valid` e `simulated_verdict`.

- Um método desconhecido não é executado: o critério fica `not_run`.
- `simulated_verdict` depende do roteiro do executor simulado.
- A métrica primária nunca usa LLM como juiz.
