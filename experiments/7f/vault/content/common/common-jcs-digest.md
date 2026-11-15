---
title: "Canonicalização JCS e digests"
language: pt-BR
topics: [jcs, digest, canonicalizacao]
---
# Canonicalização JCS e digests

Todo contrato JSON do ORQ é canonicalizado conforme a RFC 8785 (JCS) antes de gerar digest.

- O digest é o objeto `{"algorithm": "sha256", "value": "<64 hexadecimais minúsculos>"}`; o `value` casa com `^[a-f0-9]{64}$`.
- O SHA-256 é calculado sobre os bytes JCS (UTF-8) do valor.
- Inteiros fora da faixa segura IEEE 754, de -9007199254740991 a 9007199254740991, são rejeitados (`integer_outside_ieee754_safe_range`).
- Números não finitos (`NaN`, `Infinity`) e o zero negativo são rejeitados.
- Objetos com chave não textual são rejeitados.
