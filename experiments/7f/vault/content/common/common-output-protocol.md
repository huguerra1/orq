---
title: "Protocolo de saída final"
language: pt-BR
topics: [saida-estruturada, protocolo-de-saida, orq-final]
---
# Protocolo de saída final

A resposta final da CLI carrega a linha `ORQ_FINAL:{"outputs":{"<output_id>":<JSON>}}`.

- Alternativa: o marcador com `outputs` vazio e arquivos `.orq-output/<output_id>.json` no workspace.
- Todos os contratos de conteúdo são validados antes de publicar bytes no `ArtifactStore`.
