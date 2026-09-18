# Glossário da plataforma

Estado: proposta inicial da Fase 1.

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
| Run | Uma execução concreta de um objetivo/plano | Agrupa decisões, tentativas, resultados e consumo |
| Attempt | Uma tentativa de executar uma tarefa em um run | Retry cria outra tentativa e preserva a anterior |
| Artifact | Saída identificável, como patch, relatório ou evidência de testes | Sua revisão e hash identificam o conteúdo |
| Context Bundle | Conteúdo materializado para uma tentativa | Deve respeitar escopo, permissões e orçamento |
| Context Manifest | Registro dos itens, versões, ordem e identidade do contexto | Caminhos de arquivos sem snapshots não bastam |
| Operational Knowledge | Conhecimento sobre papéis, modelos e funcionamento da plataforma | Apenas fontes autorizadas podem definir instruções operacionais |
| Project Knowledge | Conhecimento sobre o projeto de destino | Conteúdo recuperado não pode ampliar permissões |
| Execution Memory | Histórico factual de decisões e execuções | Não equivale ao conhecimento editorial do Vault |
| Model Router | Módulo que filtra e ordena destinos elegíveis | Não ignora restrições obrigatórias para melhorar uma pontuação |
| Knowledge Router | Módulo que seleciona necessidades e compõe contexto limitado | Não envia todo o Vault indiscriminadamente |
| Scheduler | Módulo que escolhe tarefas prontas e propõe alocações | Respeita dependências, elegibilidade, recursos e orçamento |
| Orchestrator | Componente que controla o ciclo global e as transições de estado | Um eventual papel de LLM com esse nome não possui sua autoridade |
| Evaluation | Verificação do resultado contra critérios versionados | Conclusão técnica do executor não equivale a aprovação |

## Convenções iniciais

- task_id é único dentro de uma versão do workflow; referências persistidas também identificam o workflow e sua versão.
- run_id e attempt_id identificam ocorrências concretas, sem reutilização para uma nova execução.
- schema_version identifica o formato; versão/revisão da entidade identifica seu conteúdo.
- Datas são registradas em UTC; durações e quantidades possuem unidades explícitas.
- Dados desconhecidos não são representados por zero ou por uma estimativa sem identificação.
- Uma mudança de plano aceito produz nova revisão; não reescreve o histórico da execução.
- Metadados específicos de um provedor ficam nos adaptadores ou em extensões identificadas, sem contaminar o contrato central de tarefa.
