# Decisão de roteamento e manifesto de contexto

Estado: proposta documental da Fase 1. Não integra TypeSafe AI, Jev, provedores ou executores.

## Problema e alternativas

Antes de executar uma tarefa, a plataforma precisa demonstrar por que escolheu um destino e qual conteúdo entregou a ele. A escolha probabilística não pode ampliar permissões nem esconder um fallback.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Destino fixo | Baseline simples e reproduzível | Não se adapta às características da tarefa |
| Regras determinísticas | Auditáveis e independentes de provedor | Exigem manutenção e podem empatar candidatos |
| Modelo decide entre todos os destinos | Flexível | Pode recomendar opção incompatível ou não autorizada |
| Filtro determinístico e ranking configurável | Separa segurança de preferência e permite comparação | Exige preservar as duas etapas e suas evidências |

Recomendação: filtrar a elegibilidade por regras obrigatórias e somente depois ordenar o conjunto elegível. A política de ranking pode ser fixa, baseada em regras ou usar um motor de decisão tipada, como Jev. O Orchestrator aplica limiar, fallback, orçamento e autorização; o motor externo não executa o destino nem concede permissões.

Jev é uma integração candidata, não uma dependência do contrato. O núcleo representa a classe `typed_decision`; detalhes do provedor ficam em uma extensão identificada. Regras e destino fixo permanecem baselines experimentais.

## Sequência de seleção e admissão

1. Resolver TaskSpec, papel, política e snapshots do catálogo.
2. Excluir deterministicamente destinos incompatíveis por estado, capacidade, permissão, contrato de saída, controles e limite preliminar de contexto.
3. Aplicar a política de ranking somente aos destinos elegíveis.
4. Resolver baixa confiança, resposta inválida ou falha conforme fallback explícito e alocar `routing_decision_id`.
5. Materializar o contexto específico para o destino efetivo e preparar o rascunho de ContextManifest.
6. Fazer preflight final de contexto, disponibilidade, credenciais, quota, orçamento e limites; então finalizar primeiro a RoutingDecision e depois o ContextManifest com sua referência e hash.
7. Persistir a tentativa e sua intenção de despacho somente após a admissão; AttemptRecord referencia a decisão e o contexto usados.

O ranking usa apenas um resumo de roteamento identificado, não o ContextManifest de execução ainda inexistente. Se o contexto materializado não couber ou o preflight reprovar o destino, não ocorre despacho. A exclusão é registrada e uma nova RoutingDecision pode considerar os candidatos restantes, se a política e o prazo permitirem.

Uma chamada externa de roteamento que falha antes da admissão não consome `max_attempts` da tarefa. Seu tempo e custo conhecidos pertencem ao run e não são apagados.

## RoutingPolicy

RoutingPolicy é a seção de [ExecutionPolicy](execution-policy-contract.md) usada pela RoutingDecision. Esta proposta define a interface necessária ao roteamento; a política de execução define sua identidade, herança e composição.

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| mode | Enum | `fixed`, `rules` ou `typed_decision` |
| ranking_rules_ref | Referência ou null | Regras e desempate para `fixed`/`rules` e fallback, quando aplicável |
| decision_engine_ref | Referência ou null | Adaptador, provedor, modelo e configuração para `typed_decision` |
| input_template_ref | Referência versionada | Forma do estado e das perguntas apresentadas ao motor |
| confidence_threshold | Decimal ou null | Mínimo para aceitar a recomendação probabilística |
| low_confidence_action | Enum/configuração | `rules_fallback`, `fixed_fallback`, `human_review` ou `no_dispatch` |
| invalid_response_action | Enum/configuração | Tratamento de resposta ausente, fora do conjunto ou incompatível com o tipo |
| provider_error_action | Enum/configuração | Tratamento de timeout, indisponibilidade ou erro do motor |

O destino de fallback precisa estar no conjunto elegível no momento da decisão. O termo `safe_default` não será usado como prova de segurança: um fallback continua sujeito às mesmas permissões e verificações.

Aliases móveis de modelo podem ser usados em desenvolvimento, mas experimentos comparáveis devem registrar o identificador solicitado e o resolvido. Quando o provedor não expuser a revisão resolvida, o valor permanece desconhecido, não é inferido.

## RoutingDecision

RoutingDecision é o registro imutável e finalizado de uma operação de seleção. Seu ID é alocado antes da materialização do contexto, mas o registro só é finalizado após o preflight. O rascunho do ContextManifest usa o ID; depois referencia também o hash da decisão finalizada. Pode existir sem AttemptRecord quando nenhum destino for admitido.

| Campo | Tipo conceitual | Obrigatoriedade e significado |
| --- | --- | --- |
| schema_version | Texto | Obrigatório; versão do formato |
| routing_decision_id | Identificador | Obrigatório e único no run |
| run_id, workflow_ref e task_id | Referências | Obrigatórios; localizam a tarefa sem alterar TaskSpec |
| candidate_attempt_number | Inteiro positivo | Obrigatório; número pretendido, ainda não consumido antes da admissão |
| created_at | Data UTC | Obrigatória |
| catalog_snapshot_ref | Referência com hash | Obrigatória; perfis e destinos considerados |
| execution_policy_snapshot_ref | Referência com hash | Obrigatória; limites e permissões resolvidos para seleção |
| routing_policy_snapshot | Objeto/referência com hash | Obrigatório; política efetivamente usada |
| routing_input | Objeto identificado | Obrigatório; resumo apresentado ao ranking, sua ordem, hash e tamanho |
| candidates | Lista não vazia | Obrigatória; todos os destinos considerados e sua situação |
| recommendation | Objeto ou null | Escolha bruta do ranking, antes de fallback |
| effective_selection | Objeto ou null | Destino efetivo e razão; null significa não despachar |
| decision_status | Enum | `selected`, `no_eligible_target`, `low_confidence`, `invalid_response`, `provider_error` ou `preflight_rejected` |
| usage_record_refs | Lista | Duração, consumo e custo da decisão em UsageRecords; valores desconhecidos permanecem identificados |
| provider_extension | Objeto opcional | Dados específicos, namespaced e sem alterar a semântica central |

Cada candidato contém `target_ref`, posição de entrada, `eligible`, códigos de razão e referências das evidências. Para elegíveis, pode conter pontuação ou posição produzida pela política. Candidatos inelegíveis são preservados para auditoria, mas nunca enviados como opções selecionáveis ao motor probabilístico.

`routing_input` contém os IDs estáveis das opções e somente os atributos autorizados necessários à escolha. Objetivo, descrições e conteúdo recuperado são tratados como dados não confiáveis; não podem alterar a política, inventar candidatos ou instruir o Orchestrator. O hash cobre a representação canônica efetivamente enviada.

Para `typed_decision`, recommendation registra `target_id`, confiança, probabilidades ou pontuações disponíveis, identificador solicitado/resolvido do modelo, chamada externa e validação da resposta. Ausência de confiança segue a política de baixa confiança; não vira confiança máxima. Resposta fora do conjunto é inválida, mesmo que nomeie um destino existente no catálogo.

`effective_selection` distingue `recommended_target_ref` de `selected_target_ref`, informa se houve fallback e aponta sua regra. Uma decisão não registra justificativa textual do modelo como se fosse evidência de capacidade; elegibilidade deriva das verificações determinísticas.

## ContextManifest

ContextManifest identifica o conteúdo materializado pela plataforma para uma tentativa candidata. Ele registra o contexto fornecido pelo ORQ; conteúdo oculto ou acrescentado pelo provedor deve ser marcado como indisponível quando não puder ser observado.

| Campo | Tipo conceitual | Obrigatoriedade e significado |
| --- | --- | --- |
| schema_version | Texto | Obrigatório; versão do formato |
| context_manifest_id | Identificador | Obrigatório e único no run |
| run_id, workflow_ref e task_id | Referências | Obrigatórios |
| candidate_attempt_number | Inteiro positivo | Obrigatório; deve corresponder à decisão associada |
| routing_decision_ref | Referência com hash | Obrigatória; decisão que escolheu o destino |
| knowledge_selection_refs | Lista de referências com hash | Obrigatória; pode ser vazia somente quando não há KnowledgeRequirement nem conhecimento selecionado |
| target_ref | Referência com hash | Obrigatória; deve ser a seleção efetiva |
| created_at | Data UTC | Obrigatória |
| items | Lista ordenada | Obrigatória; pode estar vazia somente se os contratos da tarefa permitirem |
| omitted_items | Lista | Obrigatória; fontes avaliadas e não incluídas, com motivo |
| budget | Objeto | Obrigatório; limite, reserva de saída, tamanho estimado/medido e método |
| bundle_hash | Hash | Obrigatório; identidade da representação canônica montada pelo ORQ |
| provider_context_coverage | Enum | `complete`, `platform_only` ou `unknown` |

Cada item contém `sequence`, `item_id`, `category`, `source_ref`, `source_revision`, `source_hash`, `materialized_artifact_ref`, `content_hash`, `required`, `selection_reason`, tamanho e método de contagem. Itens vindos do Vault também registram seleção, requisitos cobertos, unidade e digests definidos no [contrato de metadata de conhecimento](knowledge-metadata-contract.md). Categorias iniciais: instruções operacionais, TaskSpec, entrada externa, artefato aceito de dependência, conhecimento de projeto e feedback de tentativa anterior.

Conteúdo transformado registra a cadeia de derivação: operação, ferramenta/versão, fonte e hash resultante. Resumo ou recorte nunca substitui silenciosamente um item obrigatório. Caminho mutável sem snapshot e hash não identifica conteúdo suficiente.

Credenciais, tokens de acesso e segredos de runtime não são itens de contexto persistidos. Quando uma referência autorizada precisa ser resolvida apenas no momento da execução, o manifesto registra a referência e a política de redação, nunca o valor secreto.

`budget` informa `target_context_limit_tokens`, `reserved_output_tokens`, `available_input_tokens`, `materialized_input_tokens`, método de contagem e margem aplicada. Valor desconhecido permanece null/unknown. Estimativa não é marcada como medição do provedor.

## Interfaces conceituais

| Operação | Entrada | Saída |
| --- | --- | --- |
| filter_candidates | Tarefa, papel, catálogo e política resolvidos | Elegíveis e exclusões justificadas |
| rank_candidates | Elegíveis, resumo de roteamento e RoutingPolicy | Recomendação validada ou estado de falha |
| resolve_selection | Recomendação, confiança e fallback | Seleção efetiva ou no_dispatch |
| materialize_context | Tarefa, destino, fontes autorizadas e orçamento | ContextManifest e bundle imutável, ou erro |
| final_preflight | Seleção, manifesto, orçamento e estado externo | Admissível ou rejeição identificada |

Essas operações podem ser funções locais no MVP. TypeSafe AI/Jev, quando experimentado, fica atrás de `rank_candidates`; não substitui o Orchestrator.

## Invariantes

1. Somente destinos deterministicamente elegíveis podem ser recomendados, selecionados ou usados como fallback.
2. Falta de capacidade, permissão ou evidência obrigatória não pode ser compensada por confiança ou pontuação.
3. A lista fechada enviada a um motor corresponde exatamente aos candidatos elegíveis registrados.
4. Resposta fora da lista, tipo inesperado ou confiança insuficiente segue a política e nunca produz seleção silenciosa.
5. A seleção efetiva referencia a mesma versão/hash do destino verificado.
6. ContextManifest referencia a RoutingDecision que escolheu seu destino; AttemptRecord referencia ambos.
7. A ordem e os hashes dos itens fazem parte da identidade do bundle.
8. Contexto obrigatório ausente, alterado ou acima do limite impede admissão.
9. Um novo retry gera nova RoutingDecision e novo ContextManifest; registros anteriores não são reescritos.
10. Custo do roteamento integra o consumo do run, mesmo quando não há tentativa de execução.
11. Falha prévia à admissão não consome tentativa; despacho persistido segue as regras do ciclo de execução.
12. Resultado de Jev é evidência da política de ranking, não evidência de qualidade, segurança ou capacidade do destino.

## Riscos e verificações

- **Confiança mal calibrada:** medir calibração no domínio do ORQ; limiar inicial é política experimental, não garantia.
- **Prompt injection no estado:** separar perguntas/esquema do conteúdo julgado, usar opções fechadas e validar a resposta novamente.
- **Mudança de alias ou provedor:** preservar identificadores solicitados/resolvidos, configuração, payload identificado e data.
- **Viés de descrições:** versionar o template e as descrições dos candidatos; comparar com baselines fixo e por regras.
- **Vazamento de contexto:** enviar ao ranking somente atributos autorizados e mínimos; não incluir artefatos completos por conveniência.
- **Circularidade de contexto:** usar envelope preliminar na elegibilidade e repetir a checagem com o bundle materializado.
- **Fallback inseguro:** revalidar elegibilidade e registrar por que recomendação e seleção efetiva diferem.
- **Observabilidade parcial:** não afirmar contexto completo quando runtime/provedor acrescentar conteúdo não observável.

## Casos específicos a formalizar

| ID | Caso | Resultado esperado |
| --- | --- | --- |
| R01 | Jev recomenda candidato elegível com confiança suficiente | Seleção registrada sem alterar TaskSpec |
| R02 | Jev retorna ID fora da lista fechada | `invalid_response`; fallback explícito ou nenhum despacho |
| R03 | Confiança abaixo do limiar | Ação configurada; recomendação bruta preservada |
| R04 | Motor de decisão indisponível | Custo/erro registrado; fallback somente se autorizado |
| R05 | Candidato com melhor ranking é inelegível | Nunca oferecido ao motor nem selecionado |
| R06 | Fallback deixou de ser elegível no preflight | Nenhum despacho; nova decisão, se permitida |
| R07 | Contexto obrigatório excede o limite do destino | Preflight rejeita sem truncamento silencioso |
| R08 | Item muda após materialização | Hash diverge; manifesto não autoriza despacho |
| R09 | Retry troca destino e inclui feedback anterior | Novos RoutingDecision e ContextManifest; histórico anterior preservado |
| R10 | Roteamento cobra, mas nenhuma tentativa é admitida | Consumo pertence ao run; tentativa não é contabilizada |

Esses casos são especificação para fixtures futuras, não testes executados nem validação real do Jev.

## Decisões ainda abertas

- Linguagem e biblioteca dos schemas executáveis.
- Valores de limiar, fallback padrão e política de revisão humana.
- Versão e adaptador concretos de TypeSafe AI/Jev para o primeiro experimento.
- Tokenizador e margem usados por cada executor.
- Conteúdo exato do resumo de roteamento e benchmark de calibração.

Antes de integrar Jev, concluir a revisão cruzada de RunManifest, AttemptRecord, [metadata de conhecimento](knowledge-metadata-contract.md), contabilização e [ExecutionPolicy](execution-policy-contract.md). A integração deverá começar com transporte simulado e respostas fixas, seguida de experimento isolado contra os mesmos casos usados pelos baselines.

## Referências externas

- [Apresentação oficial de System One Models e Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev), consultada em 2026-09-21.
- [Integração TypeSafe/Jev no Pydantic AI](https://pydantic.dev/docs/ai/models/typesafe/), consultada em 2026-09-21; usada como referência de comportamento de aliases, saídas tipadas e confiança, não como escolha de stack.
