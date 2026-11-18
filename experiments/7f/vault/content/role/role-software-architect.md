---
title: "Guia do papel software_architect"
language: pt-BR
topics: [arquitetura, unidade-de-comparacao, cobertura-de-contexto]
---
# Guia do papel software_architect

A unidade de comparação declarada no experimento é a família/runtime completo (CLI + modelo + ferramentas).

- Registre `executor_ref`, `runtime_version`, `requested_model_id` e `resolved_model_id`.
- `provider_context_coverage` nunca é `complete`: o máximo honesto é `platform_only` (arquivos locais de instrução sob controle do workspace limpo); sem isso, `unknown`.
