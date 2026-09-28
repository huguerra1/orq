# Glossário da plataforma

Estado: baseline normativa v0.1 aceita na revisão cruzada.

| Termo | Definição | Fronteira |
| --- | --- | --- |
| Objetivo | Resultado de alto nível solicitado pelo usuário | Pode exigir várias tarefas |
| TaskSpec | Especificação de uma unidade de trabalho verificável | Não contém o modelo selecionado nem o estado de execução |
| WorkflowSpec | Conjunto versionado de tarefas, dependências e critérios finais | É uma definição; uma execução concreta recebe run_id |
| DAG | Grafo dirigido sem ciclos que representa dependências | Não garante ausência de conflitos de escrita |
| Agent Role | Responsabilidade, instruções e permissões máximas de um papel | Não identifica um modelo ou um processo permanente |
| Model | Modelo efetivamente utilizado na inferência | Registrar identificador solicitado e resolvido, quando disponível |
| Provider | Serviço que disponibiliza o modelo | Sua identidade não substitui a do runtime |
| Runtime | Ambiente que conduz o ciclo interno de ferramentas e contexto do agente | Tem versão, configuração e capacidades próprias |
| Executor | Adaptador da plataforma para um runtime ou API | Converte pedidos e resultados, sem alterar o plano global |
| Execution Target | Combinação concreta de executor, runtime, provedor, modelo e configuração | Precisa atender às restrições da tarefa |
| Execution Policy | Política versionada de limites, permissões, orçamento, roteamento e validade de evidências | Não prova que o executor consegue aplicar os controles |
| Run | Uma execução concreta de um objetivo/plano | Agrupa decisões, tentativas, resultados e consumo |
| Run Manifest | Snapshot imutável das condições resolvidas para um run | Não contém decisões ou resultados futuros |
| Attempt | Uma tentativa de executar uma tarefa em um run | Retry cria outra tentativa e preserva a anterior |
| Journal Event | Intenção ou fato append-only ligado a um agregado e sequência | Projeções de estado não substituem seu histórico |
| Usage Record | Medição reportada, estimada ou indisponível de uma operação | Correções preservam o registro anterior |
| Approval Record | Decisão de autoridade sobre ação, alvo e validade específicos | Não amplia a Execution Policy |
| Artifact | Conteúdo imutável ingerido ou produzido, como snapshot, patch, relatório ou evidência | Produção não implica aceitação |
| ArtifactRef | Descritor da ocorrência, digest, tipo, contrato, locator e procedência de um artefato | Hash identifica bytes; artifact_id identifica ocorrência |
| Patch Application | Aplicação verificada de um patch a uma base exata | Resultado não altera o PatchArtifact original |
| Context Bundle | Conteúdo materializado para uma tentativa | Deve respeitar escopo, permissões e orçamento |
| Context Manifest | Registro dos itens, versões, ordem e identidade do contexto | Caminhos de arquivos sem snapshots não bastam |
| Operational Knowledge | Conhecimento sobre papéis, modelos e funcionamento da plataforma | Apenas fontes autorizadas podem definir instruções operacionais |
| Project Knowledge | Conhecimento sobre o projeto de destino | Conteúdo recuperado não pode ampliar permissões |
| Knowledge Source | Documento editorial identificado por source_id, revisão e digest | Não atribui autoridade ou escopo a si próprio |
| Knowledge Unit | Trecho recuperável derivado deterministicamente de uma revisão exata | Não amplia classificação, aplicabilidade ou autoridade da fonte |
| Knowledge Catalog Snapshot | Conjunto imutável de fontes, unidades e regras disponível para um run | Fecha o universo consultável; não registra o contexto efetivamente enviado |
| Knowledge Selection Record | Registro de candidatos, exclusões, ranking e cobertura de conhecimento para uma tarefa | Context Manifest registra depois os bytes materializados e sua ordem |
| Execution Memory | Histórico factual de decisões e execuções | Não equivale ao conhecimento editorial do Vault |
| Model Router | Módulo que filtra e ordena destinos elegíveis | Não ignora restrições obrigatórias para melhorar uma pontuação |
| Routing Policy | Política versionada que ordena candidatos já elegíveis e resolve confiança/fallback | Não concede capacidades, permissões ou orçamento |
| Routing Decision | Registro imutável dos candidatos, exclusões, política, recomendação e seleção efetiva | Pode existir sem tentativa quando não há admissão |
| Knowledge Router | Módulo que seleciona necessidades e compõe contexto limitado | Não envia todo o Vault indiscriminadamente |
| Scheduler | Módulo que escolhe tarefas prontas e propõe alocações | Respeita dependências, elegibilidade, recursos e orçamento |
| Orchestrator | Componente que controla o ciclo global e as transições de estado | Um eventual papel de LLM com esse nome não possui sua autoridade |
| Evaluation | Verificação do resultado contra critérios versionados | Conclusão técnica do executor não equivale a aprovação |
| Evaluation Report | Resultado imutável de critérios sobre artifact_ids/digests exatos | Não se aplica a conteúdo alterado |
| Tentativa concluída | Execução técnica terminou e disponibilizou resultado | completed não implica avaliação pass |
| Resultado indeterminado | Despacho ou encerramento externo não confirmado | Bloqueia retry até reconciliação segura |
| Reconciliação | Busca de evidência sobre execução de estado desconhecido | Não é retry e possui prazo limitado |
| Entrada externa | Insumo declarado pelo workflow e concretizado pelo run | Sua identidade não é um caminho mutável |

## Convenções iniciais

- task_id é único dentro de uma versão do workflow; referências persistidas também identificam o workflow e sua versão.
- run_id e attempt_id identificam ocorrências concretas, sem reutilização para uma nova execução.
- schema_version identifica o formato; versão/revisão da entidade identifica seu conteúdo.
- Datas são registradas em UTC; durações e quantidades possuem unidades explícitas.
- Dados desconhecidos não são representados por zero ou por uma estimativa sem identificação.
- Uma mudança de plano aceito produz nova revisão; não reescreve o histórico da execução.
- Metadados específicos de um provedor ficam nos adaptadores ou em extensões identificadas, sem contaminar o contrato central de tarefa.
- max_attempts conta todas as tentativas registradas, incluindo a primeira; retry_count conta somente as posteriores à primeira.
- A política de execução é referenciada no workflow. Restrições de tarefa herdam seus limites e só podem reduzi-los.
