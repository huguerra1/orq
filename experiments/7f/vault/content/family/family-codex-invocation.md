---
title: "Codex CLI: invocação não interativa"
language: pt-BR
topics: [invocacao, codex, saida-estruturada, diretorio-de-trabalho, modelo, sandbox, permissoes]
---
# Codex CLI: invocação não interativa

```text
codex exec -C <workspace_dir> -s <workspace-write|read-only> -m <native_model_id> -c model_reasoning_effort="<effort>" --json -o <last_message_file> "<prompt_envelope>"
```

- `--json` emite eventos JSONL no stdout; `-o` grava a mensagem final em arquivo.
- Quando o workspace da tentativa não é um repositório Git completo, acrescenta `--skip-git-repo-check`.
- O modelo e o esforço padrão vêm de `~/.codex/config.toml` quando não informados.
