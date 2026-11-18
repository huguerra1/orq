---
title: "Kiro CLI: revisão de segurança"
language: pt-BR
topics: [revisao, seguranca, kiro, permissoes]
---
# Kiro CLI: revisão de segurança

Na revisão de segurança com o Kiro, recuse `--trust-all-tools` e confirme que `execute_bash` só aparece em `--trust-tools` quando a `CommandPolicy` da tarefa o autoriza, lembrando que a concessão é ampla.
