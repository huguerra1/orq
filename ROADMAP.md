# ROADMAP — ORQ

**Status:** EM ANDAMENTO · Prioridade 2 no portfólio

O estado técnico detalhado e o próximo passo ficam em `docs/HANDOFF.md`. Este arquivo acompanha o checklist de portfólio.

## Checklist de "pronto"
- [x] CI no GitHub Actions (pytest em Python 3.12 e 3.13; os testes incluem a validação dos schemas)
- [x] Testes automatizados (91 testes)
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
- 2026-11-10 — Fase 1: licença MIT, CLAUDE.md, regras de autonomia, roadmap.
- 2026-11-10 — CI (pytest 3.12/3.13) e Dependabot; descrição e tópicos aplicados.
- 2026-11-10 — README de vitrine: diagrama Mermaid, estado por fase e exemplos reais da CLI (todos conferidos rodando).
- 2026-11-10 — Especificação da Fase 6 em `docs/phase-6/` e ADR 0007 (Proposta): executor simulado primeiro, journal JSONL encadeado, máquina de estados e cenários S01–S51. Sem código; aguardando revisão.
- 2026-11-10 — Decisões da Fase 6 aceitas (ADR 0007 Aceita): `orq run simulate`, limites de 1 MiB por linha do journal e 8 MiB por artefato simulado, lock órfão com remoção manual.
