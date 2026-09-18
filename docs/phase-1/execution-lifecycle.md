# Estados, limites e tentativas — proposta inicial

Estado: contrato de comportamento para revisão. Não existe execução implementada.

## Problema e alternativas

Falha técnica, reprovação do resultado, cancelamento solicitado e término desconhecido precisam de tratamentos diferentes.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Um estado por tarefa | Poucos campos | Perde histórico e confunde execução com avaliação |
| Tarefa, tentativas e avaliações separadas | Preserva causalidade e controla retries | Exige transições explícitas |
| Motor distribuído com leases e event sourcing integral | Suporta múltiplos workers | Complexidade sem necessidade no MVP local |

Recomendação: estados separados, controlados por um Orchestrator, com persistência e eventos identificados. Não pressupõe framework distribuído nem reconstrução integral por event sourcing.

## Limites e herança

WorkflowSpec.execution_policy_ref fornece a base. WorkflowSpec.constraints só a restringe. TaskSpec.constraints contém restrições locais adicionais e não possui outra referência de política.

Ausência significa herança. Para tetos, vale o menor limite aplicável; para permissões, a interseção. Um teto local explicitamente superior é erro de configuração. Lista de permissões vazia significa nenhuma permissão; lista ausente herda a restrição superior.

RunManifest registra limites resolvidos e origem. A estrutura completa de permissões permanece uma entrega posterior da Fase 1.

| Limite | Semântica |
| --- | --- |
| max_concurrency | Um no MVP; inclui tentativa e sua avaliação até liberar o slot |
| max_attempts | Total de tentativas por tarefa, incluindo a primeira; inteiro positivo |
| attempt_timeout_ms | Duração máxima da execução técnica |
| max_run_duration_ms | Duração desde o início do run, incluindo preparação, planejamento, espera e avaliação |
| max_evaluations_per_attempt | Total de avaliações sobre os mesmos artefatos, incluindo a primeira |
| evaluation_timeout_ms | Prazo por avaliação, inclusive a global |
| reconciliation_timeout_ms | Prazo de reconciliação, limitado pelo tempo restante do run |
| max_model_calls_per_attempt | Limite de chamadas internas quando observável e controlável |
| max_cost | Teto financeiro global com amount decimal e currency; opcional se outros limites finitos existirem |

Duração e quantidade de tentativas/avaliações precisam ter limites finitos resolvidos. Valores serão calibrados antes da Fase 6; números dos cenários são fictícios, não defaults.

O controle financeiro compara consumo conhecido e reservas antes de novas chamadas. A admissão de uma tentativa reserva também a avaliação necessária. Contabilizar Planner, Router por LLM, Evaluator e ferramentas cobradas. Reserva liquidada não é somada novamente como gasto.

Um runtime incapaz de observar ou controlar uma restrição obrigatória é inelegível. Custo indisponível não vira zero: sem estimativa conservadora e política explícita, não admitir execução sob teto financeiro obrigatório. Chamadas em andamento podem gerar custo antes da parada efetiva; teto de admissão não promete cobrança máxima exata pelo provedor.

## Tentativa e avaliação

Persistir attempt_id, attempt_number e intenção de despacho antes da chamada externa. max_attempts conta tentativas registradas, inclusive canceladas antes de iniciar. Erros anteriores à criação não consomem tentativa.

| Estado da tentativa | Significado | Transições possíveis |
| --- | --- | --- |
| queued | Registrada, sem início confirmado | running, failed, cancelled ou unknown |
| running | Execução iniciada | completed, failed, timed_out, cancelled ou unknown |
| completed | Resultado recebido e íntegro para avaliar | Terminal técnico |
| failed | Falha técnica e término confirmados | Terminal técnico |
| timed_out | Prazo excedido e encerramento confirmado | Terminal técnico |
| cancelled | Cancelamento confirmado | Terminal técnico |
| unknown | Despacho ou término incerto | running ou estado técnico terminal comprovado por reconciliação |

cancel_requested e deadline_exceeded são eventos; não provam término. Prazo excedido sem confirmação de parada produz unknown com motivo timeout.

Repetir a mesma intenção não deve gerar outro despacho lógico. Isso não garante execução exatamente uma vez no provedor: após queda entre envio e resposta, consultar o runtime e reconciliar antes de reenviar. Sem evidência suficiente, manter unknown.

O veredicto é pass, fail, inconclusive ou not_run. Qualquer critério obrigatório reprovado determina fail. Na ausência de reprovação, critério obrigatório inconclusivo ou não executado impede pass. Revisão textual não sobrepõe falha determinística obrigatória.

Reavaliar os mesmos artefatos após avaliação inconclusiva ou falha do verificador cria outro EvaluationReport dentro do limite. Uma avaliação compreende o conjunto de critérios, não apenas uma checagem individual. Não repetir um veredicto fail válido para tentar obter aprovação por variação do avaliador. Corrigir um artefato exige outra tentativa. Relatórios identificam hashes e são preservados; o Orchestrator registra qual avaliação fundamenta a aceitação.

## Estado da tarefa

| Origem | Condição | Destino |
| --- | --- | --- |
| pending | Todas as dependências aceitas | ready |
| pending | Dependência terminal sem sucesso | blocked |
| ready | Contexto, destino e orçamento aprovados; tentativa registrada | running |
| running | Tentativa completed | evaluating |
| running | Falha técnica confirmada com retry autorizado | retry_wait |
| running | Falha técnica confirmada sem retry | failed |
| running | Tentativa unknown | reconciling |
| reconciling | Resultado recuperado elegível para avaliação | evaluating |
| reconciling | Execução comprovadamente ativa dentro do prazo | running |
| reconciling | Falha confirmada | retry_wait ou failed, conforme política |
| reconciling | Prazo de reconciliação esgotado | indeterminate |
| evaluating | Término do verificador incerto | reconciling |
| evaluating | Critérios obrigatórios aprovados | succeeded |
| evaluating | Reprovação com retry autorizado | retry_wait |
| evaluating | Reprovação sem retry | failed |
| evaluating | Avaliação inconclusiva com nova avaliação permitida | waiting_evaluation |
| waiting_evaluation | Verificador disponível e orçamento permitido | evaluating |
| evaluating ou waiting_evaluation | Avaliação não concluída nos limites | failed, motivo evaluation_inconclusive |
| retry_wait | Término anterior confirmado e nova admissão aprovada | ready |
| ready ou retry_wait | Nenhum destino elegível ou orçamento suficiente | failed, motivo específico |

Indisponibilidade temporária pode manter ready ou retry_wait até o deadline, sem tentativas fictícias. succeeded, failed, blocked, cancelled e indeterminate são terminais dentro do run.

Cancelamento e reconciliação também incluem avaliadores. Se o término da avaliação for incerto, a tarefa fica reconciling, enquanto a tentativa técnica já concluída permanece completed. Não iniciar outra avaliação ou tentativa sem confirmar o encerramento. Um relatório recuperado pode ser processado em evaluating sem nova chamada ao verificador.

Cancelamento fecha admissões. Tarefas ainda não terminais e sem trabalho externo ativo ficam cancelled. Tarefas ativas exigem confirmação de parada; se faltar, passam por reconciling e podem terminar indeterminate. Cancelamento não autoriza retry.

## Retry e recuperação

Retry exige término anterior confirmado, falha recuperável, orçamento suficiente e tentativas restantes. O motivo é registrado. Trocar destino cria outra RoutingDecision sem alterar TaskSpec.

Cada tentativa nova começa em workspace limpo a partir do snapshot e dos artefatos aceitos das dependências. Pode receber relatório de falha anterior com procedência e orçamento de contexto. Não herda silenciosamente arquivos parcialmente alterados.

Crash não zera contadores nem gera outro run_id automaticamente. A retomada reconcilia registros pendentes antes de despachar. Se a persistência não confirmar a intenção, não chamar o runtime.

## Run e encerramento

Fluxo normal: created → running → succeeded. Preparação, planejamento e avaliação global pertencem a running. O manifesto das tarefas fecha após validar o plano, preservando registros anteriores ligados ao mesmo run_id.

Falha definitiva, deadline ou cancelamento leva a stopping: fechar admissões e encerrar trabalhos ativos. Só finalizar failed ou cancelled sem execução externa desconhecida. Se a reconciliação limitada não resolver a incerteza, finalizar indeterminate e manter o workspace indisponível para novas tentativas.

Em stopping, a confirmação tardia de resultado pode comprovar término, mas não inicia nova avaliação ou retry. As transições normais da tabela pressupõem run ativo e admissão ainda permitida. Tarefas não iniciadas são bloqueadas ou canceladas conforme a causa, e a parada das ativas é reconciliada.

failed indica falha conhecida, limite excedido ou avaliação global não aprovada. cancelled indica solicitação do usuário. succeeded exige os critérios globais de WorkflowSpec. A avaliação global tem prazo e uma única execução no MVP; não abre reparo automático do workflow.

Confirmações posteriores a um run terminal preservam horário e custos disponíveis, mas não reabrem tarefas nem disparam retries. Relatórios experimentais identificam o instante de corte. A gestão de recursos ainda ativos é uma ação de reconciliação, não uma nova tentativa.

## Riscos e verificações

Essas regras dependem de medição, cancelamento, identificação de chamadas e isolamento efetivamente disponíveis no executor. O catálogo declarará o que é suportado, estimado ou indisponível. attempt_id sozinho não garante recuperação segura.

Os [cenários de aceitação](acceptance-cases.md) especificam as evidências esperadas. Implementação e testes de runtime ficam para depois dos contratos restantes.
