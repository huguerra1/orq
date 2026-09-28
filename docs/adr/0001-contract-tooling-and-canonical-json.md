# ADR 0001 — Tooling dos contratos e JSON canônico

Estado: aceito em 2026-09-28.

## Contexto

O baseline v0.1 exige JSON Schema, validação semântica, fixtures reproduzíveis e hashes estáveis antes de Vault, MCP ou executores. A escolha não deve acoplar os contratos a Jev ou a outro provedor.

## Alternativas

| Alternativa | Vantagens | Limitações |
| --- | --- | --- |
| TypeScript + Ajv | Tipagem forte e ecossistema JSON maduro | Introduz Node antes do futuro caminho Python/Pydantic |
| Python + jsonschema | Implementação direta, suporte a Draft 2020-12 e caminho curto para adaptadores futuros | Tipagem estática menos rígida que TypeScript |
| Validador próprio | Controle total | Alto risco de implementar parcialmente o padrão |

Para canonicalização, ordenar chaves com `json.dumps` seria simples, mas não fixa números e Unicode de forma interoperável. JCS/RFC 8785 define representação determinística para I-JSON.

## Decisão

- Python 3.12 como runtime inicial do núcleo local.
- JSON Schema Draft 2020-12 como formato estrutural canônico.
- `jsonschema` 4.26.0 para validação estrutural e `referencing` fornecido por sua dependência.
- Validação semântica própria somente para relações que JSON Schema não expressa bem: referências, cobertura, compatibilidade e ciclos.
- `pytest` 9.1.1 para fixtures e testes.
- `setuptools` 84.0.0 como backend de build fixado.
- RFC 8785/JCS por `rfc8785` 0.1.4 para bytes canônicos.
- SHA-256 sobre os bytes JCS, representado por `{ "algorithm": "sha256", "value": "<64 hex>" }`.
- Valores decimais financeiros permanecem strings no domínio; floats não finitos, zero negativo e inteiros fora da faixa interoperável são rejeitados antes do hash.

As versões são fixadas no projeto e serão atualizadas por mudança explícita. Jev continuará atrás da interface `typed_decision`; esta ADR não escolhe seu adaptador.

## Consequências

- Schemas e fixtures são portáveis para outras linguagens.
- O primeiro pacote contém contratos e validadores, não Orchestrator nem integração externa.
- Erros estruturais e semânticos usam `code`, `path` e `message` estáveis.
- Mudança de canonicalização exige nova versão; não recalcula silenciosamente hashes históricos.

## Riscos e testes

- Divergência entre schema e Markdown: cada fixture relevante vira teste e os documentos apontam para a versão executável.
- Referências remotas acidentais: o registry carrega somente schemas locais conhecidos.
- Ambiguidade numérica: testes rejeitam valores incompatíveis com I-JSON/JCS.
- Ciclos ou referências inválidas: testes semânticos cobrem componentes desconectados e dependências diretas.

## Referências

- [jsonschema 4.26.0](https://pypi.org/project/jsonschema/)
- [pytest](https://docs.pytest.org/en/stable/)
- [RFC 8785 — JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785.html)
- [rfc8785.py 0.1.4](https://pypi.org/project/rfc8785/)
