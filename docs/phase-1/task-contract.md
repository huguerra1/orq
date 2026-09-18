# TaskSpec — proposta inicial

Estado: proposta documental. Ainda não é um schema executável nem encerra a Fase 1.

## Problema

Planner, Router, executor e avaliador precisam compartilhar o significado de tarefa. A especificação deve dizer o que entregar, com quais limites e como verificar a entrega, mantendo o destino de execução substituível.

## Alternativas

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Texto livre | Autoria simples | Referências, restrições e critérios ficam ambíguos |
| Objeto específico de um SDK | Integração rápida com um runtime | Acopla o domínio ao provedor |
| Contrato estruturado da plataforma | Validação e troca de executores | Exige definir semântica e validadores |

Recomendação: contrato estruturado próprio, com texto nos campos descritivos e referências explícitas nos campos operacionais.

## Campos

Todos os campos abaixo são obrigatórios na proposta, salvo indicação contrária. Listas podem estar vazias apenas quando a semântica permitir.

| Campo | Tipo conceitual | Semântica |
| --- | --- | --- |
| schema_version | Texto | Versão do formato de TaskSpec |
| task_id | Identificador | Único no workflow versionado |
| objective | Texto não vazio | Resultado concreto esperado |
| task_type | Identificador de catálogo | Categoria controlada, como analysis ou implementation |
| agent_role | Referência versionada | Papel responsável, independente do destino |
| requirements | Lista não vazia de Requirement | Requisitos identificados e sua obrigatoriedade |
| dependencies | Lista de task_id, sem duplicatas | Predecessoras cujos resultados devem ser aceitos; pode ser vazia |
| inputs | Lista de InputSpec | Artefatos externos ou saídas de predecessoras; pode ser vazia |
| required_capabilities | Lista de identificadores | Capacidades obrigatórias do destino; pode ser vazia |
| constraints | Objeto de limites | Política e eventuais limites mais restritivos da tarefa |
| expected_output | Lista não vazia de OutputSpec | Saídas identificadas, tipos e contratos |
| knowledge_requirements | Lista de KnowledgeRequirement | Conhecimento obrigatório ou opcional; pode ser vazia |
| evaluation_criteria | Lista não vazia de EvaluationCriterion | Critérios, evidências esperadas e requisitos cobertos |

## Objetos menores

- Requirement: requirement_id, descrição não vazia e indicação de obrigatoriedade.
- InputSpec: input_id, tipo e origem. A origem é uma referência a artefato existente ou a uma saída nomeada de tarefa predecessora. Não referencia um artefato futuro como se já existisse.
- OutputSpec: output_id, tipo de artefato, descrição e referência de contrato de conteúdo quando aplicável.
- KnowledgeRequirement: identificador, assunto ou referência documental, escopo e indicação de obrigatoriedade.
- EvaluationCriterion: criterion_id, requirement_ids cobertos, método, regra de aprovação, obrigatoriedade e referência de verificador ou rubrica.
- Constraints: referência versionada à política de execução e limites explícitos opcionais para a tarefa. Nenhuma ausência de campo significa autorização ilimitada.

Os critérios podem usar validação de contrato, testes automatizados, verificação humana ou rubrica por LLM. Um critério obrigatório sem implementação disponível produz avaliação não conclusiva; não produz aprovação automática.

## Relação com os demais contratos

- WorkflowSpec contém as tarefas e define o espaço de identificação das dependências.
- Perfis permitem validar o papel e descobrir destinos elegíveis.
- RoutingDecision guarda a escolha de destino; ela não é gravada como parte da intenção imutável da tarefa.
- AttemptRecord guarda execução, contexto, consumo, erros e resultados.
- EvaluationReport identifica quais artefatos foram avaliados e quais critérios foram atendidos.

## Regras de validação

1. A tarefa não depende de si própria.
2. Todas as dependências existem no workflow; o conjunto completo não pode conter ciclos.
3. IDs de requisitos, entradas, saídas e critérios não se repetem em suas respectivas listas.
4. Todo requisito obrigatório é coberto por pelo menos um critério obrigatório.
5. Todos os requirement_ids citados por critérios existem na tarefa.
6. Uma entrada produzida por outra tarefa referencia uma saída declarada por uma dependência direta.
7. O papel e as capacidades referenciados existem no catálogo aplicável.
8. Limites de tarefa não ampliam a autorização das políticas superiores.
9. Falta de conhecimento obrigatório impede a execução até que a necessidade seja resolvida.
10. Estados, modelos selecionados e consumo observado pertencem à execução, não ao TaskSpec.

Estrutura e tipos serão responsabilidade do schema. Referências, ciclos e consistência entre entidades exigirão validação semântica. Aplicação efetiva de permissões e limites pertence à execução.

## Exemplo de revisão, sem código

T3 implementa o desenho de autenticação aceito em T2. Seu papel é backend_engineer e sua dependência é T2. A entrada referencia a saída solution_design declarada por T2, além do snapshot de código disponibilizado pelo run. As saídas são um patch vinculado à base e um relatório. Os requisitos de comportamento têm critérios identificados. Arquitetura do backend e regras de segurança são conhecimento obrigatório. O destino é escolhido posteriormente, entre executores elegíveis.

## Casos de aceitação a formalizar

| Caso | Resultado esperado |
| --- | --- |
| Tarefa válida com destinos diferentes | TaskSpec permanece igual |
| Objetivo vazio | Rejeição estrutural |
| Dependência inexistente ou autorreferência | Rejeição semântica |
| Saída predecessora inexistente | Rejeição semântica |
| Requisito obrigatório sem critério obrigatório | Rejeição semântica |
| Critério cita requisito inexistente | Rejeição semântica |
| Executor não suporta capacidade obrigatória | Destino inelegível, sem alteração da tarefa |
| Resultado entregue, mas reprovado | Tentativa concluída tecnicamente; tarefa ainda não aceita |

## Pontos para a próxima revisão

- Especificar WorkflowSpec, referências versionadas e orçamento global.
- Definir a resolução e combinação dos limites de execução, incluindo max_attempts e deadlines.
- Definir formatos de artefato para patch e relatório.
- Especificar estados e transições da tarefa e da tentativa em documento próprio.

Depois de fechar essas relações, o próximo passo de implementação será formalizar schemas e fixtures, sem instalar SDKs de agentes ou construir o servidor MCP.
