---
title: "agy (Gemini): ruído de saída"
language: pt-BR
topics: [ruido, stderr, saida]
---
# agy (Gemini): ruído de saída

O stderr do `agy` emite a linha de inicialização `logging before google.Init`, que o parser filtra antes de classificar erros.

O artefato de saída deve ser gravado em arquivo determinístico no workspace ou delimitado por marcador canônico validado contra o `OutputSpec`.
