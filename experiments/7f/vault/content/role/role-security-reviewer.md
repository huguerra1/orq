---
title: "Guia do papel security_reviewer"
language: pt-BR
topics: [revisao, seguranca, checklist]
---
# Guia do papel security_reviewer

Checklist de revisão de segurança do hub de assinaturas:

- `--dangerously-skip-permissions` (agy) e `--trust-all-tools` (Kiro) nunca são usadas.
- Nenhum adaptador declara `provider_context_coverage: "complete"`.
- A rede é negada fora da política (`WebFetch` e `WebSearch` negados no Claude Code).
- `execute_bash` no Kiro é uma concessão ampla.
