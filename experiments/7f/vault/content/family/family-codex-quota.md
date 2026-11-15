---
title: "Codex CLI: detecção de limite de uso"
language: pt-BR
topics: [cota, deteccao-de-limite]
---
# Codex CLI: detecção de limite de uso

Mensagem real observada: `You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/plus) or try again at 10:05 PM.` Também ocorre o formato de 24 horas, `try again at 22:05`.

O detector procura `usage limit` e, opcionalmente, `try again at <HH:MM> [AM|PM]` ou data/hora explícita; o horário é resolvido no fuso do host, passando para o dia seguinte quando já passou.
