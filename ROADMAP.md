# ROADMAP — ORQ

**Status:** EM ANDAMENTO · Prioridade 2 no portfólio

O estado técnico detalhado e o próximo passo ficam em `docs/HANDOFF.md`. Este arquivo acompanha o checklist de portfólio.

## Checklist de "pronto"
- [x] CI no GitHub Actions (pytest em Python 3.12 e 3.13; os testes incluem a validação dos schemas)
- [x] Testes automatizados (histórico integral verificado: 6A = 366 aprovados e 1 ignorado, CI verde no PR #8; 6B = 485 aprovados e 4 ignorados após a revisão; ver HANDOFF)
- [x] Licença (MIT)
- [x] README com diagrama da arquitetura (Mermaid) e exemplo ponta a ponta da CLI (validação, digest e cadeia do Vault)
- [ ] Fase 6: runner sequencial do Orchestrator com executor simulado (especificação aceita em 2026-11-10; implementação em incrementos 6A–6E)
- [ ] Primeiro adaptador real de executor/provedor
- [x] Dependabot ativo (pip + GitHub Actions; PRs semanais na `dev`)
- [x] Descrição e tópicos no repositório

## Backlog
1. Workflow de CI: instalar com `requirements.lock`, rodar `pytest` em Python 3.12 e 3.13
2. Especificação da Fase 6 — aceita em 2026-11-10 (ADR 0007 Aceita)
3. Executor simulado provando o fluxo completo (incrementos 6A–6E)
4. Adaptadores de executor para CLIs de agentes (Claude Code, Codex, Gemini, Kiro), alinhados à separação papel/modelo/provedor/runtime/executor
5. Exemplo reproduzível no README (`orq plan` → `orq route` → execução simulada)

## Log
- 2026-11-10 — Achados 1–13 da revisão 6C corrigidos: evidências/ordem, prazo fixo de reconciliação, orçamento desconhecido, retomada terminal, causa de RN07 e admissão com erros/reservas liberadas. 217 testes próprios aprovados; suíte integral: 702 aprovados, 4 ignorados. Decisões nas Notas 6C; sem commit/push nesta execução; próximo incremento: 6D.
- 2026-11-10 — 6C implementado: projeções puras, 49 linhas AT/TK/RN, guardas e retomada sem despacho duplicado; 140 testes novos (suíte: 625 aprovados, 4 ignorados), incluindo RunJournal e falha de fsync. Sem dependências novas; próximo: 6D (executor simulado e avaliador determinístico).
- 2026-11-10 — Achados 1–19 do 6B corrigidos: integridade estrita com LF, durabilidade na reabertura/reutilização, publicação exclusiva, lock com token e erros tipados. Suíte integral: 485 aprovados e 4 ignorados (symlinks sem privilégio no Windows). Decisões nas Notas 6B; próximo: 6C, com reconciliação de intenção sem efeito comprovado.
- 2026-11-10 — 6B implementado: journal JCS append-only encadeado por digest e armazenamento de artefatos por SHA-256, sem dependências novas; 66 testes novos (suíte: 432 aprovados, 1 ignorado). Próximo: 6C.
- 2026-11-10 — Fase 1: licença MIT, CLAUDE.md, regras de autonomia, roadmap.
- 2026-11-10 — CI (pytest 3.12/3.13) e Dependabot; descrição e tópicos aplicados.
- 2026-11-10 — README de vitrine: diagrama Mermaid, estado por fase e exemplos reais da CLI (todos conferidos rodando).
- 2026-11-10 — Especificação da Fase 6 em `docs/phase-6/` e ADR 0007 (Proposta): executor simulado primeiro, journal JSONL encadeado, máquina de estados e cenários S01–S51. Sem código; aguardando revisão.
- 2026-11-10 — Decisões da Fase 6 aceitas (ADR 0007 Aceita): `orq run simulate`, limites de 1 MiB por linha do journal e 8 MiB por artefato simulado, lock órfão com remoção manual.
- 2026-11-10 — 6A implementado: oito schemas fechados, 22 fixtures e 144 testes novos; próximo: 6B.
- 2026-11-10 — Revisão cruzada do 6A (Kiro, Claude Opus): achados 1–10 e 12–16 corrigidos; achado 11 reservado ao 6E. Suíte integral verificada pelo orquestrador: 366 aprovados e 1 ignorado; CI verde no PR #8. Comando das verificações de 6A e 6B: `PYTHONPATH=src python -m pytest -p no:cacheprovider --basetemp=<pasta gravável>`.
