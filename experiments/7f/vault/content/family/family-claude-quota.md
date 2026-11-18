---
title: "Claude Code: detecção de limite de uso"
language: pt-BR
topics: [cota, deteccao-de-limite]
---
# Claude Code: detecção de limite de uso

O detector do Claude Code reconhece padrões de limite de sessão ou janela: `rate limit`, `usage limit`, `limit reached` e `resets at <horário>`.

A detecção só vale quando a invocação falha e o padrão aparece no canal de erro/status da CLI, nunca em conteúdo de arquivos lidos pelo agente.
