# Instruções do projeto ORQ

## Ao iniciar uma conversa

1. Leia [docs/HANDOFF.md](docs/HANDOFF.md) e o [andamento da Fase 1](docs/phase-1/README.md).
2. Confira git status, branch, remoto e alterações recentes antes de editar.
3. Retome o próximo passo registrado, respeitando instruções novas do usuário. Não refaça entregas existentes.
4. Trate documentos marcados como proposta como decisões revisáveis, não como implementação concluída.

## Comunicação e desenvolvimento

- Responda em português, de forma curta, para economizar tokens. Documentos podem conter o detalhe necessário.
- Trabalhe em etapas pequenas, verificáveis e mensuráveis.
- Antes de implementar uma parte, explique problema, alternativas, vantagens/desvantagens, recomendação, interfaces, schemas, riscos e testes.
- Não implemente grandes partes de uma vez. A Fase 1 está em especificação; não iniciar código de aplicação enquanto seus contratos não estiverem definidos e revisados.
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
