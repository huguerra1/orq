---
title: "Guia do papel backend_engineer"
language: pt-BR
topics: [adaptador, reconciliacao, digest]
---
# Guia do papel backend_engineer

Os adaptadores implementam `describe`, `dispatch`, `observe`, `cancel`, `reconcile` e `collect`.

- O registro durável do processo, com `state_hash` JCS, é gravado antes do spawn.
- `not_found` autoritativo só existe com estado durável que prove que a CLI nunca iniciou; chave desconhecida é `unknown` e nunca autoriza redespacho.
