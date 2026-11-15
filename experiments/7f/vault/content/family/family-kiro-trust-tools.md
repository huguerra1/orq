---
title: "Kiro CLI: ferramentas confiáveis"
language: pt-BR
topics: [permissoes, ferramentas, trust-tools, somente-leitura, revisao]
---
# Kiro CLI: ferramentas confiáveis

A flag `--trust-all-tools` é proibida: viola a negação por padrão da `ExecutionPolicy`. O adaptador monta `--trust-tools` só com o que a política autoriza:

- somente leitura: `--trust-tools=fs_read`;
- leitura e escrita sem comandos: `--trust-tools=fs_read,fs_write`;
- `execute_bash` só entra na lista quando a `CommandPolicy` autoriza explicitamente na tarefa. É uma concessão ampla: a flag não prova restrição por prefixo de comando.
