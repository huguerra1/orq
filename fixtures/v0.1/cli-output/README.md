# Saídas reais das CLIs

Trechos copiados de execuções reais, usados como fixtures dos adaptadores (7C). Não contêm tokens nem credenciais.

| Arquivo | Origem |
| --- | --- |
| `codex/version.txt`, `claude/version.txt`, `agy/version.txt` | `codex --version`, `claude --version` e `agy --version` em 2026-11-14 |
| `codex/usage-limit-2026-11-10.txt` | Final da saída de `codex exec` quando a cota do ChatGPT Plus acabou, em 2026-11-10 (reset às 22:05) |
| `kiro/login-io-error-2026-11-14.txt` | `kiro-cli chat --no-interactive` com o DNS do WSL quebrado em 2026-11-14: indisponibilidade que **não** é cota |
| `kiro/whoami-not-logged-in-2026-11-14.txt` | `kiro-cli whoami` na mesma situação |
| `claude/quota-synthetic.txt`, `agy/quota-synthetic.txt`, `kiro/quota-synthetic.txt` | **Sintéticas**, criadas no 7C para testar as gramáticas v1; não comprovam formato/versão de runtime real |

`tests/fake_subscription_cli.py` também produz envelopes e falhas **sintéticos**;
nenhum teste automatizado desta entrega chama CLI real ou rede.
