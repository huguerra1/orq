# Metadata de conhecimento do Vault

Estado: baseline normativa v0.1 aceita na revisão cruzada. Não implementa Vault, parser Markdown, busca, embeddings, RAG ou MCP.

## Problema e alternativas

O Knowledge Router precisa selecionar somente conhecimento pertinente, autorizado, íntegro e válido. Nome de arquivo, pasta ou frontmatter escrito pelo próprio documento não comprovam projeto, autoridade nem permissão para instruir agentes.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Pastas e nomes como metadata | Autoria simples | Convenções frágeis e sem procedência por campo |
| Frontmatter totalmente confiável | Arquivo autocontido | Documento pode ampliar a própria autoridade/classificação |
| Catálogo externo para todos os campos | Controle forte | Autoria e manutenção mais pesadas |
| Frontmatter descritivo + catálogo autorizado | Boa autoria com fronteira de confiança | Exige resolver e registrar a origem de cada campo sensível |

Recomendação: Markdown permanece a fonte editorial; um catálogo versionado resolve metadata efetiva. Campos descritivos podem vir de frontmatter conforme regras do catálogo. Projeto, autoridade, classificação, validade e capacidade de fornecer instruções são atribuídos por configuração autorizada, nunca aceitos por autoafirmação do conteúdo.

## Fronteiras

O contrato distingue:

- **KnowledgeSource:** documento/versionamento editorial completo;
- **KnowledgeUnit:** trecho recuperável derivado de uma revisão exata;
- **KnowledgeCatalogSnapshot:** conjunto fechado de fontes/unidades disponível para um run;
- **KnowledgeSelectionRecord:** candidatos, exclusões, ranking e cobertura de uma tarefa;
- **ContextManifest:** bytes materializados e ordenados enviados ao destino.

Metadata torna conteúdo elegível e rastreável; não prova que ele está correto. Recuperação não amplia ExecutionPolicy, não substitui ArtifactRef e não converte Execution Memory automaticamente em conhecimento editorial.

## KnowledgeSourceMetadata

Cada combinação source_id/revision é imutável. Mudança de conteúdo ou de metadata normativa cria nova revisão.

| Campo | Tipo conceitual | Obrigatoriedade e significado |
| --- | --- | --- |
| schema_version | Texto | Obrigatório; versão do formato |
| source_id | Identificador | Obrigatório; identidade estável da fonte |
| revision | Texto de versão | Obrigatório; revisão imutável |
| title | Texto não vazio | Obrigatório; título editorial |
| knowledge_kind | Enum | `operational` ou `project` |
| content_locator | Referência lógica | Localização autorizada no Vault, não identidade suficiente |
| media_type, encoding, language | Textos controlados | Obrigatórios para materialização/parsing |
| content_digest | Digest | Obrigatório; bytes exatos da revisão |
| size_bytes | Inteiro não negativo | Obrigatório |
| project_scope | Objeto | Projeto exato, coleção compartilhada autorizada ou global operacional |
| authority | AuthorityMetadata | Obrigatório; emissor, escopo, nível e evidência |
| instruction_scope | Enum | `none`, `project` ou `operational` |
| applicability | Objeto | Papéis, task_types, tópicos, paths/domínios e capacidades relacionadas |
| data_classification | Enum | `public`, `internal`, `confidential`, `restricted` ou `unknown` |
| owner_ref | Referência | Responsável editorial/operacional |
| verified_at | Data UTC ou null | Última verificação substantiva; null significa não verificada |
| effective_from, expires_at | Datas UTC ou null | Janela explícita; null não significa validade universal |
| relationships | Lista | supersedes, depends_on, related_to ou conflicts_with |
| chunking_policy_ref | Referência versionada | Parser e regras de unidade |
| metadata_provenance | Mapa | Origem e evidência de cada campo efetivo sensível |

`content_digest` cobre os bytes da fonte. Durante o snapshot do run, a revisão é ingerida como ArtifactRef; ContextManifest referencia a unidade, a fonte e o ArtifactRef materializado.

O ciclo de vida fica fora da metadata imutável da revisão. CatalogStatusAssertion é append-only e contém assertion_id, source_id/revision, status (`active`, `deprecated` ou `revoked`), effective_at, issuer_ref, evidence_ref e digest. O catálogo resolve o status vigente em seu cutoff; declarações conflitantes de mesma precedência produzem conflito, não escolha silenciosa.

### Escopo e autoridade

Fonte project pertence a um project_id ou coleção compartilhada autorizada. Ela nunca é global por simples ausência de project_id. Fonte operational pode ser global somente quando o catálogo autorizado assim determinar.

AuthorityMetadata contém `authority_level`, `authority_scope`, issuer_ref, assigned_at e evidence_ref. Níveis iniciais: `platform_authoritative`, `project_authoritative`, `reference` e `untrusted`. O nível só vale dentro do authority_scope, como `role_instructions`, `model_facts`, `project_architecture`, `coding_conventions` ou `domain_reference`.

`instruction_scope=operational` exige knowledge_kind operational e autoridade de plataforma compatível. `instruction_scope=project` pode orientar arquitetura/convenções dentro do projeto, mas nunca altera permissões, orçamento, estados ou regras do Orchestrator. `none` trata todo o conteúdo como dados/referência.

Mesmo instrução autorizada perde para limites de sistema e ExecutionPolicy. Texto dentro de exemplos, citações, logs, issues e código é dado não confiável, não uma nova camada de instrução.

### Metadata no Markdown

Uma CatalogPolicy versionada define quais campos de frontmatter são aceitos por root, proprietário e tipo de fonte. Por padrão, frontmatter pode sugerir título, idioma, tópicos e relações editoriais. Não pode autoatribuir project_scope, authority, instruction_scope, classificação menos restritiva, validade ou status active.

Metadata efetiva registra por campo se veio de regra do catálogo, manifestação editorial aceita, herança controlada ou verificação externa. Conflitos com regra superior são erro; não são resolvidos em favor do documento.

Segredos e credenciais não pertencem ao Vault. Referências a segredo seguem SecretPolicy e nunca são materializadas como conhecimento recuperável.

## KnowledgeUnitMetadata

Unidades são derivadas deterministicamente de uma revisão da fonte. unit_id é único dentro de source_id/revision e não é presumido estável entre revisões.

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| unit_id | Identificador | Único na revisão |
| source_ref | Referência com digest | source_id/revision exatos |
| ordinal | Inteiro não negativo | Ordem na fonte |
| unit_kind | Enum | `prose`, `code`, `table`, `list`, `frontmatter` ou `mixed` |
| heading_path | Lista de textos | Hierarquia editorial |
| source_span | Objeto | Intervalo de bytes e, quando disponível, linhas |
| content_digest, size_bytes | Digest e inteiro | Bytes materializados da unidade |
| parent_unit_id | ID ou null | Hierarquia recuperável |
| effective_topics | Lista | Tópicos resolvidos da fonte/unidade |
| effective_applicability | Objeto | Pode restringir, nunca ampliar, a fonte |
| effective_classification | Enum | Igual ou mais restritiva que a fonte |
| instruction_scope | Enum | Igual ou mais restrito que a fonte |
| token_estimates | Lista | tokenizer_ref, quantidade, status e instante |
| derivation | Objeto | Parser/chunker, versão, configuração e transformação |

O chunker preserva limites semânticos: heading com seu conteúdo, code fence e tabela não são cortados silenciosamente. Unidade acima do orçamento é marcada oversized e exige estratégia explícita versionada; truncamento ad hoc não satisfaz conhecimento obrigatório.

Resumo, tradução ou recorte gerado é outra unidade derivada, com digest, transformação e fontes próprias. Não herda automaticamente autoridade nem substitui a unidade original quando a exigência requer conteúdo integral.

## KnowledgeCatalogSnapshot

KnowledgeCatalogSnapshot fecha o universo consultável de um run.

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| catalog_id, version | Identidade versionada | Snapshot imutável |
| created_at | Data UTC | Momento de fechamento |
| catalog_policy_ref | Referência com hash | Regras de confiança e herança |
| status_cutoff | Data UTC | Instante de resolução dos estados das fontes |
| status_assertion_refs | Lista | Declarações autorizadas usadas para resolver cada status |
| source_entries | Lista | source_id/revision/digest, status e unidades incluídas |
| parser_chunker_snapshots | Lista | Ferramentas, versões e configurações |
| vocabulary_refs | Lista | Tópicos, roles, task_types e authority scopes controlados |
| exclusions | Lista | Fontes rejeitadas e motivos |
| catalog_hash | Digest | Representação canônica do snapshot |

Mesmos source_id/revision com digest ou metadata efetiva diferentes são conflito. Fonte inválida, ausente ou revogada aparece em exclusions; não some silenciosamente. RunManifest referencia o catalog_hash utilizado, que permanece fixo durante o run. Uma nova revisão de fonte exige novo run; retry somente refaz a seleção dentro do mesmo snapshot.

Execution Memory não entra automaticamente no catálogo. Promover um fato histórico exige curadoria, responsável, revisão e nova KnowledgeSource; métricas observadas continuam registros factuais, não frontmatter editorial.

## KnowledgeRequirement refinado

O objeto já declarado em TaskSpec passa a explicitar:

| Campo | Semântica |
| --- | --- |
| knowledge_requirement_id | Identificador único na tarefa |
| subject | Assunto/query ou referência exata não vazia |
| required | Booleano |
| allowed_kinds | operational, project ou ambos |
| required_authority_scopes | Lista; pode ser vazia para referência geral |
| source_refs | Lista opcional de fontes/revisões obrigatórias |
| applicability_filters | Tópicos, paths, domínio e outros filtros controlados |
| freshness | Regra ou null; não inventa prazo universal |
| coverage_rule | `at_least_one`, `all_source_refs` ou regra versionada |
| max_context_tokens | Inteiro positivo ou null | Teto local que só restringe o orçamento superior |

Uma exigência obrigatória sem cobertura válida impede despacho. Uma exigência opcional não atendida gera lacuna registrada, não conteúdo fabricado.

## Seleção e ranking inicial

O MVP usa filtros determinísticos seguidos por busca lexical versionada. Embeddings, banco vetorial e reranker por modelo ficam fora da Fase 1.

Ordem conceitual:

1. resolver KnowledgeRequirements e KnowledgeCatalogSnapshot;
2. excluir status não active, projeto/coleção incompatível e conteúdo não autorizado;
3. validar autoridade, instruction_scope, classificação, validade, integridade e applicability;
4. detectar conflitos obrigatórios e fontes explicitamente requeridas;
5. aplicar ranking lexical somente às unidades elegíveis;
6. selecionar sob orçamento, preservando cobertura das exigências obrigatórias;
7. materializar/validar bytes e registrar ContextManifest.

Ranking nunca compensa falha de autorização, validade ou autoridade. Desempate é estável por score, source_id/revision, ordinal e unit_id. A política registra tokenizer, normalização, analisador lexical, pesos e versão.

Conteúdo da query/tarefa é dado não confiável: não pode alterar filtros nem instruir o recuperador. Mesmos bytes vindos de fontes distintas podem ser deduplicados no bundle, mas KnowledgeSelectionRecord preserva todas as procedências consideradas e qual foi escolhida como principal.

## KnowledgeSelectionRecord

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| schema_version | Texto | Versão do formato |
| selection_id | Identificador | Único no run |
| run_id, workflow_ref, task_id | Referências | Escopo da seleção |
| candidate_attempt_number | Inteiro positivo | Tentativa pretendida; não a consome |
| catalog_snapshot_ref | Referência com hash | Universo consultado |
| requirement_snapshots | Lista | KnowledgeRequirements efetivos |
| query_input | Objeto/hash | Termos/estado efetivamente usados |
| policy_snapshot_refs | Lista | ExecutionPolicy, CatalogPolicy e política de ranking |
| candidates | Lista | Unidades consideradas, scores, elegibilidade e exclusões |
| selected_units | Lista ordenada | source/unit refs, digests, score e motivo |
| coverage | Lista | Status e unidades por requirement_id |
| budget | Objeto | Limite, estimativa, tokenizer e margem |
| status | Enum | `satisfied`, `partial`, `unsatisfied` ou `conflict` |
| created_at | Data UTC | Momento da decisão |
| selection_hash | Digest | Representação canônica |

`partial` só permite continuar quando todas as exigências obrigatórias estão satisfied e faltam apenas opcionais. `unsatisfied` ou `conflict` em requisito obrigatório impede materialização admissível.

Cada exclusão possui reason_code verificável, como wrong_project, insufficient_authority, expired, revoked, classification_denied, integrity_error, not_applicable ou over_budget. Score lexical baixo não é erro; apenas afeta ordenação entre elegíveis.

ContextManifest adiciona `knowledge_selection_refs`. Cada item de conhecimento registra selection_id, requirement_ids cobertos, source_id/revision, unit_id, source/unit digests, materialized ArtifactRef, transformação e posição final. O bundle_hash cobre a ordem efetivamente enviada.

Retry executa nova seleção quando requisito, política efetiva, contexto anterior ou orçamento mudam, sempre sobre o catálogo fixado pelo RunManifest. Reutilização exata é permitida por referência quando todas as entradas e hashes coincidem; cada ContextManifest ainda registra o que foi materializado naquela tentativa. Catálogo revisado inicia outro run.

## Conflitos, validade e revogação

`supersedes` explícito pode retirar revisão anterior de novas seleções sem apagar histórico. Recência sozinha não decide conflito semântico. Fontes de autoridade diferente são comparadas somente dentro do mesmo authority_scope.

Contradição relevante é registrada com unidades e campos conflitantes. Para requisito obrigatório, conflito não resolvido produz status conflict. Para conteúdo opcional, a política pode excluir ambos ou selecionar a precedência autorizada, sempre registrando a decisão.

Revogação impede novas seleções e materializações. Runs históricos preservam snapshots e ContextManifests; revogação posterior não reescreve o passado. Antes do despacho, o preflight resolve novamente as CatalogStatusAssertions vigentes e registra a evidência. Se uma fonte selecionada tiver sido revogada, reprova o contexto e exige nova seleção entre unidades do mesmo catálogo; não pode introduzir revisão nova no run.

`expires_at` vencido, freshness não atendida ou verified_at ausente seguem a exigência/política. Unknown não vira atual. Não existe validade universal igual para todos os tipos de conhecimento.

## Interfaces conceituais

| Operação | Entrada | Saída |
| --- | --- | --- |
| validate_source | Conteúdo, metadata proposta e CatalogPolicy | Metadata efetiva ou erros |
| build_catalog_snapshot | Fontes, parser/chunker e vocabulários | Snapshot imutável/exclusões |
| derive_units | Fonte válida e ChunkingPolicy | KnowledgeUnits determinísticas |
| filter_knowledge | Requisitos, catálogo, projeto e política | Elegíveis e exclusões |
| rank_knowledge | Elegíveis, query e política lexical | Ranking reproduzível |
| select_knowledge | Ranking, cobertura e orçamento | KnowledgeSelectionRecord |
| materialize_knowledge | Seleção e armazenamento | Itens íntegros para ContextManifest |

Essas operações podem ser funções locais. MCP futuramente expõe operações pequenas; não recebe autoridade para mudar catálogo, política ou seleção registrada.

## Invariantes

1. source_id/revision publicado não muda conteúdo nem metadata normativa.
2. Documento não atribui a si mesmo projeto, autoridade, classificação ou poder instrucional.
3. Project Knowledge não amplia ExecutionPolicy nem vira instrução operacional.
4. Somente fontes active, íntegras, válidas e autorizadas entram em nova seleção.
5. Unidade nunca amplia applicability, classificação ou instruction_scope da fonte.
6. Catálogo do run fecha revisões, digests, parser e chunker usados.
7. Filtros obrigatórios precedem ranking; score não recupera candidato inelegível.
8. Requisito obrigatório sem cobertura ou com conflito impede despacho.
9. Truncamento/resumo não substitui silenciosamente unidade obrigatória.
10. SelectionRecord preserva candidatos, exclusões, política, scores e cobertura.
11. ContextManifest identifica seleção e bytes/unidades realmente enviados.
12. Revogação append-only não reescreve runs históricos, mas bloqueia novo despacho quando já vigente.
13. Conteúdo classificado respeita política do destino, rede e contexto.
14. Execution Memory só vira fonte editorial após promoção explícita e versionada.

## Riscos e verificações

- **Autoelevação por frontmatter:** separar campos editoriais de campos atribuídos pelo catálogo.
- **Vazamento entre projetos:** exigir project_scope exato antes do ranking e da materialização.
- **Prompt injection:** tratar conteúdo/query como dados e separar instruction_scope autorizado.
- **Fonte obsoleta:** registrar verified_at, validade, supersedes e conflitos sem assumir que “mais novo” vence.
- **Chunking destrutivo:** preservar unidades semânticas e registrar parser/configuração.
- **Ranking irreproduzível:** versionar normalização, analisador, pesos e desempate.
- **Orçamento enviesado:** garantir cobertura obrigatória antes de preencher contexto com opcionais.
- **Autoridade excessiva:** limitar autoridade por scope; classificação e permissão continuam independentes.
- **Memória contaminada:** não promover resultados/alegações automaticamente ao Vault.
- **Conteúdo indisponível:** manter exclusão/lacuna explícita; não substituir por documento semelhante.

## Casos específicos a formalizar

| ID | Caso | Resultado esperado |
| --- | --- | --- |
| K01 | Frontmatter se declara platform_authoritative | Autoridade ignorada/rejeitada conforme CatalogPolicy |
| K02 | Documento project não possui project_scope autorizado | Excluído do catálogo/seleção |
| K03 | Fonte de outro projeto tem score maior | Excluída antes do ranking |
| K04 | Project Knowledge tenta ampliar rede/escrita | Conteúdo não altera ExecutionPolicy |
| K05 | Mesma source_id/revision possui digest diferente | Conflito de catálogo |
| K06 | Fonte deprecated/revoked aparece em busca | Não entra em nova seleção; histórico preservado |
| K07 | Unidade reduz classificação da fonte | Metadata inválida |
| K08 | Fonte obrigatória vencida ou não verificada | Requisito unsatisfied/conflict conforme regra; sem despacho |
| K09 | Unidade obrigatória excede orçamento | Não truncar; seleção não admissível ou estratégia explícita |
| K10 | Dois documentos obrigatórios se contradizem | conflict registrado; bloqueio até resolução |
| K11 | Seleção omite conhecimento opcional | partial permitido com lacuna explícita |
| K12 | Mesmos bytes existem em duas fontes | Bundle pode deduplicar; procedências preservadas |
| K13 | Fonte é revogada entre seleção e despacho | Preflight rejeita contexto e exige nova seleção |
| K14 | Retry muda feedback/contexto no mesmo catálogo | Novo SelectionRecord e ContextManifest; anterior preservado |
| K15 | Markdown contém instrução em exemplo/log | Tratado como dados, sem elevar instruction_scope |
| K16 | Fato da Execution Memory é inserido automaticamente | Rejeitado sem promoção editorial versionada |
| K17 | Retry tenta introduzir revisão ausente do catálogo do run | Rejeitado; nova revisão exige novo run |

Esses casos especificam validadores e recuperação futuros; não são testes executados.

## Decisões ainda abertas

- Layout físico do Vault e formato de CatalogPolicy.
- Vocabulários iniciais de tópicos, authority_scope e relações.
- Parser Markdown, serialização e política exata de chunking.
- Algoritmo lexical e pesos do ranking inicial.
- Tokenizadores e margens por destino.
- Fluxo editorial de revisão, revogação e promoção da Execution Memory.

A revisão cruzada está registrada em [baseline v0.1](cross-review.md). O próximo passo é decidir linguagem/tooling e serialização canônica antes de implementar JSON Schemas e fixtures.
