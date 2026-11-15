---
title: "Regras de audiência da memória"
language: pt-BR
topics: [audiencia, familia, memoria]
---
# Regras de audiência da memória

Cada unidade de conhecimento tem uma audiência com `area_scope`:

- `common`: `target_families` e `target_roles` vazios; visível a todas as famílias.
- `family`: pelo menos uma das listas é não vazia; com as duas preenchidas, vale a interseção (família listada e papel listado).
- `area_scope`, `target_families` e `target_roles` são atribuídos pelo catálogo, nunca pelo frontmatter. O frontmatter aceita só `title`, `language`, `topics` e `relationships`.
