---
title: "agy (Gemini): permissões em modo headless"
language: pt-BR
topics: [permissoes, headless, ferramentas, saida-vazia]
---
# agy (Gemini): permissões em modo headless

Em modo headless (`-p`), qualquer ferramenta sem regra explícita de permissão é negada automaticamente: a execução termina sem saída útil e com a mensagem `no output produced`.

- As regras ficam em `permissions.allow` do `settings.json` do `agy`, por exemplo `read_file(<caminho>)` e `command(<prefixo>)`.
- O adaptador grava as regras no `.gemini/antigravity-cli/settings.json` do workspace e as remove ao final.
- Saída vazia por permissão vira `ErrorRecord` com código `permission_or_empty_output` (`failed`), nunca `completed`.
