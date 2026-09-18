# Perfis de papel, modelo e executor

Estado: proposta documental da Fase 1. Não configura provedores nem implementa executores.

## Problema e alternativas

Precisamos trocar o destino de execução sem alterar a tarefa e distinguir capacidade técnica, permissão e qualidade observada.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Papel vinculado a um provedor | Configuração simples | Impede substituição sem alterar o papel |
| Perfil único de agente | Poucas estruturas | Mistura instruções, modelo, ferramentas e medição |
| Três perfis e um destino composto | Responsabilidades claras e comparação rastreável | Exige validar compatibilidade entre referências |

Recomendação: três contratos de dados carregados por um catálogo local. Não criar serviços separados para cada perfil. O Router usa o catálogo para verificar elegibilidade antes de ordenar destinos.

## Convenções comuns

Cada perfil contém schema_version, seu identificador, version e status. status admite active, deprecated ou disabled. No MVP, somente active participa de novas seleções; os demais permanecem consultáveis pelo histórico.

Uma versão publicada não muda de conteúdo. O catálogo resolve referências por identidade e versão e calcula content_hash. Arquivos editoriais podem ser revisados, mas o run preserva o snapshot utilizado. Revisões conflitantes com a mesma identidade e versão são rejeitadas.

O catálogo só carrega fontes operacionais autorizadas. Um documento de projeto não pode se declarar confiável pelo próprio frontmatter. Credenciais e chaves privadas nunca fazem parte desses perfis.

## AgentRoleProfile

| Campo específico | Tipo | Obrigatoriedade e significado |
| --- | --- | --- |
| role_id | Identificador | Obrigatório; por exemplo backend_engineer |
| responsibilities | Lista de textos | Obrigatória e não vazia |
| supported_task_types | Lista de IDs | Obrigatória e não vazia; categorias permitidas |
| required_capabilities | Lista de IDs | Obrigatória; pode ser vazia |
| permission_ceiling | Objeto de permissões | Obrigatório; teto de ferramentas, escrita e acesso |
| instructions_ref | Referência versionada | Obrigatória; instruções operacionais resolvidas e identificadas |
| output_contract_refs | Lista de referências | Obrigatória; contratos de saída aceitos pelo papel, não vazia |

O papel descreve responsabilidade e limites; não contém model_id, provider_id ou executor_id fixos. Trocar um destino não cria outra versão do papel.

As capacidades exigidas são a união das exigências do papel e da tarefa. task_type deve ser aceito pelo papel. As saídas da tarefa devem ser compatíveis com seus contratos de saída; o validador verifica referências e tipos, não apenas nomes semelhantes.

## ModelProfile

| Campo específico | Tipo | Obrigatoriedade e significado |
| --- | --- | --- |
| model_id | Identificador local | Obrigatório |
| provider_id | Identificador de catálogo | Obrigatório; serviço que oferece o modelo |
| native_model_id | Texto | Obrigatório; identificador solicitado ao provedor |
| model_revision | Texto ou null | Obrigatório; null quando a revisão efetiva não é exposta |
| capabilities | Lista de declarações | Obrigatória; pode ser vazia sem evidência disponível |
| context_limit_tokens | Inteiro positivo ou null | Obrigatório; null significa desconhecido |
| max_output_tokens | Inteiro positivo ou null | Obrigatório; null significa desconhecido |
| source_refs | Lista de referências | Obrigatória e não vazia; fontes das declarações |
| verified_at | Data UTC | Obrigatória; momento da última verificação |
| specializations | Lista de textos | Opcional; hipóteses editoriais para ranking, não prova de capacidade |

A versão deste perfil é diferente da revisão do modelo. Um alias do provedor pode mudar sem alteração do texto solicitado; AttemptRecord registrará o identificador solicitado e o resolvido, quando disponível.

Médias de custo, latência e sucesso não são campos editoriais deste contrato. O Router recebe um snapshot separado de métricas, com população e janela de observação. Sem histórico, usa regras e registra a ausência de evidência.

Não preencher limites ou características com palpites. Requisito obrigatório dependente de limite desconhecido exige verificação antes da admissão.

## ExecutorProfile

| Campo específico | Tipo | Obrigatoriedade e significado |
| --- | --- | --- |
| executor_id | Identificador | Obrigatório; adaptador da plataforma |
| adapter_version | Texto | Obrigatório; versão da integração |
| runtime_id | Identificador | Obrigatório; ambiente de execução ou ciclo próprio da aplicação |
| runtime_version | Texto | Obrigatório; versão identificável do ambiente usado |
| compatible_model_refs | Lista de referências | Obrigatória e não vazia; combinações explicitamente suportadas |
| capabilities | Lista de declarações | Obrigatória; operações e controles efetivamente disponíveis |
| supported_output_contract_refs | Lista de referências | Obrigatória e não vazia |
| permission_ceiling | Objeto de permissões | Obrigatório; alcance máximo que o adaptador pode oferecer |
| telemetry | Mapa de disponibilidade | Obrigatório; tokens de entrada/saída, custo e duração |
| verified_at | Data UTC | Obrigatória; data da verificação do adaptador |

Cada item de telemetry declara reported, estimated ou unavailable, com origem da informação. Isso descreve a disponibilidade esperada; os valores e sua procedência efetiva pertencem a cada tentativa. Capacidade de estimar custo não equivale a conhecer a cobrança final.

Controles relevantes incluem isolamento do workspace, restrição de ferramentas, aplicação de timeout, confirmação de cancelamento, identificação de chamada e reconciliação. Não declarar todos como suportados sem verificá-los. Os testes reais desses controles serão feitos na fase do executor.

## Declarações de capacidade e permissões

CapabilityDeclaration contém capability_id, support (supported, unsupported ou unknown), evidence_ref e verified_at. Uma afirmação supported exige evidência identificável e adequada à combinação de versões; unknown não atende a requisito obrigatório.

IDs de capacidade pertencem a um vocabulário controlado, que informa a camada responsável: modelo, runtime ou controle do host/adaptador. Ferramentas de filesystem pertencem ao ambiente de execução, não ao modelo. Qualidade de programação é uma medida de resultado, não um booleano de capacidade.

Permissão e capacidade são verificadas separadamente: suportar escrita não significa estar autorizado a escrever. A permissão efetiva é a interseção dos limites de sistema/projeto, workflow, papel, executor e tarefa. Um perfil não amplia limites superiores.

permission_ceiling explicita ferramentas autorizáveis, áreas de leitura/escrita e acesso à rede. Lista vazia nega acesso. A gramática de caminhos e a estrutura completa de ExecutionPolicy ainda precisam ser fechadas na Fase 1; não serão delegadas a texto livre de prompt. Implementação deverá verificar caminhos resolvidos, inclusive links simbólicos.

## ExecutionTarget e elegibilidade

ExecutionTarget é um objeto composto, não outro serviço. Contém target_id, model_ref, executor_ref e execution_config_ref, todos resolvidos para versões e hashes. A configuração inclui opções do runtime e limites efetivos; credenciais permanecem fora dela.

Operações conceituais:

| Operação | Entrada | Saída |
| --- | --- | --- |
| resolve_profile | Tipo, identidade e versão | Perfil e hash, ou erro de referência |
| check_eligibility | TaskSpec, papel, destino e política resolvida | eligible, reasons e referências da evidência |
| list_candidates | Tarefa e snapshot do catálogo | Destinos elegíveis e exclusões justificadas |

Verificar antes do ranking: referências ativas; task_type; compatibilidade executor/modelo; capacidades obrigatórias; permissões; contratos de saída; contexto e controles exigidos pelos limites. Disponibilidade de credenciais, quota e ambiente é verificada em preflight sem alterar o perfil estático. A composição final do contexto passa por nova checagem antes do despacho.

A evidência deve ser válida para a versão e para a política de validade aplicável. Não escolher um prazo de expiração universal neste contrato; a política deverá defini-lo. Provas vencidas ou ausentes não sustentam um requisito obrigatório.

Sem candidatos, retornar no_eligible_target com exclusões. Falta de permissão ou capacidade obrigatória não pode ser compensada por pontuação de qualidade. O Router ordena apenas destinos elegíveis e registra sua política; RoutingDecision será detalhado na próxima entrega.

## Exemplo conceitual

A mesma tarefa de backend requer escrita em workspace isolado, execução de testes e confirmação de cancelamento. O destino A oferece esses controles com evidência válida. O destino B possui bom histórico de programação, mas cancelamento unknown. Somente A é elegível quando esse controle é obrigatório.

Se ambos atenderem às restrições, a política pode escolher qualquer um sem modificar TaskSpec ou AgentRoleProfile. O resultado mede a combinação de papel, modelo, runtime, configuração e contexto, não apenas o nome do modelo.

## Riscos e casos de aceitação

Riscos principais: perfis desatualizados, permissões confundidas com capacidades, texto editorial tratado como medição e incompatibilidade entre versões. Cada seleção deve preservar o snapshot e as razões verificáveis.

| ID | Caso | Resultado esperado |
| --- | --- | --- |
| P01 | Mesmo papel em dois destinos elegíveis | Papel e TaskSpec permanecem iguais |
| P02 | Modelo não listado como compatível com executor | Destino excluído |
| P03 | Capacidade obrigatória unknown | Destino excluído até verificação |
| P04 | Runtime suporta escrita, mas política nega | Nenhuma escrita autorizada |
| P05 | Limite obrigatório depende de valor desconhecido | Nenhuma admissão baseada em palpite |
| P06 | Revisões conflitantes com mesmo ID/versão | Catálogo rejeita o conflito |
| P07 | Perfil disabled/deprecated | Consultável no histórico, excluído de nova seleção |
| P08 | Falta de métricas históricas | Regras explícitas, sem fabricar taxas ou custos |

Esses oito casos complementam os [32 cenários gerais](acceptance-cases.md); ainda não foram executados.

## Próxima etapa

Definir ContextManifest e RoutingDecision, incluindo candidatos considerados, evidências, conteúdo enviado e orçamento de contexto. Depois fechar ExecutionPolicy, registros de execução, artefatos, avaliação e metadata de conhecimento. Só após a revisão correspondente implementar schemas e validadores.
