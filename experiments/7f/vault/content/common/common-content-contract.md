---
title: "Contrato de conteúdo de saída"
language: pt-BR
topics: [contrato-de-conteudo, digest, avaliacao]
---
# Contrato de conteúdo de saída

Cada saída esperada aponta para um contrato de conteúdo por `{id, version, content_digest}`.

- O `content_digest` é o digest JCS do schema JSON (Draft 2020-12) do contrato.
- O registro local de contratos recusa um schema cujo digest difere da referência (`content_contract_digest_mismatch`).
- Uma referência desconhecida nunca é aprovada por heurística, e `format` é apenas anotação.
