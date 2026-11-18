---
title: "Política de uso das assinaturas"
language: pt-BR
topics: [politica-de-assinatura, seguranca, assinaturas]
---
# Política de uso das assinaturas

O hub de assinaturas opera sob quatro restrições inegociáveis:

1. Só as CLIs oficiais (`claude`, `codex`, `agy`, `kiro-cli`), em modo não interativo, com stdin fechado.
2. Nunca ler, copiar ou retransmitir tokens ou credenciais das CLIs, como os diretórios `~/.codex/`, `~/.claude/` e `~/.gemini/`.
3. Nunca automatizar interface web (Playwright, Selenium, Puppeteer, scraping).
4. Sem API paga e sem LLM local: não injetar `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` nem `GEMINI_API_KEY`.
