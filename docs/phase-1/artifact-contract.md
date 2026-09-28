# ArtifactRef e contratos de patch/relatório

Estado: proposta documental da Fase 1. Não implementa armazenamento, aplicação de patches ou geração de relatórios.

## Problema e alternativas

Saídas precisam manter identidade, integridade e procedência ao atravessar tarefas, retries e avaliações. Um caminho de arquivo mutável ou um texto produzido pelo agente não comprova qual conteúdo foi usado.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Referenciar somente caminhos | Simples no workspace local | Conteúdo pode mudar sem alterar a referência |
| Tratar toda saída como blob opaco | Armazenamento uniforme | Não valida base de patch, estrutura ou evidências |
| Um schema diferente sem referência comum | Contratos específicos fortes | Duplica identidade, integridade e procedência |
| ArtifactRef comum com contratos tipados | Núcleo uniforme e validação específica | Exige separar descritor, conteúdo e registros de uso |

Recomendação: ArtifactRef imutável identifica uma ocorrência e o digest de seus bytes. Contratos versionados definem a semântica de tipos como snapshot, patch e relatório. Aceitação, aplicação e avaliação pertencem a registros separados; não alteram o artefato original.

## Fronteiras

ArtifactRef responde **qual conteúdo**, **de onde veio**, **como localizá-lo** e **qual contrato deve validar**. Ele não afirma que o conteúdo é correto, seguro, aceito ou aplicável.

- `artifact_id` identifica a ocorrência e sua procedência no run.
- `content_digest` identifica os bytes armazenados.
- Bytes iguais podem aparecer em ocorrências distintas, com artifact_ids e produtores diferentes.
- Um mesmo artifact_id nunca aponta para outros bytes.
- Estado de tarefa, veredicto e vínculo de saída aceita ficam fora do ArtifactRef.

## ArtifactRef

| Campo | Tipo conceitual | Obrigatoriedade e significado |
| --- | --- | --- |
| schema_version | Texto | Obrigatório; versão do formato ArtifactRef |
| artifact_id | Identificador | Obrigatório e único no run |
| artifact_type | Identificador controlado | Obrigatório; por exemplo `repository_snapshot`, `patch`, `report` ou `test_evidence` |
| media_type | Media type | Obrigatório; descreve os bytes, não substitui artifact_type |
| content_contract_ref | Referência versionada | Obrigatória; schema/contrato semântico aplicável |
| content_digest | Digest | Obrigatório; algoritmo e valor sobre os bytes armazenados |
| size_bytes | Inteiro não negativo | Obrigatório; tamanho exato dos bytes armazenados |
| locator | Referência lógica | Obrigatória; localização no armazenamento controlado, não caminho arbitrário do produtor |
| created_at | Data UTC | Obrigatória; momento de ingestão/finalização |
| producer | ProducerRef | Obrigatório; origem da ocorrência |
| derived_from | Lista de ArtifactRefs | Obrigatória; pode ser vazia; fontes diretas ordenadas quando a ordem importar |
| data_classification | Enum | Obrigatório: `public`, `internal`, `confidential`, `restricted` ou `unknown` |
| metadata | Mapa limitado | Opcional; somente chaves definidas pelo contrato ou namespaced |

O digest inicial proposto é SHA-256, identificado explicitamente; trocar ou adicionar algoritmo não reutiliza o mesmo campo sem declarar qual foi usado. O digest cobre os bytes exatos armazenados. Para JSON canônico, texto normalizado ou arquivo compactado, a normalização/serialização pertence ao contrato de conteúdo e ocorre antes do digest.

`locator` usa identidade lógica do armazenamento, como conteúdo endereçado pelo digest. URL externa e caminho do workspace não são locators persistidos diretamente: o conteúdo é ingerido, limitado, verificado e então recebe ArtifactRef. Réplicas podem mudar sem mudar a identidade; um registro de armazenamento resolve o locator para localizações atuais.

ProducerRef distingue inicialmente:

- `external_input`: fonte e vínculo declarados pelo run;
- `attempt_output`: run_id, task_id e attempt_id; o output_id pertence ao binding posterior;
- `evaluation_output`: evaluation_id e finalidade;
- `orchestrator_materialization`: operação identificada, como composição de snapshot;
- `imported_history`: origem histórica explícita, sem fingir produção no run atual.

Procedência não implica confiança. Um artifact externo ou produzido por agente só satisfaz uma entrada após autorização, validação de contrato, integridade e classificação compatível.

## Integridade, disponibilidade e imutabilidade

Ao ingerir, o ORQ calcula tamanho e digest em fluxo, aplica o limite antes de persistir além do permitido e valida o media type real quando possível. O valor declarado pelo produtor não é aceito como prova.

Antes de consumo relevante, os bytes são revalidados contra digest e tamanho. Conteúdo ausente produz `unavailable`; divergência produz `integrity_mismatch`. Nenhum deles é substituído por uma cópia parecida ou arquivo com mesmo nome.

Verificações são eventos dos [registros de execução/avaliação](execution-records-contract.md), pois podem ocorrer várias vezes. ArtifactRef guarda os valores esperados, não um status mutável de verificação.

Arquivos compactados e diretórios exigem manifesto interno com entradas, caminhos relativos, tipos, tamanhos e digests. Extração rejeita caminho absoluto, `..`, colisões por normalização/case, dispositivos, links inseguros e expansão acima do orçamento. Um snapshot de repositório identifica também o digest lógico da árvore e, quando aplicável, commit/base Git; um commit isolado não substitui o conteúdo quando arquivos não rastreados fazem parte da entrada.

## Vínculo com TaskSpec e execução

OutputSpec declara `output_id`, artifact_type e content_contract_ref esperados. AttemptRecord separa:

- `produced_artifacts`: tudo que a tentativa materializou e preservou;
- `output_bindings`: mapeamento entre output_id declarado e ArtifactRef entregue;
- `undeclared_artifacts`: diagnósticos ou anexos preservados que não satisfazem saídas por si próprios.

Um output binding válido exige tipo e contrato compatíveis. Produzir bytes não torna a saída aceita. EvaluationReport referencia os artifact_ids e digests exatos avaliados; somente seu veredicto pode fundamentar a seleção de uma saída aceita. O journal e o RunSummary preservam o vínculo aceito usado por dependências e saídas finais; o RunManifest inicial não contém resultados futuros.

Artefato de tentativa reprovada permanece no histórico, mas não satisfaz task_output. Retry cria novas ocorrências mesmo quando os bytes coincidem, preservando a tentativa produtora.

## PatchArtifact

PatchArtifact é um manifesto estruturado cujo conteúdo referencia o payload do patch e a composição exata sobre a qual ele deve ser aplicado.

### Alternativas de formato

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Arquivos finais completos | Aplicação simples | Dificulta revisão e pode sobrescrever mudanças fora do escopo |
| Unified diff genérico | Legível e difundido | Metadados, renames, modos e binários variam entre ferramentas |
| Git diff versionado | Representa operações comuns do repositório | Exige parser/aplicador compatível e validação segura |
| Commit Git como saída | Identidade nativa | Acopla histórico, autoria e efeitos de repositório à execução |

Recomendação inicial: `git_diff_v1` como payload de repositórios Git, sem criar commit automaticamente. O contrato fixa opções aceitas e recursos suportados; extensões, binários ou operações não suportadas são rejeitados, não degradados silenciosamente. Outros domínios podem definir formatos próprios depois.

### Campos

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| patch_contract_version | Texto | Versão da semântica do patch |
| patch_format | Enum | Inicialmente `git_diff_v1` |
| payload_ref | ArtifactRef | Bytes do patch, com media type e digest próprios |
| root_snapshot_ref | ArtifactRef | Snapshot original autorizado do repositório |
| prerequisite_patch_refs | Lista ordenada | Patches aceitos/materializados antes deste patch; pode ser vazia |
| base_state_digest | Digest de árvore | Estado exato esperado após root + pré-requisitos |
| path_scope | Lista de padrões | Escopo máximo declarado; nunca amplia ExecutionPolicy |
| declared_operations | Lista | Caminhos e operações que o produtor afirma realizar |
| encoding | Texto | Codificação aplicável a conteúdo textual |
| expected_result_digest | Digest ou null | Estado esperado pelo produtor; null quando não calculado |

`root_snapshot_ref + prerequisite_patch_refs + base_state_digest` formam PatchBase. Em T3, code_patch usa o snapshot original e lista vazia. Em T4, test_patch usa o mesmo snapshot, code_patch como pré-requisito e o digest do estado resultante de sua aplicação. A ordem é parte da identidade da base.

O aplicador não confia em `declared_operations`: analisa o payload, compara operações reais, valida path_scope e ExecutionPolicy, e só então tenta aplicar. Cabeçalhos de patch não concedem acesso a caminhos.

Aplicação ocorre em workspace limpo e isolado. Primeiro materializa e verifica a base; depois executa check sem efeito quando disponível; em seguida aplica uma única vez e calcula o digest da árvore resultante. Falha parcial não produz snapshot aceito.

O resultado pertence a PatchApplicationRecord, não ao PatchArtifact imutável. Esse registro contém patch/base exatos, aplicador e versão, horários, status `applied`, `rejected`, `conflict`, `policy_denied`, `integrity_error` ou `unknown`, diagnósticos e, em sucesso, o repository_snapshot resultante. AttemptRecord ou EvaluationReport referencia o registro conforme a finalidade da aplicação.

Aplicar novamente sobre base diferente é outra operação e não prova equivalência. Fuzz, rejeições ignoradas, resolução automática de conflito e aplicação parcial são proibidos no MVP. Modos de arquivo, renames, symlinks e binários só são aceitos quando contrato, política e aplicador declaram suporte verificável.

## ReportArtifact

Relatórios precisam ser legíveis, mas também verificáveis. Um Markdown livre pode existir como apresentação; o contrato normativo é um manifesto estruturado.

### Campos

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| report_contract_version | Texto | Versão do contrato |
| report_kind | Identificador | Por exemplo `analysis`, `implementation`, `test` ou `review` |
| title | Texto não vazio | Título humano |
| summary | Texto não vazio | Resumo, não veredicto autoritativo |
| scope | Objeto | Task/run, requisitos e artefatos cobertos |
| assertions | Lista | Afirmações identificadas e sua base declarada |
| evidence_refs | Lista de ArtifactRefs | Evidências preservadas referenciadas pelo relatório |
| produced_artifact_refs | Lista de ArtifactRefs | Outras entregas descritas, como patches |
| limitations | Lista de textos | Limites conhecidos; pode estar vazia somente quando justificado pelo contrato específico |
| unknowns | Lista de textos | Fatos relevantes não determinados |
| attachments | Lista de ArtifactRefs | Apresentação ou material auxiliar |

Cada assertion contém `assertion_id`, texto, natureza `observed`, `reported`, `inferred` ou `unknown`, referências de evidência e, quando aplicável, requirement_ids/criterion_ids relacionados. `observed` significa que o produtor afirma ter observado; a força da prova ainda é julgada pelo avaliador.

Afirmação obrigatória sem evidência exigida pelo contrato torna o relatório inválido ou insuficiente, conforme o tipo. Uma saída textual do agente, seu resumo ou o próprio relatório não prova que teste passou, patch aplica ou requisito foi atendido. Logs, resultados estruturados, snapshots e PatchApplicationRecords são preservados separadamente e referenciados.

ReportArtifact não contém o veredicto normativo da tarefa. Palavras como “aprovado” no texto são alegações do produtor. EvaluationReport aplica critérios versionados aos digests exatos e emite `pass`, `fail`, `inconclusive` ou `not_run`.

Anexo não herda automaticamente confiança, classificação ou escopo do relatório. Cada anexo possui ArtifactRef próprio. A versão Markdown opcional referencia o mesmo manifesto e não pode introduzir evidência ausente na parte estruturada.

## Interfaces conceituais

| Operação | Entrada | Saída |
| --- | --- | --- |
| ingest_artifact | Fonte autorizada, tipo, contrato e limites | ArtifactRef ou erro sem vínculo |
| verify_artifact | ArtifactRef e armazenamento | Verificação identificada ou falha |
| validate_content | ArtifactRef, bytes e contrato | Relatório estrutural/semântico |
| bind_output | OutputSpec, tentativa e ArtifactRef | Output binding ou incompatibilidade |
| materialize_patch_base | PatchBase e política | Snapshot exato ou erro |
| apply_patch | PatchArtifact, base e política | PatchApplicationRecord |
| validate_report | ReportArtifact e contratos referenciados | Erros, avisos e referências resolvidas |

Essas operações são conceituais e podem ser funções locais. Armazenamento por conteúdo não exige serviço separado no MVP.

## Invariantes

1. ArtifactRef imutável nunca muda de bytes, digest, tamanho, tipo, contrato ou produtor.
2. Digest identifica bytes; artifact_id identifica ocorrência e procedência.
3. Caminho ou URL mutável nunca substitui ingestão e digest.
4. Conteúdo indisponível ou com digest divergente não satisfaz entrada, saída ou evidência.
5. Output binding corresponde a OutputSpec por output_id, tipo e contrato.
6. Artefato produzido não é aceito sem EvaluationReport aplicável aos mesmos digests.
7. Dependências consomem somente bindings aceitos; artefatos reprovados permanecem históricos.
8. Patch só é aplicado à PatchBase exata e na ordem declarada.
9. Operações reais do patch respeitam contrato, path_scope e ExecutionPolicy.
10. Falha ou aplicação parcial não produz snapshot aceito.
11. Relatório não transforma alegação em observação independente nem em veredicto.
12. Evidências, anexos e derivados preservam ArtifactRefs e classificação próprios.
13. Grafo derived_from é acíclico e não inventa procedência ausente.
14. Limites são verificados sobre bytes ingeridos e, para containers, também sobre conteúdo expandido.

## Riscos e verificações

- **Confusão entre ID e hash:** permitir deduplicação física sem perder ocorrências e produtores distintos.
- **Canonicalização ambígua:** definir bytes/serialização antes do digest e nunca recalcular sobre representação diferente.
- **Conteúdo hostil:** limitar tamanho, tipo, parsing, compactação e expansão; tratar metadados como dados não confiáveis.
- **Path traversal:** validar manifestos, payload do patch e extrações após normalização segura.
- **Patch na base errada:** exigir digest de árvore e pré-requisitos ordenados, não apenas nome de branch ou commit.
- **Relatório autojustificável:** separar alegações, evidências brutas e EvaluationReport.
- **Armazenamento perdido:** registrar unavailable e política de retenção; não fabricar substituto.
- **Dados sensíveis:** propagar classificação de forma conservadora e aplicar ExecutionPolicy a leitura, contexto e rede.

## Casos específicos a formalizar

| ID | Caso | Resultado esperado |
| --- | --- | --- |
| A01 | Mesmo conteúdo produzido por tentativas distintas | Mesmo digest permitido; artifact_ids/produtores distintos |
| A02 | Mesmo artifact_id com bytes diferentes | Conflito rejeitado |
| A03 | Arquivo no locator diverge do digest | `integrity_mismatch`; nenhum consumo |
| A04 | Output binding usa tipo/contrato incompatível | Binding rejeitado |
| A05 | Artefato de tentativa reprovada é usado por dependência | Uso rejeitado; histórico preservado |
| A06 | Patch declara base diferente do workspace materializado | Aplicação rejeitada antes do efeito |
| A07 | Patch real altera caminho fora de path_scope | `policy_denied`, mesmo que manifesto omita o caminho |
| A08 | Test patch referencia code patch como pré-requisito | Base composta na ordem e digest declarados |
| A09 | Patch aplica parcialmente e depois falha | Nenhum snapshot aceito; diagnóstico preservado |
| A10 | Relatório afirma testes aprovados sem evidência | Alegação não sustenta pass |
| A11 | EvaluationReport usa digest diferente do output binding | Avaliação não aceita aquela saída |
| A12 | Arquivo compactado expande acima do limite | Ingestão/materialização interrompida sem consumo parcial |
| A13 | Retry produz bytes iguais ao anterior | Nova ocorrência; tentativa produtora correta |
| A14 | Artefato classificado é enviado a destino incompatível | Contexto/rede negados pela política |

Esses casos especificam validadores e armazenamento futuros; não são testes executados.

## Decisões ainda abertas

- Backend e política de retenção do armazenamento local.
- Serialização canônica dos manifestos JSON.
- Subconjunto exato de `git_diff_v1`, incluindo modos, renames, symlinks e binários.
- Vocabulário inicial de artifact_type, report_kind e classificação de dados.
- Limites de tamanho por tipo e proteção contra parsing/expansão excessiva.

O [contrato de registros](execution-records-contract.md) especifica RunManifest, AttemptRecord, PatchApplicationRecord, ApprovalRecord, uso/custos/erros e EvaluationReport usando ArtifactRef como referência imutável. A [metadata de conhecimento](knowledge-metadata-contract.md) aplica a mesma identidade à ingestão das fontes e unidades do Vault. O próximo passo é revisar esses contratos em conjunto numa execução fictícia completa.
