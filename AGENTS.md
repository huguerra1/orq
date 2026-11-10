# Instruções do projeto ORQ

## Ao iniciar uma conversa

1. Leia [docs/HANDOFF.md](docs/HANDOFF.md), o [baseline da Fase 1](docs/phase-1/README.md) e o [andamento da Fase 2](docs/phase-2/README.md).
2. Confira git status, branch, remoto e alterações recentes antes de editar.
3. Retome o próximo passo registrado, respeitando instruções novas do usuário. Não refaça entregas existentes.
4. Trate documentos marcados como proposta como decisões revisáveis, não como implementação concluída.

## Comunicação e desenvolvimento

- Responda em português, de forma curta, para economizar tokens. Documentos podem conter o detalhe necessário.
- Trabalhe em etapas pequenas, verificáveis e mensuráveis.
- Antes de implementar uma parte, explique problema, alternativas, vantagens/desvantagens, recomendação, interfaces, schemas, riscos e testes.
- Não implemente grandes partes de uma vez. O baseline v0.1 da Fase 1 está fechado; código novo deve seguir seus contratos e os gates registrados na Fase 2.
- Apresente alternativas para dúvidas arquiteturais relevantes, sem assumir decisões silenciosamente.
- Mantenha o MVP pequeno. Questione a necessidade de novos componentes, dependências e serviços.
- Não confunda casos de aceitação documentados com testes executados.
- Atualize HANDOFF e os índices ao concluir uma entrega, registrando decisões, verificações, pendências e o próximo passo.

## Arquitetura

- Separar papel, modelo, provedor, runtime e executor.
- Orchestrator controla estados, limites e tentativas; MCP fornece ferramentas pequenas e conhecimento.
- Começar com aplicação modular local e execução sequencial de um DAG.
- RAG Markdown separa conhecimento operacional e de projeto; cada tarefa recebe apenas contexto necessário.
- Execution Memory registra fatos; perfis editoriais não inventam métricas.
- Priorizar avaliação determinística e evidências ligadas aos artefatos.
- Preservar versões, contexto, decisões, tentativas e custos conhecidos para comparação experimental.
- Não transformar unknown em sucesso ou custo zero; não repetir execução externa ainda indeterminada.

## Git

- Repositório autorizado: git@github.com:huguerra1/orq.git. Branch atual: main.
- O usuário autorizou commits e pushes incrementais das alterações revisadas deste projeto.
- Identidade local exigida: user.name = huguerra1; user.email = hugofmourao@gmail.com.
- Usar configuração local deste repositório. Não alterar a configuração global de trabalho.
- Conferir autor e committer antes de publicar. Não adicionar autoria do assistente, Co-authored-by ou atribuição automática a ferramentas de IA.
- Não reescrever commits já publicados nem fazer force push sem instrução explícita.
- Conferir diferenças e verificações apropriadas; preservar alterações do usuário.
- Chaves, tokens e outros segredos não entram no Git.
- Se o push falhar, relatar claramente o que está apenas local. Respeitar aprovações técnicas exigidas pelo ambiente.

## Regras de autonomia (agentes de IA)

Este repositório faz parte do plano de melhoria contínua do portfólio. O painel geral fica no repositório privado `portfolio-ops`.

1. **Branches:** todo trabalho acontece em `claude/<tema>`, com PRs pequenos e focados. O CI precisa estar verde antes do merge.
2. **Integração:** os PRs dos agentes entram na branch `dev`. A `main` só muda quando o dono revisar e aprovar o PR `dev → main`.
3. **Backlog:** o arquivo `ROADMAP.md` é a fonte da verdade. Atualize o status ao final de cada execução.
4. **Nunca:** apagar o repositório, mudar a visibilidade, commitar segredos (use `.env` + `.env.example`), reescrever o histórico da `main`, fazer force-push em branches que não são suas, ou adicionar serviços e dependências pagas. Esses pontos sempre voltam para o dono.
5. **Pronto:** quando o checklist de "pronto" do `ROADMAP.md` estiver completo, marque o status como **PRONTO PARA REVISÃO** e passe para o próximo repositório da fila no `portfolio-ops`.
6. **Commits:** mensagens em português, no imperativo, curtas e descritivas (nada de "atualização").
7. **Modo autônomo × regras acima:** quando não houver um humano na conversa, a explicação de alternativas e riscos exigida antes de implementar vai para a descrição do PR e para o HANDOFF. Decisões arquiteturais abertas viram uma proposta em `docs/` e não são implementadas até a revisão do dono.
