---
title: "Escopo de leitura e Row-Level Security"
language: pt-BR
topics: [rls, escopo, memoria]
---
# Escopo de leitura e Row-Level Security

A leitura sob RLS exige quatro variáveis fixadas pelo host em transação `READ ONLY`: `app.catalog_id`, `app.current_project_id`, `app.current_family_id` e `app.current_role_id`.

- Os valores vêm do `RunManifest` e da `RoutingDecision`, nunca dos argumentos do agente.
- Escopo ausente ou incompleto não lê nada.
- As roles de leitura não têm `SUPERUSER` nem `BYPASSRLS`.
