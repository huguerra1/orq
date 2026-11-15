---
title: "agy (Gemini): invocação não interativa"
language: pt-BR
topics: [invocacao, gemini, diretorio-de-trabalho, modelo, timeout, saida-estruturada]
---
# agy (Gemini): invocação não interativa

```text
agy -p "<prompt_envelope>" --model <native_model_id> --mode <accept-edits|plan> --output-format text --print-timeout <timeout_s>
```

- O `agy` não tem flag de diretório de trabalho (`-C`): o adaptador define `cwd=workspace_dir` no subprocesso.
- `--print-timeout` recebe o timeout em segundos (`<timeout_s>`).
- Os modelos são listados por `agy models`.
