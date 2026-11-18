# Protocolo de calibração, viés entre famílias e uso no experimento 7F

Estado: Aceita em 2026-11-18 ([ADR 0009](../adr/0009-phase-8-model-and-rubric-evaluator.md#decisões-do-dono-aceitas-em-2026-11-18)). Complementa a especificação do [avaliador por rubrica](rubric-evaluator.md) e estabelece as regras metodológicas para uso em conjunto com o [experimento científico 7F](../phase-7/experiment.md).

## Motivação e problema científico

Avaliadores baseados em modelos de linguagem (*LLM-as-a-judge*) estão sujeitos a três ameaças clássicas à validade de medição:

1. **Inconsistência inter-família:** diferentes famílias de modelos (`claude`, `codex`, `gemini`, `kiro`) podem interpretar descrições abertas de critérios com réguas de severidade diferentes, reduzindo a reprodutibilidade quando o roteamento ciente de cota troca a família avaliadora.
2. **Descalibração perante defeitos reais:** um modelo pode atribuir notas altas a um artefato bem redigido que, na prática, viola uma regra crítica do contrato (falso positivo), ou reprovar um artefato correto por preferências superficiais de estilo (falso negativo).
3. **Viés de autoavaliação (*self-preference / intra-family bias*):** modelos tendem a atribuir pontuações mais altas a textos e códigos gerados pela própria família ou a ignorar alucinações características do seu próprio runtime.

Para que o `CompositeEvaluator` da Fase 8 produza evidências defensáveis em publicações científicas, toda `RubricSpec` precisa ser calibrada contra um **gabarito de referência curado (padrão-ouro revisado por humano + defeitos semânticos injetados por construção)**, ter sua **concordância entre famílias** medida formalmente (com limiares iniciais tratados como proposta a calibrar no piloto do `8E`) e operar de modo que **jamais contamine a métrica primária determinística** do experimento `7F`.

## 1. Calibração contra o gabarito de referência curado (`CalibrationCorpus`)

### Estrutura, tamanho mínimo e custo em cota do `CalibrationCorpus`

Cada rubrica candidata é validada sobre um corpus versionado e imutável (`CalibrationCorpus`, fixado por `corpus_hash` JCS SHA-256). Como qualquer falha sintática já é barrada no Estágio 1, todos os artefatos do corpus **passam nas checagens determinísticas do Estágio 1** (`output_binding_present`, `digest_matches` e `content_contract_valid`); o rótulo semântico esperado (`expected_status`) não vem de uma regra sintática automática, mas sim **por construção (mutação controlada) e revisão humana do dono**:

| Estrato | Qtd. mínima proposta | Gabarito esperado (`expected_status`) | Como é construído (por curadoria e construção) | Objetivo da medição (ótica de aprovação = `pass`) |
| --- | --- | --- | --- | --- |
| **Positivos padrão-ouro (`gold_pass`)** | 10 itens | `pass` | Artefatos de referência revisados manualmente pelo dono e aprovados por checklists e testes funcionais completos | Medir **sensibilidade de aprovação ($\text{Recall}_{\text{pass}} = \text{TP}/(\text{TP}+\text{FN})$)** e taxa de falsos negativos de aprovação (reprovação indevida de artefato correto por rigor excessivo ou ruído do juiz) |
| **Negativos com mutação semântica (`seeded_semantic_fail`)** | 10 itens | `fail` | Clones de itens `gold_pass` nos quais foi injetado manualmente exatamente **um defeito semântico ou arquitetural conhecido** que não quebra o JSON Schema (ex.: leitura de token OAuth em `~/.codex`, conversão de custo `unavailable` em `"0"`, omissão de reconciliação após `unknown`, ou vazamento de escopo entre famílias) | Medir **especificidade de aprovação (sensibilidade de detecção de defeito)** e **taxa de falsos positivos críticos** ($\text{CFPR}$: aprovar como `pass` um artefato com defeito bloqueante injetado) |
| **Fronteira de incerteza (`borderline_cases`)** | 4 itens | `inconclusive` ou fronteira documentada | Artefatos curados com cobertura parcial controlada (notas de referência na vizinhança de `pass_threshold`) | Calibrar a largura de `inconclusive_band` e a clareza dos `anchors` da rubrica |

#### Tamanho amostral, poder estatístico e orçamento de cota para calibração

- **Tamanho mínimo proposto (a confirmar pelo dono):** **24 artefatos por rubrica** ($10\text{ `gold\_pass`} + 10\text{ `seeded\_semantic\_fail`} + 4\text{ `borderline\_cases`}$), balanceados à razão de 6 artefatos originalmente redigidos por cada uma das 4 famílias (`claude`, `codex`, `gemini`, `kiro`) e revisados/ajustados manualmente para garantir o gabarito.
- **Justificativa estatística:** com $N = 20$ itens dicotômicos (`gold_pass` vs. `seeded_semantic_fail`) e $N = 24$ itens ordinais (multiplicados pelas dimensões da rubrica no cálculo de $\alpha_{\text{ord}}$), um teste unilateral para $\kappa$ de Cohen contra uma concordância nula fraca ($\kappa_0 = 0,20$) detecta concordância substancial ($\kappa \ge 0,70$) com nível de significância $\alpha = 0,05$ e poder estatístico $\approx 80\%$, enquanto os 10 itens `seeded_semantic_fail` oferecem resolução de $0,10$ por item na detecção de falsos positivos bloqueantes.
- **Estimativa de consumo de cota nas assinaturas pessoais:**
  1. **Calibração operacional padrão (2 famílias revisoras autorizadas na ADR 0008, `gemini` e `kiro`):** $24 \text{ itens} \times 2 \text{ famílias} = \mathbf{48\text{ chamadas de CLI}}$ ($24$ mensagens na assinatura Gemini e $24$ mensagens no Kiro Power), executáveis em uma única janela sem comprometer o uso diário.
  2. **Experimento completo $4 \times 4$ de viés intrafamília (opcional, executado uma vez no `8E` para o artigo):** $24 \text{ itens} \times 4 \text{ famílias} = \mathbf{96\text{ chamadas de CLI}}$ ($24$ mensagens por assinatura, distribuídas ao longo de 1 a 2 dias). Cada chamada registra seu `UsageRecord` (`status: "unavailable"`, `amount: null`) e `QuotaConsumptionRecord`.

### Métricas contra o gabarito de referência curado

Para cada família avaliadora $F_j$ executada sobre os itens `gold_pass` ($P$, $|P| = 10$) e `seeded_semantic_fail` ($N$, $|N| = 10$):

1. **Taxa de falsos positivos em defeitos bloqueantes (`critical_false_positive_rate`):**
   $$\text{CFPR}(F_j) = \frac{|\{x \in N_{\text{blocking}} : \text{verdict}(F_j, x) = \text{pass}\}|}{|N_{\text{blocking}}|}$$
   Representa a fração de artefatos com violação grave injetada que o avaliador aprovou indevidamente. **Proposta inicial de gate (a calibrar no 8E):** $\text{CFPR}(F_j) = 0,0000$.
2. **Precisão, Recall (sensibilidade de aprovação) e $F_1$ de aprovação legítima:**
   $$\text{Precision}_{\text{pass}} = \frac{\text{TP}}{\text{TP} + \text{FP}}, \quad \text{Recall}_{\text{pass}} = \frac{\text{TP}}{\text{TP} + \text{FN}}, \quad F_1 = \frac{2 \cdot \text{Precision}_{\text{pass}} \cdot \text{Recall}_{\text{pass}}}{\text{Precision}_{\text{pass}} + \text{Recall}_{\text{pass}}}$$
   onde $\text{TP}$ são itens `gold_pass` avaliados como `pass`, $\text{FP}$ são itens `seeded_semantic_fail` avaliados como `pass` e $\text{FN}$ são itens `gold_pass` avaliados como `fail` ou `inconclusive`. **Proposta inicial de referência (a calibrar no 8E):** $\text{Recall}_{\text{pass}} \ge 0,80$ e $F_1 \ge 0,85$.
3. **Taxa de citação válida (`valid_citation_rate`):**
   proporção de avaliações em que 100% dos `verbatim_excerpt` citados pelo modelo passaram na verificação byte a byte contra `subject_bindings`. **Proposta inicial de gate (a calibrar no 8E):** $\text{valid\_citation\_rate} \ge 0,95$.

## 2. Concordância entre avaliadores de famílias diferentes (*Inter-Family Agreement*)

Como o roteamento ciente de cota pode alternar a avaliação entre diferentes famílias elegíveis (por exemplo, entre `family_gemini` e `family_kiro`), é essencial demonstrar que famílias distintas chegam a veredictos consistentes quando aplicam a mesma `RubricSpec`.

Sobre os itens do corpus avaliados pelas famílias habilitadas, o comando `orq calibrate evaluator` calcula quatro indicadores determinísticos. Os limiares abaixo são **valores iniciais propostos (baseados nas escalas de referência de Landis & Koch para $\kappa$ e de Krippendorff para $\alpha_{\text{ord}}$), a calibrar empiricamente na coleta piloto do incremento `8E`**:

| Métrica | Papel no `CalibrationReport` | Escopo | Definição e interpretação | Valor inicial proposto *(a calibrar no 8E)* |
| --- | --- | --- | --- | --- |
| **Kappa de Cohen ($\kappa(F_a, F_b)$)** | Gate de promoção (`rubric_promotion_gates`) | Par de famílias sobre o veredicto discreto (`pass`, `fail`, `inconclusive`) | $\kappa = \frac{p_o - p_e}{1 - p_e}$, onde $p_o$ é a proporção de concordância exata observada entre $F_a$ e $F_b$ e $p_e$ é a concordância esperada ao acaso pelas frequências marginais | $\kappa \ge 0,70$ para todo par de famílias habilitadas como avaliadoras |
| **Alfa de Krippendorff ordinal ($\alpha_{\text{ord}}$)** | Gate de promoção (`rubric_promotion_gates`) | Múltiplas famílias sobre as notas inteiras $s_i \in [s_{\min}, s_{\max}]$ de cada dimensão | Mede a confiabilidade inter-avaliador tratando a escala da rubrica como ordinal e suportando matrizes incompletas | $\alpha_{\text{ord}} \ge 0,75$ global e $\ge 0,67$ por dimensão |
| **Tau-b de Kendall ($\tau_b$)** | Métrica diagnóstica reportada | Par de famílias sobre o `normalized_score` $S \in [0, 1]$ | Mede a concordância de ordenamento (ranking relativo dos artefatos) com ajuste para empates | $\tau_b \ge 0,70$ (diagnóstico de ranking) |
| **Desvio absoluto médio inter-família ($\text{MAD}_{\text{inter}}$)** | Métrica diagnóstica e filtro de severidade por família | Par de famílias sobre $S \in [0, 1]$ | $\frac{1}{M} \sum_{x=1}^M |S(F_a, x) - S(F_b, x)|$; identifica se uma família é sistematicamente mais leniente ou mais severa que outra | $\text{MAD}_{\text{inter}} \le 0,10$ na escala normalizada $[0, 1]$ |

Se uma dimensão da rubrica apresentar $\alpha_{\text{ord}}$ abaixo do limiar calibrado (proposto em $0,67$), isso indica que suas âncoras (`anchors`) estão subjetivas demais; a rubrica deve ter suas âncoras refinadas em nova versão antes de ser promovida (`calibration_status: "rejected"`).

## 3. Medição do viés de autoavaliação (*Self-Preference Bias*) e da colisão `kiro` × `claude`

### Desenho experimental para medir o viés intrafamília e por fornecedor de modelo

Embora a política de produção (`cross_family_policy: "require_different"`, `enforce_distinct_model_vendor: true`) proíba uma família ou mesmo `model_vendor` de avaliar artefatos que ela mesma produziu, medir empiricamente o tamanho desse viés na fase de calibração (`calibration_mode: true`) fornece evidência científica direta para justificar a regra de revisão cruzada da [ADR 0008](../adr/0008-phase-7-subscription-hub-and-family-memory.md) e da [ADR 0009](../adr/0009-phase-8-model-and-rubric-evaluator.md).

Quando o modo experimental completo $4 \times 4$ é executado sobre os 24 artefatos balanceados ($6$ por família autora $F_{\text{author}} \in \{\text{claude}, \text{codex}, \text{gemini}, \text{kiro}\}$, todos anonimizados):

1. **Condição intrafamília (*self-evaluation*):** o artefato produzido por $F_i$ é avaliado pela própria família $F_i$.
2. **Condição mesmo fornecedor de modelo sob outra CLI (*cross-CLI same-vendor*):** artefatos de `family_claude` avaliados por `family_kiro` rodando modelo Claude (`anthropic`) e vice-versa, testando empiricamente se o viés acompanha o modelo subjacente além da CLI.
3. **Condição inter-família e inter-fornecedor (*true cross-family evaluation*):** o artefato produzido por $F_i$ é avaliado por famílias $F_j \neq F_i$ com `model_vendor` distinto.

### Índice de viés de autoavaliação ($\Delta_{\text{self}}$) e tratamento de $\Delta_{\text{self}}$ alto

Como uma família $F_i$ pode ser globalmente mais generosa ou mais rigorosa com todos os artefatos, o cálculo separa o efeito principal de severidade do juiz do efeito de favoritismo pela própria família.

Seja $S(F_{\text{eval}}, x)$ o `normalized_score` atribuído pela família avaliadora $F_{\text{eval}}$ ao artefato $x$, e $A_i$ o conjunto de artefatos cuja família autora é $F_i$:

1. **Diferença bruta intrafamília vs. inter-família no mesmo artefato $x \in A_i$:**
   $$d_{\text{raw}}(x) = S(F_i, x) - \frac{1}{|\mathcal{F}_{\neq i}|} \sum_{F_j \in \mathcal{F}_{\neq i}} S(F_j, x)$$
2. **Correção pelo offset de severidade geral da família $F_i$ (medido apenas nos artefatos em que $F_i$ não é autora, $x \notin A_i$):**
   $$\text{offset}(F_i) = \frac{1}{|A_{\neq i}|} \sum_{x \notin A_i} \left( S(F_i, x) - \frac{1}{|\mathcal{F}_{\notin \{i, \text{author}(x)\}}|} \sum_{F_k \notin \{F_i, \text{author}(x)\}} S(F_k, x) \right)$$
3. **Índice líquido de autoavaliação ($\Delta_{\text{self}}(F_i)$):**
   $$\Delta_{\text{self}}(F_i) = \left( \frac{1}{|A_i|} \sum_{x \in A_i} d_{\text{raw}}(x) \right) - \text{offset}(F_i)$$

**O que acontece quando $|\Delta_{\text{self}}(F_i)|$ é alto (distinção entre propriedade da família e qualidade da rubrica):**

- $\Delta_{\text{self}}(F_i)$ é uma **propriedade comportamental da família/modelo $F_i$**, e não um defeito da `RubricSpec`. Um valor $|\Delta_{\text{self}}(F_i)| > 0,10$ (limiar diagnóstico inicial proposto a calibrar) **não reprova a rubrica** para uso cross-family; pelo contrário, ele registra `self_bias_detected: true` em `self_preference_metrics[F_i]`, comprovando empiricamente por que $F_i$ jamais deve avaliar suas próprias saídas (e, se o viés persistir no par `family_kiro` × `family_claude`, confirma a obrigatoriedade de `enforce_distinct_model_vendor: true`).
- Em contrapartida, se uma família $F_i$ apresentar **erro em defeito bloqueante ($\text{CFPR}(F_i) > 0$)** ou um **offset de severidade/leniência geral extremo em avaliações cross-family** ($|\text{offset}(F_i)| > 0,15$ ou $\text{MAD}_{\text{inter}} > 0,10$, valores propostos a calibrar):
  - se a divergência ocorrer em múltiplas famílias ($\kappa < 0,70$ ou $\alpha_{\text{ord}} < 0,67$), a rubrica inteira recebe `calibration_status: "rejected"` para revisão de seus `anchors`;
  - se as demais famílias concordarem entre si ($\kappa \ge 0,70$) e apenas uma família destoar, essa família é listada em `disqualified_evaluator_families` daquele `CalibrationReport`, ficando impedida de atuar como juiz daquela rubrica.

## 4. Contrato `CalibrationReport` e gates unificados de promoção da rubrica

O incremento `8E` introduz o registro imutável `CalibrationReport` (com `record_hash` JCS SHA-256), gerado pelo subcomando `orq calibrate evaluator`, contendo todas as métricas de gabarito, concordância e viés descritas nas seções 1 a 3:

| Campo | Tipo | Obrigatório | Semântica |
| --- | --- | --- | --- |
| `schema_version` | `"0.1"` | Sim | |
| `calibration_id` | identifier | Sim | Único para a sessão de calibração |
| `rubric_ref` | versionedRef | Sim | Rubrica avaliada, com `content_digest` |
| `prompt_template_ref` | versionedRef | Sim | Template de prompt utilizado |
| `corpus_ref` | versionedRef | Sim | Referência e digest do `CalibrationCorpus` (com contagem de itens por estrato) |
| `evaluated_families` | lista de identifiers | Sim | Famílias participantes da calibração |
| `thresholds_snapshot` | objeto | Sim | Limiares aplicados na sessão (`min_kappa`, `min_alpha_global`, `min_alpha_dimension`, `max_cfpr`, `min_valid_citation_rate`, `max_mad_inter`, `self_bias_diagnostic_threshold`) e flag `calibrated_from_pilot: boolean` |
| `gold_standard_metrics` | mapa por `family_id` | Sim | `critical_false_positive_rate`, `precision_pass`, `recall_pass`, `f1_pass`, `valid_citation_rate` |
| `pairwise_cohen_kappa` | lista `{family_a, family_b, kappa, n_items}` | Sim | $\kappa$ de Cohen para cada par de famílias sobre veredictos discretos |
| `krippendorff_alpha_ordinal` | decimal textual | Sim | $\alpha_{\text{ord}}$ global sobre as notas das dimensões |
| `dimension_krippendorff_alpha` | mapa por `dimension_id` | Sim | $\alpha_{\text{ord}}$ individual de cada dimensão da `RubricSpec` |
| `pairwise_kendall_tau_b` | lista `{family_a, family_b, tau_b, n_items}` | Sim | $\tau_b$ de Kendall par a par sobre `normalized_score` |
| `pairwise_mad_inter` | lista `{family_a, family_b, mad_inter, n_items}` | Sim | $\text{MAD}_{\text{inter}}$ par a par sobre `normalized_score` |
| `self_preference_metrics` | mapa por `family_id` ou `{}` | Sim | Quando a matriz $4 \times 4$ é executada: `raw_diff`, `severity_offset`, `net_delta_self` ($\Delta_{\text{self}}$), `cross_cli_same_vendor_diff`, `wilcoxon_p_value` e `self_bias_detected` |
| `disqualified_evaluator_families` | lista de identifiers | Sim | Famílias excluídas do pool de juízes desta rubrica por $\text{CFPR} > 0$ ou severidade fora de faixa |
| `quota_consumption_summary` | objeto | Sim | Mensagens e tokens consumidos por assinatura durante a calibração (`UsageRecord`s e `QuotaConsumptionRecord`s com custo monetário `unavailable`) |
| `calibration_status` | enum | Sim | `accepted` (se todos os `rubric_promotion_gates` passarem para ao menos o par revisor ativo) ou `rejected` |
| `rejection_reasons` | lista de strings | Sim | Vazia se `accepted`; lista os gates violados caso `rejected` (`cfpr_exceeded`, `kappa_below_threshold`, `alpha_below_threshold`, `valid_citation_rate_below_threshold`) |
| `recorded_at` | utcTimestamp | Sim | |
| `record_hash` | digest | Sim | SHA-256 JCS |

**Gates unificados de promoção (`rubric_promotion_gates`, valores iniciais propostos a calibrar no piloto do 8E):**
Uma `RubricSpec` só recebe `calibration_status: "accepted"` quando, para as famílias avaliadoras habilitadas (não listadas em `disqualified_evaluator_families`), cumpre simultaneamente:
1. $\text{CFPR}(F_j) = 0,0000$ (`max_cfpr: "0.0000"`);
2. $\kappa(F_a, F_b) \ge 0,70$ (`min_kappa: "0.7000"`);
3. $\alpha_{\text{ord}} \ge 0,75$ global e $\ge 0,67$ em cada dimensão (`min_alpha_global: "0.7500"`, `min_alpha_dimension: "0.6700"`);
4. $\text{valid\_citation\_rate}(F_j) \ge 0,95$ (`min_valid_citation_rate: "0.9500"`).

## 5. Isolamento por validador do experimento 7F e contrato de auditoria pós-hoc opcional

O experimento científico `7F` ([experiment.md](../phase-7/experiment.md)) investiga o impacto de **memória compartilhada vs. particionada por família com RLS** (RQ1), **seleção estática vs. escolha pelo agente via MCP** (RQ2) e **roteamento ciente de cota** (RQ3). Na seção de benchmark de `experiment.md`, ficou estabelecido que:

> *"Nenhum juiz baseado em LLM ('LLM-as-a-judge') será usado na métrica primária para não introduzir viés de família nem consumir cota extra."*

Para que essa proteção **não dependa apenas de texto documental**, a Fase 8 impõe isolamento técnico por validador e define um contrato separado para eventual auditoria pós-hoc:

### 1. Proibição por validador de `model_rubric` nos runs do 7F (`experiment_7f_forbids_model_rubric`)

- Qualquer `WorkflowSpec` ou `RunIntent` identificado como parte do benchmark confirmatório `7F` (`experiment_metadata.experiment_id == "7F"`) é verificado por `validate_instance` antes da admissão: se qualquer tarefa ou workflow contiver um critério com `method: "model_rubric"`, a validação falha imediatamente com erro `experiment_7f_forbids_model_rubric`.
- Dessa forma, 100% dos runs do `7F` executam exclusivamente o `DeterministicEvaluator`, e `EvaluationReport.verdict` nos runs do `7F` é determinístico por construção.

### 2. Contrato de auditoria pós-hoc opcional sobre runs encerrados (`PostHocRubricAuditRecord` / `orq audit rubric`)

- Os runs do `7F` já estão encerrados (`run_closed`) quando a Fase 8 entra em operação, e seus diretórios `runs/<run_id>/` são imutáveis (não admitem novos `EvaluationReport`s internos).
- Caso o dono decida realizar a auditoria qualitativa complementar após a conclusão do `7F` (ver Decisão 5 da ADR 0009), ela é executada pelo comando separado **`orq audit rubric --source-run <run_dir> --rubric <rubric.json> --output-dir <audit_dir>`**, que:
  1. abre o `runs/<run_id>/journal.jsonl` original em **modo estritamente somente leitura**, verifica sua integridade (`event_hash` final e `run_closed`) e localiza os artefatos finais aprovados (`outputs_accepted`);
  2. executa o `RubricModelEvaluator` cross-family (`evaluator_family_id != author_family_id` e `evaluator_model_vendor != author_model_vendor`) em workspace temporário isolado fora de `<store_root>`;
  3. grava os artefatos de evidência, `UsageRecord` (`status: "unavailable"`, `amount: null`), `QuotaConsumptionRecord` e o registro **`PostHocRubricAuditRecord`** exclusivamente em `<audit_dir>/` (fora de `runs/<run_id>/`):

| Campo de `PostHocRubricAuditRecord` | Tipo | Obrigatório | Semântica |
| --- | --- | --- | --- |
| `schema_version` | `"0.1"` | Sim | |
| `audit_id` | identifier | Sim | Identificador único da auditoria pós-hoc |
| `source_run_id`, `source_task_id`, `source_attempt_id`, `source_evaluation_id` | identifier | Sim | Vínculo somente leitura com a tentativa e avaliação determinística original do run encerrado |
| `source_run_cutoff_event_hash` | digest | Sim | `event_hash` do `run_closed` do run original, provando que o run auditado estava fechado e íntegro |
| `source_deterministic_verdict` | `"pass"` | Sim | Veredicto determinístico original (inalterável pela auditoria) |
| `subject_bindings` | lista não vazia | Sim | Mesmos bindings e digests SHA-256 aprovados no run original |
| `author_family_id`, `author_model_vendor` | identifier, nonEmptyText | Sim | Família e fornecedor de modelo que produziram a tentativa original |
| `rubric_ref`, `calibration_report_ref` | versionedRef | Sim | Rubrica aplicada e referência ao `CalibrationReport` com `calibration_status: "accepted"` |
| `rubric_evidence_refs` | lista não vazia de `artifactRef` | Sim | Referência(s) ao(s) `RubricEvaluationEvidence` gerados na auditoria |
| `audit_derived_status` | enum | Sim | `pass`, `fail` ou `inconclusive` segundo a rubrica (apenas para análise secundária) |
| `audit_normalized_score` | decimal textual `[0, 1]` ou `null` | Sim | Escore normalizado apurado na auditoria |
| `usage_record_refs`, `quota_consumption_refs` | listas não vazias de `versionedRef` | Sim | Uso (`unavailable`/`null`) e consumo de cota próprios da auditoria |
| `recorded_at` | utcTimestamp | Sim | |
| `record_hash` | digest | Sim | SHA-256 JCS |

### 3. Apresentação segregada nas tabelas do artigo

Toda tabela ou figura gerada pelos scripts de agregação separa explicitamente a camada confirmatória obrigatória da camada exploratória/secundária opcional:

| Camada no artigo | Fonte do veredicto / escore | Papel no teste de hipóteses | Consumo de cota |
| --- | --- | --- | --- |
| **Camada Primária (Confirmatória — Fase 7)** | `DeterministicEvaluator` nos runs originais do `7F` (`model_rubric` proibido por `experiment_7f_forbids_model_rubric`) | Decide $H_{1,0}/H_{1,1}$, $H_{2,0}/H_{2,1}$ e $H_{3,0}/H_{3,1}$ (`pass_at_1`, `run_success_rate`) | Zero cota de avaliador |
| **Camada Secundária Opcional (Pós-hoc e Calibração — Fase 8)** | `PostHocRubricAuditRecord` (`orq audit rubric`) e `CalibrationReport` (`orq calibrate evaluator`) gravados em diretório próprio | Análise qualitativa complementar (se autorizada pelo dono) sobre artefatos já aprovados e medição empírica do viés $\Delta_{\text{self}}$ | Contabilizado em `QuotaConsumptionRecord` próprio da auditoria, após o término do `7F`, com custo monetário `unavailable` (`amount: null`) |
