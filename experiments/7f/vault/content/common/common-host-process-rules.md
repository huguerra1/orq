---
title: "Regras de processo do host"
language: pt-BR
topics: [processo, timeout, execucao]
---
# Regras de processo do host

O host lança cada CLI sem shell (`shell=False`, `argv` explícito), com stdin fechado e em workspace novo por tentativa.

- O timeout é imposto pelo host (`attempt_timeout_ms`): nenhuma CLI observada encerra sozinha uma tarefa longa.
- Cancelamento e timeout encerram toda a árvore de processos antes de qualquer retry.
