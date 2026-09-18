# Cenários de aceitação — Fase 1

Estado: especificação dos testes futuros. Não são testes executáveis nem resultados de provedores.

## Estratégia

JSON Schema verificará campos e tipos; validação semântica verificará referências e grafo; executor simulado verificará posteriormente o ciclo. Testes de integração real pertencem às fases dos executores.

| Alternativa | Vantagem | Limitação |
| --- | --- | --- |
| Somente exemplos de sucesso | Revisão rápida | Não especifica falhas e recuperação |
| Casos positivos e negativos determinísticos | Reproduzíveis e independentes de provedor | Não comprovam comportamento externo |
| Agentes reais desde agora | Observa o runtime | Mistura erros de contrato com variabilidade e custo |

Recomendação: matriz determinística primeiro. Nenhuma fixture inicial depende de rede, credenciais ou nomes reais de modelos.

## Matriz

| ID | Dado / ação | Resultado esperado | Camada futura |
| --- | --- | --- | --- |
| C01 | Objetivo vazio ou campo obrigatório ausente | Rejeição com path e code | Schema |
| C02 | Uma tarefa válida sem dependências | Plano aceito | Validação |
| C03 | Ramificação seguida de junção válida | Junção aguarda todos os pais | Validação e ciclo |
| C04 | IDs de tarefa repetidos | Rejeição por duplicação | Semântica |
| C05 | Dependência inexistente ou autorreferência | Rejeição localizada | Semântica |
| C06 | Ciclo, inclusive em componente desconectado | Rejeição do workflow inteiro | Semântica |
| C07 | task_output inexistente ou sem dependência direta | Rejeição da referência | Semântica |
| C08 | Entrada externa sem declaração ou vínculo concreto | Rejeição do plano ou do vínculo, respectivamente | Semântica |
| C09 | Requisito obrigatório sem cobertura/critério obrigatório | Rejeição com identificação do requisito | Semântica |
| C10 | Critério referencia requisito ou saída inexistente | Rejeição da referência | Semântica |
| C11 | Trocar entre dois destinos elegíveis | TaskSpec permanece idêntico | Contrato |
| C12 | Destino sem suporte a restrição obrigatória | Exclusão com motivo, sem execução | Roteamento |
| C13 | Override amplia limite superior | Rejeição de configuração | Política |
| C14 | max_attempts igual a dois | No máximo duas tentativas registradas | Ciclo |
| C15 | Tentativa completed e avaliação fail | Tarefa não aceita; dependentes não liberadas | Ciclo |
| C16 | Critério obrigatório inconclusivo | Não aprovar; reavaliar dentro do limite ou falhar | Avaliação |
| C17 | Artefato muda depois de avaliação pass | Aprovação não vale para o novo hash | Artefatos |
| C18 | Retry de tentativa reprovada | Novo attempt_id, workspace limpo, histórico preservado | Recuperação |
| C19 | Timeout sem término confirmado | unknown/reconciling; nenhum segundo despacho | Recuperação |
| C20 | Reconciliação comprova execução ativa dentro do prazo | Observar a mesma tentativa, sem reenviar | Recuperação |
| C21 | Prazo de reconciliação esgotado | Tarefa/run indeterminate; workspace não reutilizado | Recuperação |
| C22 | Cancelamento com processo ativo | Fechar admissões; confirmar parada antes de cancelled | Ciclo |
| C23 | Crash após intenção e antes da resposta | Reconciliar sem presumir ausência de execução | Recuperação |
| C24 | Evento ou despacho repetido | Uma contabilização e um despacho lógico | Persistência |
| C25 | Falha definitiva de tarefa | Dependentes blocked; demais não iniciadas cancelled | Workflow |
| C26 | Tarefas aceitas, avaliação global fail | Run failed | Workflow |
| C27 | Custo indisponível | Preservar desconhecido e cobertura da medição | Métricas |
| C28 | Orçamento cobre execução mas não avaliação reservada | Não admitir tentativa | Orçamento |
| C29 | Documento de outro projeto ou conhecimento obrigatório ausente | Negar contexto inadequado ou impedir despacho | Conhecimento |
| C30 | Contexto completo acima do limite | Não truncar requisitos obrigatórios silenciosamente | Contexto |
| C31 | Resposta chega depois de run terminal | Registrar evidência/custo sem reabrir execução | Recuperação |
| C32 | Modificar plano aceito | Nova versão e novo run no MVP | Versionamento |

## Cenário completo de referência

Usar T1 a T5 do [workflow](workflow-contract.md). Todos os valores são fictícios e servem para verificar a especificação.

1. O run inicia com projeto e conhecimento identificados. Planejamento produz a versão 1, validada antes de T1; o manifesto fecha suas referências.
2. A política resolve concorrência um, duas tentativas por tarefa, duas avaliações por tentativa, execução técnica de até 60 segundos, avaliação de 20 segundos, reconciliação de 15 segundos e duração global de 600 segundos. Capacidades e permissões são compatíveis com os destinos simulados.
3. T1 e T2 produzem artefatos aceitos. O snapshot e o desenho de T2 estão disponíveis para T3.
4. Destinos A e B são elegíveis para T3. Uma regra versionada prefere A e registra os candidatos e o motivo, sem alterar TaskSpec.
5. O primeiro contexto é materializado e identificado. T3/1 entrega patch P1 e relatório, ficando completed.
6. Avaliação reprova requisito obrigatório, referenciando P1. T4 e T5 permanecem pending.
7. Com orçamento e uma tentativa restante, T3 passa por retry_wait. Novo workspace parte da base original, sem aplicar P1; o contexto recebe o relatório anterior com procedência.
8. Uma política explícita de fallback escolhe B e gera outra RoutingDecision. T3/2 entrega P2, aprovado. T3 fica succeeded, preservando as duas tentativas.
9. T4 usa P2 e produz test_patch e test_report sobre a base declarada. T5 revisa as entregas aceitas e produz review_report.
10. A avaliação global aplica os patches em ordem sobre base limpa e aprova os critérios globais. O run termina succeeded. P2 é o patch aceito; P1 permanece apenas no histórico.

O cenário demonstra rastreabilidade, não superioridade de B sobre A. Duas observações com contextos diferentes não sustentam essa conclusão.

## Exercício de contabilização

Exemplo separado, em USD, com todos os custos conhecidos. Orçamento fictício: 1,00. Planejamento: 0,05; execuções de T1/T2: 0,10; T3/1: 0,20; T3/2: 0,25; execuções de T4/T5: 0,10; avaliações locais e global: 0,10. Total esperado: 0,80. Roteamento por regras não faz chamadas cobradas neste exemplo.

A tentativa reprovada entra no custo. Reservas não são despesas adicionais. Com parcela desconhecida, apresentar subtotal conhecido e lacuna, sem afirmar custo total exato.

## Critério de revisão

Os casos devem corresponder às regras de [tarefa](task-contract.md), [workflow](workflow-contract.md), [execução](execution-lifecycle.md) ou a um contrato pendente identificado. Catálogo, contexto, artefatos e métricas ainda exigem estruturas próprias; seus casos orientam as próximas entregas.

Os oito casos específicos de [perfis](agent-profiles.md) complementam esta matriz. Antes de encerrar a Fase 1, revisar esses perfis e especificar ExecutionPolicy, RoutingDecision, ContextManifest, RunManifest, AttemptRecord, ArtifactRef, EvaluationReport e metadata de conhecimento. Depois converter esta matriz em fixtures e testes na etapa de implementação autorizada.
