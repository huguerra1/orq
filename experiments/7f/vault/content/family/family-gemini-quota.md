---
title: "agy (Gemini): detecção de limite de uso"
language: pt-BR
topics: [cota, deteccao-de-limite]
---
# agy (Gemini): detecção de limite de uso

O detector do `agy` reconhece mensagens de exaustão de quota: `RESOURCE_EXHAUSTED`, `quota exceeded` e `usage limit`.

A detecção só vale quando a invocação falha e o padrão aparece no canal de erro/status da CLI.
