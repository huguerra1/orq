---
title: "Kiro CLI: invocação não interativa"
language: pt-BR
topics: [invocacao, kiro, wsl, diretorio-de-trabalho, modelo, permissoes]
---
# Kiro CLI: invocação não interativa

```text
wsl.exe --cd <workspace_dir> -- kiro-cli chat --no-interactive --trust-workspace --trust-tools=<lista_fechada> --model <native_model_id> "<prompt_envelope>"
```

- No Linux nativo, executa `kiro-cli` direto; no Windows, usa `wsl.exe` sem shell interativo.
- Os workspaces ficam em `C:\dev\...`, vistos no WSL como `/mnt/c/...`.
- O login observado usa o IAM Identity Center, sem API key.
