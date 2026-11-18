"""Classificação manual de TODA unidade elegível (no regime shared_global) para cada tarefa do 7F.

Classes disjuntas, além do `gold` declarado em f7_tasks.py:

- `alternatives`: conteúdo correto para a tarefa vindo de outra família ou papel. Escolhê-las não é
  contaminação e conta como neutra nas métricas de precisão.
- `distractors`: conteúdo inadequado ou contraditório (regra de outra família que daria resposta errada
  ou regra fictícia). Só estas contam como contaminação.
- `irrelevant`: elegível pelo filtro de tópicos, mas sem resposta nem conflito para a tarefa (com motivo).

`answer_sources` declara, por checagem de resposta (inclusive booleanos e números), as fontes que a
sustentam, inclusive por paráfrase. Nenhum distrator pode constar nelas (verificado nos testes).
"""

from __future__ import annotations

SYN = "Regra fictícia que contradiz a regra real do ORQ."

CLASSES: dict[str, dict[str, dict]] = {
    "architect-claude-tools": {
        "alternatives": {
            "family-claude-security": "Repete flag, ferramentas negadas e tratamento do custo do Claude Code (visível só ao papel security_reviewer).",
        },
        "distractors": {
            "family-kiro-trust-tools": "Outra flag de permissão (`--trust-tools`), da família Kiro.",
            "family-gemini-permissions": "Regras `permissions.allow` do agy no lugar de `--disallowedTools`.",
            "syn-codex-process": SYN,
            "syn-codex-cost": SYN,
        },
        "irrelevant": {
            "family-kiro-output": "Trata de créditos do Kiro; não menciona `total_cost_usd` nem ferramentas negadas.",
        },
        "answer_sources": {
            "CHK-deny-flag": ["family-claude-invocation", "family-claude-security"],
            "CHK-denied-tools": ["family-claude-invocation", "family-claude-security"],
            "CHK-cost": ["common-cost-honesty", "family-claude-security"],
        },
    },
    "architect-codex-readonly": {
        "alternatives": {},
        "distractors": {
            "family-gemini-readonly": "Modo somente leitura por `--mode plan`, do agy.",
            "family-kiro-trust-tools": "Somente leitura por `--trust-tools=fs_read`, do Kiro.",
            "family-claude-invocation": "Saída por `--output-format json`, do Claude Code.",
            "family-gemini-invocation": "Saída por `--output-format text`, do agy.",
            "syn-kiro-coverage": SYN,
            "syn-claude-marker": SYN,
        },
        "irrelevant": {
            "common-output-protocol": "Descreve o marcador ORQ_FINAL; não traz flags do Codex nem cobertura de contexto.",
        },
        "answer_sources": {
            "CHK-sandbox": ["family-codex-readonly"],
            "CHK-final-flag": ["family-codex-invocation"],
            "CHK-events-flag": ["family-codex-invocation"],
            "CHK-coverage": ["role-software-architect"],
        },
    },
    "architect-gemini-workdir": {
        "alternatives": {
            "family-claude-invocation": "Também fixa o diretório por `cwd=workspace_dir` e usa `--model` (visível só à família Claude).",
            "family-kiro-invocation": "Também usa `--model` (visível só à família Kiro).",
        },
        "distractors": {
            "family-codex-invocation": "Diretório por `-C` e modelo por `-m`, do Codex.",
            "family-codex-readonly": "Somente leitura por `-s read-only`, do Codex.",
            "family-kiro-trust-tools": "Somente leitura por `--trust-tools=fs_read`, do Kiro.",
            "syn-codex-comparison": SYN,
        },
        "irrelevant": {},
        "answer_sources": {
            "CHK-workdir": ["family-gemini-invocation", "family-claude-invocation"],
            "CHK-model-flag": ["family-gemini-invocation", "family-claude-invocation", "family-kiro-invocation"],
            "CHK-readonly-mode": ["family-gemini-readonly"],
            "CHK-comparison-unit": ["role-software-architect"],
        },
    },
    "backend-codex-windows-tests": {
        "alternatives": {},
        "distractors": {
            "family-gemini-permissions": "Sintoma `no output produced` do agy, que não é o sintoma do sandbox do Codex.",
            "family-kiro-trust-tools": "Trata de `execute_bash` e `--trust-tools` do Kiro.",
            "syn-gemini-tests": SYN,
            "syn-claude-permissions": SYN,
        },
        "irrelevant": {
            "role-tester": "Guia do papel tester sobre CLIs falsas e evidência; sem opções de runner nem flags do Codex.",
            "family-claude-invocation": "Flags do Claude Code; sem relação com sandbox de testes ou repositório Git.",
            "family-codex-readonly": "Modo somente leitura do Codex; sem execução de testes nem repositório Git.",
            "family-gemini-readonly": "Modo somente leitura do agy; sem relação com a tarefa.",
            "family-gemini-security": "Revisão de segurança do agy; sem relação com a tarefa.",
            "family-kiro-invocation": "Invocação do Kiro; sem relação com a tarefa.",
            "family-kiro-security": "Revisão de segurança do Kiro; sem relação com a tarefa.",
        },
        "answer_sources": {
            "CHK-symptom": ["family-codex-windows-tests"],
            "CHK-runner-options": ["family-codex-windows-tests"],
            "CHK-pytest": ["family-codex-windows-tests"],
            "CHK-git-flag": ["family-codex-invocation"],
        },
    },
    "backend-gemini-headless": {
        "alternatives": {
            "family-gemini-security": "Cita `permissions.allow`, `read_file` e `command` do agy (visível só ao papel security_reviewer).",
        },
        "distractors": {
            "family-claude-invocation": "Permissões por `--allowedTools`, do Claude Code.",
            "family-kiro-trust-tools": "Permissões por `--trust-tools`, do Kiro.",
            "family-codex-invocation": "Permissões por `-s`, do Codex.",
            "family-kiro-output": "Ruído `[tool]` do Kiro no lugar do ruído do agy.",
            "syn-codex-noise": SYN,
            "syn-claude-permissions": SYN,
        },
        "irrelevant": {
            "family-gemini-readonly": "Modos `plan`/`accept-edits`; não traz regra de permissão, código de erro nem ruído de stderr.",
            "family-codex-readonly": "Somente leitura do Codex; sem relação com a tarefa.",
            "family-kiro-invocation": "Invocação do Kiro; sem relação com a tarefa.",
            "family-kiro-security": "Revisão de segurança do Kiro; sem relação com a tarefa.",
        },
        "answer_sources": {
            "CHK-cause": ["family-gemini-permissions"],
            "CHK-settings-key": ["family-gemini-permissions", "family-gemini-security"],
            "CHK-rule-kinds": ["family-gemini-permissions", "family-gemini-security"],
            "CHK-error-code": ["family-gemini-permissions"],
            "CHK-noise": ["family-gemini-noise"],
        },
    },
    "backend-kiro-wsl": {
        "alternatives": {
            "family-kiro-security": "Manda recusar `--trust-all-tools` (sustenta `trust_all_tools_allowed=false`); visível só ao papel security_reviewer.",
        },
        "distractors": {
            "family-codex-invocation": "Diretório por `-C` e permissões por `-s`, do Codex.",
            "family-gemini-permissions": "Regras `permissions.allow` do agy.",
            "family-claude-invocation": "Permissões por `--allowedTools`, do Claude Code.",
            "syn-codex-process": SYN,
            "syn-claude-permissions": SYN,
        },
        "irrelevant": {
            "family-gemini-invocation": "Invocação do agy (cwd); não contradiz nem responde a invocação do Kiro.",
            "family-gemini-security": "Revisão de segurança do agy; sem relação com a tarefa.",
            "family-codex-readonly": "Somente leitura do Codex; sem relação com a tarefa.",
            "family-gemini-readonly": "Modos do agy; sem relação com a tarefa.",
        },
        "answer_sources": {
            "CHK-launcher": ["family-kiro-invocation"],
            "CHK-prefix": ["family-kiro-invocation"],
            "CHK-trust-tools": ["family-kiro-trust-tools"],
            "CHK-trust-all": ["family-kiro-trust-tools", "family-kiro-security"],
        },
    },
    "tester-claude-digest": {
        "alternatives": {},
        "distractors": {
            "syn-gemini-digest": SYN,
            "syn-kiro-numbers": SYN,
            "syn-gemini-tests": SYN,
        },
        "irrelevant": {
            "common-content-contract": "Digest do schema de contrato de conteúdo; não traz o formato do digest nem a faixa numérica.",
            "role-backend-engineer": "Cita `state_hash` JCS dos adaptadores; sem formato de digest nem números rejeitados.",
            "family-codex-windows-tests": "Testes no sandbox do Codex; sem relação com o contrato de digest.",
        },
        "answer_sources": {
            "CHK-pattern": ["common-jcs-digest"],
            "CHK-algorithm": ["common-jcs-digest"],
            "CHK-max-int": ["common-jcs-digest"],
            "CHK-rejected": ["common-jcs-digest"],
        },
    },
    "tester-kiro-quota": {
        "alternatives": {
            "family-claude-quota": "Restringe a detecção ao canal de erro/status (sustenta, por paráfrase, `free_stdout_text_is_quota_source=false`).",
            "family-gemini-quota": "Restringe a detecção ao canal de erro/status (sustenta, por paráfrase, `free_stdout_text_is_quota_source=false`).",
            "family-kiro-quota": "Restringe a detecção ao canal de erro/status (sustenta, por paráfrase, `free_stdout_text_is_quota_source=false`).",
        },
        "distractors": {
            "syn-codex-cooldown": SYN,
            "syn-gemini-tests": SYN,
            "syn-claude-stdout": SYN,
        },
        "irrelevant": {
            "family-codex-quota": "Mensagem do Codex e horário `try again at`; não trata cooldown padrão nem canal de detecção.",
            "family-codex-windows-tests": "Testes no sandbox do Codex; sem relação com cota.",
        },
        "answer_sources": {
            "CHK-cooldown": ["common-quota-cooldown"],
            "CHK-source": ["common-quota-cooldown"],
            "CHK-status": ["common-quota-cooldown"],
            "CHK-reason": ["common-quota-cooldown"],
            "CHK-stdout": ["common-quota-cooldown", "family-claude-quota", "family-gemini-quota", "family-kiro-quota"],
        },
    },
    "security-claude-policy": {
        "alternatives": {},
        "distractors": {
            "syn-gemini-policy": SYN,
            "syn-codex-process": SYN,
            "family-kiro-security": "Lista `execute_bash` e `--trust-all-tools`, do Kiro, no lugar das ferramentas do Claude Code.",
            "family-gemini-security": "Lista `read_file`/`command` e `permissions.allow`, do agy.",
            "family-kiro-trust-tools": "Lista `--trust-tools` e `execute_bash`, do Kiro.",
        },
        "irrelevant": {
            "family-codex-readonly": "Somente leitura do Codex; sem política de assinatura, diretórios ou lançamento de processos.",
            "family-gemini-readonly": "Modos do agy; sem relação com a tarefa.",
        },
        "answer_sources": {
            "CHK-env-vars": ["common-subscription-policy"],
            "CHK-credential-dirs": ["common-subscription-policy"],
            "CHK-spawn": ["common-host-process-rules"],
            "CHK-denied-tools": ["family-claude-security"],
        },
    },
    "security-kiro-trust-tools": {
        "alternatives": {},
        "distractors": {
            "family-gemini-security": "Flag proibida do agy (`--dangerously-skip-permissions`) e regras `permissions.allow`.",
            "family-claude-security": "Ferramentas negadas por `--disallowedTools`, do Claude Code.",
            "family-gemini-permissions": "Regras `permissions.allow` do agy.",
            "family-codex-readonly": "Somente leitura por `-s read-only`, do Codex.",
            "family-claude-invocation": "Permissões por `--allowedTools`/`--disallowedTools`, do Claude Code.",
            "family-codex-invocation": "Permissões por `-s`, do Codex.",
            "family-gemini-readonly": "Somente leitura por `--mode plan`, do agy.",
            "syn-claude-permissions": SYN,
        },
        "irrelevant": {
            "family-kiro-invocation": "Mostra `--trust-tools=<lista_fechada>` sem os valores pedidos (somente leitura, execute_bash).",
        },
        "answer_sources": {
            "CHK-prohibited": ["family-kiro-trust-tools", "family-kiro-security", "role-security-reviewer"],
            "CHK-read-only": ["family-kiro-trust-tools"],
            "CHK-precondition": ["family-kiro-trust-tools", "family-kiro-security"],
            "CHK-scope": ["family-kiro-trust-tools", "family-kiro-security", "role-security-reviewer"],
        },
    },
}
