# ROADMAP — ORQ

**Status:** EM ANDAMENTO · Prioridade 2 no portfólio

O estado técnico detalhado e o próximo passo ficam em `docs/HANDOFF.md`. Este arquivo acompanha o checklist de portfólio.

## Checklist de "pronto"
- [ ] CI no GitHub Actions (pytest + validação dos schemas) verde
- [x] Testes automatizados (77 testes)
- [x] Licença (MIT)
- [ ] README com diagrama da arquitetura (planner → router → contexto → tentativa → avaliação) e exemplo ponta a ponta
- [ ] Fase 6: runner sequencial do Orchestrator com executor simulado
- [ ] Primeiro adaptador real de executor/provedor
- [ ] Dependabot ativo (pip + GitHub Actions)
- [ ] Descrição e tópicos no repositório (depende do `gh`)

## Backlog
1. Workflow de CI: instalar com `requirements.lock`, rodar `pytest` em Python 3.12 e 3.13
2. Especificação da Fase 6 (ver o "Próximo passo concreto" no HANDOFF)
3. Executor simulado provando o fluxo completo
4. Adaptadores de executor para CLIs de agentes (Claude Code, Codex, Gemini, Kiro), alinhados à separação papel/modelo/provedor/runtime/executor
5. Exemplo reproduzível no README (`orq plan` → `orq route` → execução simulada)

## Log
- 2026-11-10 — Fase 1: licença MIT, CLAUDE.md, regras de autonomia, roadmap.
