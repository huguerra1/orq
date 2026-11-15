---
title: "Cooldown de cota e fallback"
language: pt-BR
topics: [cota, cooldown, roteamento]
---
# Cooldown de cota e fallback

Quando a mensagem de limite não traz horário de reset, `reset_at` fica `null` e vale `blocked_until = observed_at + default_cooldown_ms`.

- O padrão é de 60 minutos: `default_cooldown_ms = 3600000`.
- O registro leva `reset_source: "cooldown_policy"`; as demais origens são `cli_reported` e `clock_expired`.
- Depois de um cooldown por política, o destino só recebe tarefa real após uma sondagem leve.
- Se todas as famílias elegíveis estão bloqueadas, o Router devolve `decision_status: "no_eligible_target"` com o motivo `all_eligible_families_quota_exhausted`.
- Texto livre de stdout não é fonte de cota.
