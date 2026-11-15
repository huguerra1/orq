---
title: "Codex CLI: testes no sandbox do Windows"
language: pt-BR
topics: [testes, execucao-de-testes, sandbox, windows]
---
# Codex CLI: testes no sandbox do Windows

Observado no Codex CLI 0.167.0 em Windows 10: no sandbox `workspace-write`, runners de teste com vários processos (vitest, jest) falharam ao criar processos filhos com `spawn EPERM`.

- Funcionaram com `--pool threads` ou `--runInBand`.
- Instrução operacional da família: execute os testes em thread única; no Python, `pytest` sequencial.
