# WorkflowSpec — proposta inicial

Estado: especificação documental para revisão. Nenhum scheduler ou schema executável foi implementado.

## Problema e alternativas

O workflow precisa representar dependências, insumos, entregas e condições de sucesso sem embutir a escolha do modelo nem o estado da execução.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Lista sequencial | Execução simples | Não distingue dependência de ordem arbitrária |
| DAG estático versionado | Validação prévia e base para paralelismo | Mudanças exigem outra revisão |
| Grafo alterável durante a execução | Adaptação direta | Complica estados, auditoria e comparação experimental |

Recomendação: DAG estático versionado, executado sequencialmente no MVP. Todas as tarefas são obrigatórias nesta versão. Branches condicionais, tarefas opcionais, revisão dinâmica e execução distribuída ficam fora deste contrato inicial.

## Campos

Todos os campos são obrigatórios. Listas podem ser vazias somente quando indicado.

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| schema_version | Texto | Versão do formato |
| workflow_id | Identificador | Identidade estável do workflow |
| version | Texto de versão | Revisão imutável após aceitação |
| project_id | Identificador | Um projeto de destino por workflow |
| objective | Texto não vazio | Resultado de alto nível |
| requirements | Lista não vazia de Requirement | Requisitos globais, com ao menos um obrigatório, distintos dos locais |
| external_inputs | Lista de declarações | Insumos vinculados pelo run; pode ser vazia |
| tasks | Lista não vazia de TaskSpec | Tarefas com IDs únicos |
| requirement_coverage | Lista não vazia de vínculos | Requisitos globais ligados a requisitos de tarefas |
| outputs | Lista não vazia de saídas | Entregas finais com referências às saídas das tarefas |
| evaluation_criteria | Lista não vazia de critérios | Aceitação final do objetivo, com ao menos um critério obrigatório |
| execution_policy_ref | Referência versionada | Política de limites e permissões de base |
| constraints | Objeto | Restrições adicionais do workflow; pode ser vazio se a política já resolver todos os limites |

Cada vínculo de cobertura contém workflow_requirement_id, task_id e task_requirement_id. Requisitos locais são interpretados dentro da tarefa referenciada.

Cada entrada externa declara input_id, tipo, descrição e contrato de conteúdo quando aplicável. Nesta versão todas as entradas externas declaradas são obrigatórias. Cada saída final declara output_id, tipo, source_task_id e source_output_id. Critérios globais declaram os output_ids usados como evidência e os requisitos globais cobertos.

O run vincula entradas externas a ArtifactRefs concretos. Assim, o plano pode declarar repository_snapshot sem fixar um caminho mutável nem inventar o hash de uma entrega futura.

## Dependências e referências

TaskSpec.dependencies é a única fonte das arestas. Se T3 depende de T2, a aresta é T2 → T3. Não existe uma segunda lista editável de arestas.

Uma entrada de tarefa possui uma das origens:

- external_input: input_id declarado pelo workflow e vinculado a artefato no run.
- task_output: task_id de uma dependência direta e output_id declarado por ela.

Dependências podem representar apenas precedência. Uma entrada task_output exige a dependência correspondente. Não inferimos dependências por nomes de arquivos.

Referências a perfis e políticas contêm identidade e versão; o RunManifest também preserva seus hashes. Referências entre tarefas usam task_id dentro da mesma versão do workflow. Registros persistidos incluem workflow_id e version para evitar ambiguidade.

## Interfaces

| Operação conceitual | Entrada | Saída |
| --- | --- | --- |
| validate_workflow | WorkflowSpec e catálogo versionado | Relatório sem efeitos de execução |
| bind_inputs | Plano válido e vínculos externos | Referências verificadas ou erros |
| derive_ready_tasks | Plano e estado do run | Tarefas cujas dependências foram aceitas |
| select_dispatch | Tarefas prontas, destinos elegíveis e orçamento | Proposta de alocação ou motivo para aguardar |

Uma tarefa pronta ainda pode não ser despachável: conhecimento, destino ou orçamento podem faltar. O Orchestrator verifica essas condições antes de criar a tentativa. Essas operações podem ser funções locais; não exigem serviços ou tools MCP próprios.

ValidationReport contém valid, errors e warnings. Cada erro possui code, path e message, além de IDs relacionados quando aplicável. path identifica o campo inválido. Só ausência de erros permite aceitar o plano; avisos não resolvem decisões necessárias à execução.

## Invariantes

1. IDs de tarefa são únicos; dependências existem, não se repetem e não apontam para a própria tarefa.
2. O grafo é acíclico, inclusive em componentes desconectados.
3. Entradas externas, saídas finais, requisitos globais e critérios têm IDs únicos nas respectivas listas.
4. Referências de entradas, saídas, cobertura e critérios existem e seus tipos são compatíveis.
5. Cada requisito global obrigatório tem cobertura por requisito local obrigatório e critério global obrigatório. O vínculo estrutural não prova suficiência da descrição em linguagem natural.
6. Cada requisito local obrigatório tem critério local obrigatório, conforme TaskSpec.
7. Todos os insumos externos são vinculados a artefatos autorizados e íntegros antes de executar tarefas.
8. Uma saída final referencia o artefato da tentativa aceita. Artefatos reprovados não satisfazem dependências.
9. Alterar plano aceito cria outra versão. No MVP, a execução dessa versão começa em outro run; não há reaproveitamento automático de resultados.
10. Sucesso exige todas as tarefas aceitas, saídas finais disponíveis e avaliação global aprovada. Aprovações locais não dispensam verificar o resultado integrado.

## Política inicial

Concorrência igual a um. Entre tarefas prontas, desempatar pela ordem da lista tasks e registrar a decisão. Essa ordem nunca supera dependências.

O Router fornece destinos elegíveis ordenados; o Scheduler inicial usa o primeiro. Uma estratégia futura pode escolher outro candidato elegível, registrando a diferença, sem alterar WorkflowSpec.

Na primeira falha definitiva de tarefa, parar novas admissões. Dependentes diretos e transitivos ficam blocked; tarefas independentes não iniciadas ficam cancelled com motivo workflow_failed. Falha de tentativa com retry permitido ainda não é falha definitiva de tarefa.

## Exemplo de referência

| Tarefa | Dependências | Insumos | Saídas |
| --- | --- | --- | --- |
| T1 — analisar | Nenhuma | repository_snapshot externo | architecture_report |
| T2 — projetar | T1 | T1.architecture_report | solution_design |
| T3 — implementar | T2 | T2.solution_design e repository_snapshot | code_patch, implementation_report |
| T4 — testar | T3 | T3.code_patch e repository_snapshot | test_patch, test_report |
| T5 — revisar | T2, T3, T4 | T2.solution_design, T3.code_patch, T4.test_patch e T4.test_report | review_report |

As saídas finais incluem os patches e relatórios necessários à avaliação global. Antes dessa avaliação, o executor prepara uma cópia limpa do snapshot, aplica code_patch e depois test_patch e verifica o resultado integrado. test_patch declara como base o resultado de code_patch. O formato exato dos artefatos será especificado depois; esta é sua relação semântica.

T5 depende diretamente de T2 porque consome sua saída; um ancestral indireto não basta.

## Riscos, verificações e limite da entrega

Um DAG válido não prova que o objetivo foi atendido nem evita conflitos de arquivos. Isolamento do workspace e verificação final serão necessários antes de executar código.

A matriz de [aceitação](acceptance-cases.md) cobre grafo, referências e resultado integrado. [Estados e limites](execution-lifecycle.md) complementam o contrato. Após fechar a Fase 1, a implementação começa pelos validadores e fixtures; não pelo scheduler concorrente.
