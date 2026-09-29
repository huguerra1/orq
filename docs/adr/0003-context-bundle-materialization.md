# ADR 0003 — Materialização do bundle de contexto

Estado: aceito em 2026-09-29.

## Contexto

KnowledgeSelectionRecord prova o que foi escolhido, mas não identifica sozinho os bytes e a ordem efetivamente preparados para um executor. ContextManifest precisa ser auditável sem duplicar conteúdo potencialmente sensível dentro do próprio registro.

## Alternativas

| Alternativa | Vantagens | Limitações |
| --- | --- | --- |
| Somente referências no manifesto | Registro pequeno | Não identifica a representação entregue |
| Conteúdo embutido no manifesto | Artefato autocontido | Duplica dados, aumenta vazamento e mistura evidência com payload |
| Manifesto e bundle canônico separados | Hash, ordem e procedência explícitos sem embutir conteúdo | Exige preservar os dois artefatos |

## Decisão

- ContextManifest é um registro fechado, separado dos bytes do bundle.
- O bundle local v0.1 é JSON canônico RFC 8785 com `context_manifest_id` e itens ordenados por `sequence`.
- Cada item do bundle contém somente `item_id`, categoria e conteúdo materializado. O manifesto registra fonte, revisão, digests, classificação, seleção e tamanho.
- Unidades do Vault são materializadas novamente pelo span e seus digests de fonte e unidade são verificados.
- A primeira implementação não resume, recorta nem transforma unidades. Qualquer transformação futura terá cadeia explícita e novos digests.
- `char4-v1` continua uma estimativa conservadora; não é apresentado como contagem do provedor.
- O materializador rejeita seleção obrigatória não satisfeita, referência divergente, bytes alterados e contexto acima do limite disponível. Não trunca silenciosamente.
- Revogação posterior à seleção pertence ao preflight do roteamento; o materializador usa o catálogo fixado pelo run e não cria revisão nova.
- Persistência física por conteúdo e retenção permanecem fora deste incremento. A API retorna manifesto, bundle e bytes dos itens para a futura camada de artefatos.

## Consequências

- O mesmo conjunto ordenado de entradas gera os mesmos bytes e hash.
- ContextManifest pode ser validado sem expor o conteúdo.
- O adaptador de executor terá de converter o bundle observável para seu protocolo e declarar qualquer contexto adicional do provedor como parcial ou desconhecido.
- Alterar a representação do bundle exige nova versão do contrato.

## Testes exigidos

- manifesto e bundle determinísticos;
- hash do bundle sobre os bytes canônicos exatos;
- ordem contígua e digests cruzados;
- mutação após seleção rejeitada;
- orçamento do destino respeitado sem truncamento;
- seleção obrigatória não satisfeita rejeitada.
