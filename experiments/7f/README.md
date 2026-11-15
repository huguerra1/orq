# Pacote do benchmark 7F

Dados auditáveis do experimento 7F (ver [docs/phase-7/experiment.md](../../docs/phase-7/experiment.md#benchmark-7f)): 10 tarefas × 3 repetições × 2 condições (`shared_global` e `family_partitioned_rls`) = 60 runs reais, em 6 lotes diários de 10 runs. Este pacote **não executa nada**: não chama CLIs de assinatura nem acessa a rede. Os scripts de execução virão depois e só leem estes arquivos.

## Conteúdo

| Caminho | O que é |
| --- | --- |
| `vault/content/` | 50 unidades Markdown no formato do Vault (uma fonte, um título H1, uma unidade). Grupos: `common` (12, regras e contratos do ORQ), `role` (4, guias por papel), `family` (12, operação real das CLIs), `family_role` (6, família + papel) e `synthetic` (16, distratores fictícios) |
| `vault/catalog-definition.json` | Definição autorizada do catálogo (audiência por fonte, status `active`) |
| `vault/catalog-snapshot.json` | Snapshot gerado por `build_catalog_snapshot`; seu `catalog_hash` está fixado no manifesto |
| `tasks/*.json` | Uma tarefa por arquivo: `task` (objeto `Task` do schema do projeto), `output_contract` (JSON Schema entregue ao agente), `knowledge` (`gold` fechado, `acceptable_alternatives`, `distractors`, `irrelevant` e `answer_sources`), `acceptance.deterministic_checks` e `reference_answers` |
| `manifest.json` | Tarefas, condições, sementes, `catalog_hash`, digests de cada arquivo (dados e scripts de `tools/`), plano de lotes e `package_digest` |
| `tools/` | Gerador (`f7_build.py`), definições-fonte (`f7_corpus.py`, `f7_tasks.py`) e o avaliador das checagens (`f7_checks.py`), só com a biblioteca padrão |

## Regras do corpus

- Fatos sobre as CLIs vêm só de `docs/phase-7/subscription-adapters.md`, `docs/phase-7/quota-aware-routing.md` e `docs/phase-6/executor-choice.md`; um teste confere que toda flag citada em unidade real existe nessas docs.
- Unidades `synthetic` (`syn-*`, pasta `content/synthetic/`, tópico `sintetico`, nota final no texto e lista em `catalog.synthetic_source_ids` do manifesto) são **fictícias**: contradizem regras reais de outra família e só servem para medir contaminação. Nunca são gabarito.
- A audiência (`common` ou `family`, famílias e papéis) é atribuída pelo catálogo, nunca pelo frontmatter.

## Como os scripts de execução usam o pacote

1. Verificar o pacote: recalcular `raw_sha256`/`jcs_digest` dos arquivos listados em `manifest.files` (inclui os scripts de `tools/`) e o `package_digest`, que é o digest JCS do manifesto inteiro (arquivos, sementes e plano de lotes) sem o próprio campo `package_digest`; sincronizar o snapshot com o Postgres (`vault sync-db`) e conferir o `catalog_hash`.
2. Percorrer `manifest.batch_plan.batches[*].runs` na ordem dada (um lote por dia; as duas condições de um par rodam em sequência na mesma janela; a condição inicial é contrabalanceada por tarefa e por família-alvo, com diferença máxima de 1 par). `run_seed` é a semente do run.
3. Por run: escopo = `(family_id, role_id)` de `tasks/<task_id>.json#target`; na condição `family_partitioned_rls` vale o RLS estrito, e em `shared_global` a política é relaxada. A busca usa os `knowledge_requirements` do `task`.
4. Dar ao agente `task.objective` e `output_contract`; **nunca** `knowledge`, `acceptance` nem `reference_answers`.
5. Avaliar com `DeterministicEvaluator` (`output_binding_present`, `digest_matches`, `content_contract_valid`, registrando o contrato com `expected_output[0].content_contract_ref`) e depois com `f7_checks.evaluate_checks(resposta, acceptance.deterministic_checks)`. Pass do run (`pass@1`): avaliador `pass` **e** todas as checagens verdadeiras. A checagem `CHK-no-contamination` sinaliza texto vindo de distratores.
6. Métricas de seleção (definições em `manifest.metric_definitions`): toda unidade elegível a uma tarefa no regime global está em exatamente uma classe (`gold`, `acceptable_alternatives`, `distractors` ou `irrelevant`). Alternativas são neutras na precisão, e só `distractors` contam como contaminação; a exposição cruzada por família é medida à parte pela audiência. `answer_sources` declara, por checagem, as fontes que sustentam cada resposta.

## Regenerar e verificar

```bash
PYTHONPATH=src python experiments/7f/tools/f7_build.py          # reescreve o pacote
PYTHONPATH=src python experiments/7f/tools/f7_build.py --check  # falha se algo divergir
```

Qualquer alteração de corpus, tarefa ou semente muda digests e `catalog_hash`: refazer o pacote antes de iniciar a coleta e não editar nada depois do primeiro run real. `tests/test_experiment_7f_benchmark.py` valida corpus, tarefas, gabaritos, distratores, digests e o plano de lotes.
