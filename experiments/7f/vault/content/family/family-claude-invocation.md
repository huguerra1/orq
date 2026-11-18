---
title: "Claude Code: invocação não interativa"
language: pt-BR
topics: [invocacao, claude, ferramentas, permissoes, saida-estruturada, diretorio-de-trabalho, modelo]
---
# Claude Code: invocação não interativa

```text
claude -p "<prompt_envelope>" --output-format json --model <native_model_id> --permission-mode <mode> --allowedTools <lista_fechada> --disallowedTools <lista_negada>
```

- Roda com `cwd=workspace_dir` e stdin fechado.
- A `ExecutionPolicy` vira `--allowedTools` (por exemplo `Read`, `Edit` e `Write` restritos ao workspace) e `--disallowedTools`.
- Quando a tarefa não os autoriza, `--disallowedTools` nega as ferramentas de rede (`WebFetch`, `WebSearch`) e o shell (`Bash`).
- O envelope JSON traz `result`, `session_id` e `usage`, e às vezes `total_cost_usd`.
