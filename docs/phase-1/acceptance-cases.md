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
| C33 | Motor tipado recomenda ID fora dos elegíveis | Resposta inválida; fallback explícito ou nenhum despacho | Roteamento |
| C34 | Confiança de roteamento abaixo do limiar | Preservar recomendação e aplicar ação configurada | Roteamento |
| C35 | Roteamento externo falha antes da admissão | Registrar custo/erro sem consumir tentativa | Roteamento e orçamento |
| C36 | Bundle materializado diverge de seu manifesto | Impedir despacho por falha de integridade | Contexto |
| C37 | Runtime acrescenta contexto não observável | Marcar cobertura parcial/desconhecida, sem alegar reprodução completa | Contexto |
| C38 | Restrição de tarefa amplia teto do workflow | Rejeição de configuração, sem truncamento silencioso | Política |
| C39 | Caminho permitido resolve por symlink fora do workspace | Operação negada antes do efeito | Política e filesystem |
| C40 | Ferramenta permitida recebe argumento de efeito proibido | Operação negada | Política e ferramentas |
| C41 | URL autorizada redireciona para destino não permitido | Redirecionamento negado | Política e rede |
| C42 | Efeito externo exige aprovação ausente ou vencida | Nenhum efeito; aguardar ou falhar conforme prazo | Política e aprovação |
| C43 | Evidência obrigatória pertence a outra versão | Destino inelegível | Política e evidência |
| C44 | Custo desconhecido sob teto financeiro obrigatório | Não admitir sem estimativa conservadora autorizada | Política e orçamento |
| C45 | Executor não consegue aplicar controle obrigatório | Destino inelegível | Política e executor |
| C46 | Mesmo conteúdo vem de tentativas diferentes | Mesmo digest possível; ocorrências e produtores distintos | Artefatos |
| C47 | Bytes do locator divergem do digest | Integridade falha; nenhum consumo | Artefatos |
| C48 | Output binding possui tipo ou contrato incompatível | Binding rejeitado | Artefatos e tarefa |
| C49 | Dependência tenta consumir artefato reprovado | Uso rejeitado; histórico preservado | Artefatos e workflow |
| C50 | Patch referencia base diferente da materializada | Aplicação rejeitada antes do efeito | Patch |
| C51 | Patch altera caminho fora do escopo | Negação pela política | Patch e política |
| C52 | Test patch declara code patch como pré-requisito | Composição ordenada e digest da base verificados | Patch |
| C53 | Aplicação parcial falha | Nenhum snapshot resultante aceito | Patch |
| C54 | Relatório afirma sucesso sem evidência adequada | Alegação não sustenta veredicto pass | Relatório e avaliação |
| C55 | Avaliação referencia digest diferente da saída | Não aceita o output binding | Artefatos e avaliação |
| C56 | Planejamento falha após gerar custo | RunIntent, Usage/Error e manifesto incompleto explícito preservados | Registros |
| C57 | Crash após intenção de despacho | Reconciliar a mesma intenção; nenhum segundo despacho lógico | Persistência |
| C58 | Evento idempotente é repetido | Deduplicar sem nova transição ou contabilização | Persistência |
| C59 | Mesma chave idempotente chega com conteúdo diferente | Rejeitar conflito | Persistência |
| C60 | Transição declara estado anterior divergente | Evento rejeitado | Estado |
| C61 | Dado tardio chega após estado terminal | Registrar fato/custo sem reabrir estado ou cutoff | Registros |
| C62 | Attempt completed ainda não avaliado | Dependências permanecem bloqueadas | Avaliação |
| C63 | Correção de uso substitui estimativa | Histórico preservado; agregação usa a correção uma vez | Métricas |
| C64 | Reserva financeira é liquidada | Reserva e custo não são somados simultaneamente | Orçamento |
| C65 | Aprovação pertence a outra ação/escopo | Uso rejeitado | Aprovação |
| C66 | PatchApplication applied | Base, aplicador e snapshot resultante exatos registrados | Patch |
| C67 | EvaluationReport omite critério obrigatório | Verdict pass inválido | Avaliação |
| C68 | Falha determinística e rubrica textual pass | Verdict final fail | Avaliação |
| C69 | Reavaliação autorizada após inconclusive | Novo evaluation_id; relatórios preservados | Avaliação |
| C70 | Evaluation pass usa digest diferente do binding | outputs_accepted rejeitado | Avaliação e artefatos |
| C71 | Frontmatter se declara autoridade de plataforma | Autoatribuição rejeitada ou ignorada conforme CatalogPolicy | Conhecimento |
| C72 | Fonte de outro projeto tem score lexical maior | Excluída antes do ranking | Conhecimento |
| C73 | Project Knowledge tenta ampliar rede ou escrita | ExecutionPolicy permanece inalterada | Conhecimento e política |
| C74 | Mesma source_id/revision possui digest diferente | Conflito de catálogo; snapshot não é admitido | Conhecimento |
| C75 | Fonte obrigatória está vencida ou não verificada | Cobertura obrigatória falha; nenhum despacho | Conhecimento |
| C76 | Unidade obrigatória excede o orçamento de contexto | Não truncar silenciosamente; seleção não admissível ou transformação explícita | Conhecimento e contexto |
| C77 | Fontes obrigatórias se contradizem sem precedência | SelectionRecord conflict; nenhum despacho | Conhecimento |
| C78 | Conhecimento opcional não é encontrado | partial permitido com lacuna explícita | Conhecimento |
| C79 | Mesmos bytes existem em duas fontes | Bundle pode deduplicar; procedências permanecem registradas | Conhecimento e contexto |
| C80 | Fonte é revogada entre seleção e despacho | Preflight rejeita; nova seleção é exigida | Conhecimento e contexto |
| C81 | Retry usa catálogo revisado | Novos SelectionRecord e ContextManifest; histórico preservado | Conhecimento e recuperação |
| C82 | Fato da Execution Memory é promovido automaticamente | Rejeição sem curadoria e revisão editorial versionada | Conhecimento e registros |

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

Os casos devem corresponder às regras de [tarefa](task-contract.md), [workflow](workflow-contract.md), [execução](execution-lifecycle.md) e aos contratos complementares identificados. Os contratos permanecem sujeitos à revisão cruzada.

Os oito casos específicos de [perfis](agent-profiles.md), dez de [roteamento e contexto](routing-context-contract.md), 12 de [política de execução](execution-policy-contract.md), 14 de [artefatos](artifact-contract.md), 16 de [registros](execution-records-contract.md) e 16 de [conhecimento](knowledge-metadata-contract.md) complementam esta matriz. Antes de encerrar a Fase 1, realizar a revisão cruzada. Depois converter esta matriz em fixtures e testes na etapa de implementação autorizada.
