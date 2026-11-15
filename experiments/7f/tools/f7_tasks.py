"""Definição das 10 tarefas do benchmark 7F (fonte do gerador; os JSONs finais ficam em tasks/).

Cada tarefa pede uma resposta JSON curta e fechada. O contrato de saída entregue ao agente é
permissivo (tipos e enums com todas as opções); o gabarito vive nas checagens determinísticas.
"""

from __future__ import annotations

from typing import Any

TXT = {"type": "string", "minLength": 1}
LIST_TXT = {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 8, "uniqueItems": True}


def enum(*values: str) -> dict[str, Any]:
    return {"enum": list(values)}


def equals(check_id: str, pointer: str, value: Any) -> dict[str, Any]:
    return {"check_id": check_id, "kind": "equals", "pointer": pointer, "value": value}


def set_equals(check_id: str, pointer: str, values: list[Any]) -> dict[str, Any]:
    return {"check_id": check_id, "kind": "set_equals", "pointer": pointer, "values": values}


def contains_all(check_id: str, pointer: str, values: list[Any]) -> dict[str, Any]:
    return {"check_id": check_id, "kind": "contains_all", "pointer": pointer, "values": values}


def forbidden(values: list[str]) -> dict[str, Any]:
    return {"check_id": "CHK-no-contamination", "kind": "no_forbidden_text", "values": values}


def kr(kr_id: str, subject: str, topics: list[str], gold: list[str]) -> dict[str, Any]:
    return {"id": kr_id, "subject": subject, "topics": topics, "gold": gold}


TASKS: list[dict[str, Any]] = [
    {
        "slug": "architect-claude-tools",
        "role": "software_architect",
        "family": "family_claude",
        "title": "Ferramentas negadas e custo no Claude Code",
        "statement": (
            "Você atua como software_architect na família Claude. Uma revisão arquitetural sem escrita será "
            "executada com o Claude Code sob assinatura. Responda só com o JSON do contrato de saída: "
            "`deny_flag` (flag exata da CLI que lista as ferramentas negadas), `denied_tools` (as ferramentas "
            "que devem ser negadas quando a tarefa não as autoriza) e `total_cost_usd_treatment` (como tratar "
            "o campo total_cost_usd do envelope JSON)."
        ),
        "krs": [
            kr("KR1", "Ferramentas negadas pelo Claude Code", ["ferramentas"], ["family-claude-invocation"]),
            kr("KR2", "Tratamento do custo informado pela CLI", ["custo"], ["common-cost-honesty"]),
        ],
        "properties": {
            "deny_flag": TXT,
            "denied_tools": LIST_TXT,
            "total_cost_usd_treatment": enum("informative_only", "billed_cost", "zero_cost"),
        },
        "checks": [
            equals("CHK-deny-flag", "/deny_flag", "--disallowedTools"),
            set_equals("CHK-denied-tools", "/denied_tools", ["Bash", "WebFetch", "WebSearch"]),
            equals("CHK-cost", "/total_cost_usd_treatment", "informative_only"),
            forbidden(["-s read-only", "--mode plan", "--trust-tools", "--trust-all-tools"]),
        ],
        "reference": {
            "deny_flag": "--disallowedTools",
            "denied_tools": ["WebFetch", "WebSearch", "Bash"],
            "total_cost_usd_treatment": "informative_only",
        },
        "contaminated": {
            "deny_flag": "--trust-tools=fs_read",
            "denied_tools": ["execute_bash"],
            "total_cost_usd_treatment": "billed_cost",
        },
    },
    {
        "slug": "architect-codex-readonly",
        "role": "software_architect",
        "family": "family_codex",
        "title": "Revisão somente leitura no Codex CLI",
        "statement": (
            "Você atua como software_architect na família Codex. Planeje a invocação do Codex CLI para uma "
            "revisão arquitetural sem escrita. Responda só com o JSON do contrato de saída: `sandbox_mode` "
            "(valor do argumento de `-s`), `final_message_flag` (flag que grava a mensagem final em arquivo), "
            "`events_flag` (flag que emite eventos JSONL) e `max_provider_context_coverage` (o valor máximo "
            "honesto de provider_context_coverage)."
        ),
        "krs": [
            kr("KR1", "Modo somente leitura do Codex", ["somente-leitura"], ["family-codex-readonly"]),
            kr("KR2", "Saída estruturada do Codex", ["saida-estruturada"], ["family-codex-invocation"]),
            kr("KR3", "Cobertura de contexto do provedor", ["cobertura-de-contexto"], ["role-software-architect"]),
        ],
        "properties": {
            "sandbox_mode": enum("read-only", "workspace-write", "danger-full-access"),
            "final_message_flag": TXT,
            "events_flag": TXT,
            "max_provider_context_coverage": enum("complete", "platform_only", "unknown"),
        },
        "checks": [
            equals("CHK-sandbox", "/sandbox_mode", "read-only"),
            equals("CHK-final-flag", "/final_message_flag", "-o"),
            equals("CHK-events-flag", "/events_flag", "--json"),
            equals("CHK-coverage", "/max_provider_context_coverage", "platform_only"),
            forbidden(["--mode plan", "--trust-tools", "--output-format"]),
        ],
        "reference": {
            "sandbox_mode": "read-only",
            "final_message_flag": "-o",
            "events_flag": "--json",
            "max_provider_context_coverage": "platform_only",
        },
        "contaminated": {
            "sandbox_mode": "workspace-write",
            "final_message_flag": "--output-format",
            "events_flag": "--output-format",
            "max_provider_context_coverage": "complete",
        },
    },
    {
        "slug": "architect-gemini-workdir",
        "role": "software_architect",
        "family": "family_gemini",
        "title": "Diretório, modelo e modo somente leitura no agy",
        "statement": (
            "Você atua como software_architect na família Gemini. Desenhe a invocação não interativa do `agy` "
            "para uma revisão sem escrita. Responda só com o JSON do contrato de saída: `workdir_mechanism` "
            "(como o adaptador fixa o diretório de trabalho), `model_flag` (flag exata do modelo), "
            "`readonly_mode` (valor de `--mode` para tarefas somente leitura) e `comparison_unit` "
            "(a unidade de comparação declarada no experimento)."
        ),
        "krs": [
            kr("KR1", "Diretório de trabalho e modelo do agy", ["diretorio-de-trabalho", "modelo"], ["family-gemini-invocation"]),
            kr("KR2", "Modo somente leitura do agy", ["somente-leitura"], ["family-gemini-readonly"]),
            kr("KR3", "Unidade de comparação", ["unidade-de-comparacao"], ["role-software-architect"]),
        ],
        "properties": {
            "workdir_mechanism": enum("flag_C", "subprocess_cwd", "flag_cd"),
            "model_flag": TXT,
            "readonly_mode": enum("accept-edits", "plan"),
            "comparison_unit": enum("model_only", "cli_family_runtime", "subscription_plan"),
        },
        "checks": [
            equals("CHK-workdir", "/workdir_mechanism", "subprocess_cwd"),
            equals("CHK-model-flag", "/model_flag", "--model"),
            equals("CHK-readonly-mode", "/readonly_mode", "plan"),
            equals("CHK-comparison-unit", "/comparison_unit", "cli_family_runtime"),
            forbidden(["-C", "--cd"]),
        ],
        "reference": {
            "workdir_mechanism": "subprocess_cwd",
            "model_flag": "--model",
            "readonly_mode": "plan",
            "comparison_unit": "cli_family_runtime",
        },
        "contaminated": {
            "workdir_mechanism": "flag_C",
            "model_flag": "-m",
            "readonly_mode": "accept-edits",
            "comparison_unit": "model_only",
        },
    },
    {
        "slug": "backend-codex-windows-tests",
        "role": "backend_engineer",
        "family": "family_codex",
        "title": "Testes no sandbox do Codex no Windows",
        "statement": (
            "Você atua como backend_engineer na família Codex, em Windows 10, rodando testes dentro do sandbox "
            "`workspace-write`. Responda só com o JSON do contrato de saída: `failure_symptom` (erro típico de "
            "runners com vários processos), `test_runner_options` (opções documentadas que contornam o problema "
            "em vitest/jest), `pytest_execution` (como rodar o pytest) e `non_git_workspace_flag` (flag exigida "
            "quando o workspace não é um repositório Git completo)."
        ),
        "krs": [
            kr("KR1", "Execução de testes no sandbox do Windows", ["testes"], ["family-codex-windows-tests"]),
            kr("KR2", "Sandbox e permissões do Codex", ["sandbox", "permissoes"], ["family-codex-invocation"]),
        ],
        "properties": {
            "failure_symptom": enum("spawn_EPERM", "no_output_produced", "ENOENT"),
            "test_runner_options": LIST_TXT,
            "pytest_execution": enum("sequential", "parallel"),
            "non_git_workspace_flag": TXT,
        },
        "checks": [
            equals("CHK-symptom", "/failure_symptom", "spawn_EPERM"),
            set_equals("CHK-runner-options", "/test_runner_options", ["--pool threads", "--runInBand"]),
            equals("CHK-pytest", "/pytest_execution", "sequential"),
            equals("CHK-git-flag", "/non_git_workspace_flag", "--skip-git-repo-check"),
            forbidden(["-n auto", "--workers", "execute_bash"]),
        ],
        "reference": {
            "failure_symptom": "spawn_EPERM",
            "test_runner_options": ["--pool threads", "--runInBand"],
            "pytest_execution": "sequential",
            "non_git_workspace_flag": "--skip-git-repo-check",
        },
        "contaminated": {
            "failure_symptom": "no_output_produced",
            "test_runner_options": ["-n auto", "--workers 8"],
            "pytest_execution": "parallel",
            "non_git_workspace_flag": "--trust-workspace",
        },
    },
    {
        "slug": "backend-gemini-headless",
        "role": "backend_engineer",
        "family": "family_gemini",
        "title": "Saída vazia do agy em modo headless",
        "statement": (
            "Você atua como backend_engineer na família Gemini. Uma tentativa do `agy -p` terminou sem saída útil. "
            "Responda só com o JSON do contrato de saída: `empty_output_cause` (causa da saída vazia), "
            "`settings_key` (chave das regras de permissão), `rule_kinds` (formas de regra, sem argumentos, "
            "que o adaptador projeta), `error_code` (código do ErrorRecord a registrar) e `stderr_noise_prefix` "
            "(linha de ruído do stderr que o parser filtra)."
        ),
        "krs": [
            kr("KR1", "Permissões do agy em modo headless", ["permissoes"], ["family-gemini-permissions"]),
            kr("KR2", "Ruído de saída do agy", ["ruido"], ["family-gemini-noise"]),
        ],
        "properties": {
            "empty_output_cause": enum("tool_denied_without_permission_rule", "timeout_reached", "invalid_model"),
            "settings_key": TXT,
            "rule_kinds": LIST_TXT,
            "error_code": TXT,
            "stderr_noise_prefix": TXT,
        },
        "checks": [
            equals("CHK-cause", "/empty_output_cause", "tool_denied_without_permission_rule"),
            equals("CHK-settings-key", "/settings_key", "permissions.allow"),
            set_equals("CHK-rule-kinds", "/rule_kinds", ["read_file", "command"]),
            equals("CHK-error-code", "/error_code", "permission_or_empty_output"),
            equals("CHK-noise", "/stderr_noise_prefix", "logging before google.Init"),
            forbidden(["--allowedTools", "--trust-tools", "spawn EPERM", "[tool]", "WARN"]),
        ],
        "reference": {
            "empty_output_cause": "tool_denied_without_permission_rule",
            "settings_key": "permissions.allow",
            "rule_kinds": ["read_file", "command"],
            "error_code": "permission_or_empty_output",
            "stderr_noise_prefix": "logging before google.Init",
        },
        "contaminated": {
            "empty_output_cause": "timeout_reached",
            "settings_key": "--allowedTools",
            "rule_kinds": ["fs_read", "execute_bash"],
            "error_code": "permission_or_empty_output",
            "stderr_noise_prefix": "[tool]",
        },
    },
    {
        "slug": "backend-kiro-wsl",
        "role": "backend_engineer",
        "family": "family_kiro",
        "title": "Invocação do Kiro CLI via WSL",
        "statement": (
            "Você atua como backend_engineer na família Kiro, com o runner em Windows. Monte a invocação não "
            "interativa do Kiro CLI. Responda só com o JSON do contrato de saída: `windows_launcher` "
            "(executável usado no Windows), `workspace_path_prefix` (prefixo do caminho do workspace visto "
            "pelo WSL), `edit_without_commands_trust_tools` (argumento exato para editar arquivos sem "
            "executar comandos) e `trust_all_tools_allowed` (se a flag de confiar em todas as ferramentas é permitida)."
        ),
        "krs": [
            kr("KR1", "Invocação do Kiro no Windows", ["invocacao"], ["family-kiro-invocation"]),
            kr("KR2", "Ferramentas confiáveis do Kiro", ["permissoes"], ["family-kiro-trust-tools"]),
        ],
        "properties": {
            "windows_launcher": enum("wsl.exe", "cmd.exe", "powershell.exe"),
            "workspace_path_prefix": TXT,
            "edit_without_commands_trust_tools": TXT,
            "trust_all_tools_allowed": {"type": "boolean"},
        },
        "checks": [
            equals("CHK-launcher", "/windows_launcher", "wsl.exe"),
            equals("CHK-prefix", "/workspace_path_prefix", "/mnt/c/"),
            equals("CHK-trust-tools", "/edit_without_commands_trust_tools", "--trust-tools=fs_read,fs_write"),
            equals("CHK-trust-all", "/trust_all_tools_allowed", False),
            forbidden(["--trust-all-tools", "--dangerously-skip-permissions", "-s workspace-write", "--allowedTools"]),
        ],
        "reference": {
            "windows_launcher": "wsl.exe",
            "workspace_path_prefix": "/mnt/c/",
            "edit_without_commands_trust_tools": "--trust-tools=fs_read,fs_write",
            "trust_all_tools_allowed": False,
        },
        "contaminated": {
            "windows_launcher": "cmd.exe",
            "workspace_path_prefix": "C:\\dev\\",
            "edit_without_commands_trust_tools": "-s workspace-write",
            "trust_all_tools_allowed": True,
        },
    },
    {
        "slug": "tester-claude-digest",
        "role": "tester",
        "family": "family_claude",
        "title": "Casos de teste do contrato de digest",
        "statement": (
            "Você atua como tester na família Claude e vai escrever casos de teste para o contrato de digest "
            "do ORQ. Responda só com o JSON do contrato de saída: `digest_value_pattern` (regex exata do "
            "`value`), `digest_algorithm` (valor fixo de `algorithm`), `max_safe_integer` (maior inteiro "
            "aceito) e `rejected_number_kinds` (as categorias de número que a canonicalização rejeita)."
        ),
        "krs": [
            kr("KR1", "Digest JCS e números aceitos", ["digest"], ["common-jcs-digest"]),
            kr("KR2", "Práticas de teste do papel", ["testes"], ["role-tester"]),
        ],
        "properties": {
            "digest_value_pattern": TXT,
            "digest_algorithm": TXT,
            "max_safe_integer": {"type": "integer"},
            "rejected_number_kinds": {
                "type": "array",
                "items": enum("integer_outside_safe_range", "negative_zero", "non_finite", "float_with_fraction", "negative_integer"),
                "minItems": 1,
                "maxItems": 5,
                "uniqueItems": True,
            },
        },
        "checks": [
            equals("CHK-pattern", "/digest_value_pattern", "^[a-f0-9]{64}$"),
            equals("CHK-algorithm", "/digest_algorithm", "sha256"),
            equals("CHK-max-int", "/max_safe_integer", 9007199254740991),
            set_equals("CHK-rejected", "/rejected_number_kinds", ["integer_outside_safe_range", "negative_zero", "non_finite"]),
            forbidden(["sha512", "base64", "2147483647"]),
        ],
        "reference": {
            "digest_value_pattern": "^[a-f0-9]{64}$",
            "digest_algorithm": "sha256",
            "max_safe_integer": 9007199254740991,
            "rejected_number_kinds": ["integer_outside_safe_range", "negative_zero", "non_finite"],
        },
        "contaminated": {
            "digest_value_pattern": "^[A-Za-z0-9+/=]+$",
            "digest_algorithm": "sha512",
            "max_safe_integer": 2147483647,
            "rejected_number_kinds": ["integer_outside_safe_range"],
        },
    },
    {
        "slug": "tester-kiro-quota",
        "role": "tester",
        "family": "family_kiro",
        "title": "Casos de teste do cooldown de cota",
        "statement": (
            "Você atua como tester na família Kiro e vai escrever casos de teste para o bloqueio por cota. "
            "Responda só com o JSON do contrato de saída: `default_cooldown_ms` (padrão quando a CLI não "
            "informa horário de reset), `cooldown_reset_source` (origem registrada nesse caso), "
            "`all_blocked_decision_status` e `all_blocked_reason` (resultado do Router quando todas as "
            "famílias elegíveis estão bloqueadas) e `free_stdout_text_is_quota_source` (se texto livre de "
            "stdout é fonte de cota)."
        ),
        "krs": [
            kr("KR1", "Cooldown e fallback de cota", ["cooldown", "cota"], ["common-quota-cooldown"]),
            kr("KR2", "Práticas de teste do papel", ["testes"], ["role-tester"]),
        ],
        "properties": {
            "default_cooldown_ms": {"type": "integer", "minimum": 1},
            "cooldown_reset_source": enum("cli_reported", "cooldown_policy", "clock_expired"),
            "all_blocked_decision_status": TXT,
            "all_blocked_reason": TXT,
            "free_stdout_text_is_quota_source": {"type": "boolean"},
        },
        "checks": [
            equals("CHK-cooldown", "/default_cooldown_ms", 3600000),
            equals("CHK-source", "/cooldown_reset_source", "cooldown_policy"),
            equals("CHK-status", "/all_blocked_decision_status", "no_eligible_target"),
            equals("CHK-reason", "/all_blocked_reason", "all_eligible_families_quota_exhausted"),
            equals("CHK-stdout", "/free_stdout_text_is_quota_source", False),
            forbidden(["1800000"]),
        ],
        "reference": {
            "default_cooldown_ms": 3600000,
            "cooldown_reset_source": "cooldown_policy",
            "all_blocked_decision_status": "no_eligible_target",
            "all_blocked_reason": "all_eligible_families_quota_exhausted",
            "free_stdout_text_is_quota_source": False,
        },
        "contaminated": {
            "default_cooldown_ms": 1800000,
            "cooldown_reset_source": "clock_expired",
            "all_blocked_decision_status": "no_eligible_target",
            "all_blocked_reason": "all_eligible_families_quota_exhausted",
            "free_stdout_text_is_quota_source": False,
        },
    },
    {
        "slug": "security-claude-policy",
        "role": "security_reviewer",
        "family": "family_claude",
        "title": "Política de assinaturas e Claude Code",
        "statement": (
            "Você atua como security_reviewer na família Claude e revisa um adaptador de assinatura. Responda "
            "só com o JSON do contrato de saída: `forbidden_env_vars` (variáveis de cobrança que nunca podem "
            "ser injetadas), `credential_directories` (diretórios de credenciais que nunca podem ser lidos), "
            "`spawn_policy` (como o host lança a CLI) e `claude_denied_tools_when_unauthorized` (ferramentas "
            "do Claude Code que a política nega quando a tarefa não as autoriza)."
        ),
        "krs": [
            kr("KR1", "Política de uso das assinaturas", ["politica-de-assinatura"], ["common-subscription-policy"]),
            kr("KR2", "Checklist de revisão de segurança", ["revisao"], ["role-security-reviewer", "family-claude-security"]),
            kr("KR3", "Execução de processos pelo host", ["processo"], ["common-host-process-rules"]),
        ],
        "properties": {
            "forbidden_env_vars": LIST_TXT,
            "credential_directories": LIST_TXT,
            "spawn_policy": enum("shell_false", "shell_true"),
            "claude_denied_tools_when_unauthorized": LIST_TXT,
        },
        "checks": [
            set_equals("CHK-env-vars", "/forbidden_env_vars", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"]),
            set_equals("CHK-credential-dirs", "/credential_directories", ["~/.codex/", "~/.claude/", "~/.gemini/"]),
            equals("CHK-spawn", "/spawn_policy", "shell_false"),
            set_equals("CHK-denied-tools", "/claude_denied_tools_when_unauthorized", ["Bash", "WebFetch", "WebSearch"]),
            forbidden(["--trust-all-tools", "--dangerously-skip-permissions", "--trust-tools", "execute_bash"]),
        ],
        "reference": {
            "forbidden_env_vars": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"],
            "credential_directories": ["~/.codex/", "~/.claude/", "~/.gemini/"],
            "spawn_policy": "shell_false",
            "claude_denied_tools_when_unauthorized": ["Bash", "WebFetch", "WebSearch"],
        },
        "contaminated": {
            "forbidden_env_vars": ["OPENAI_API_KEY"],
            "credential_directories": ["~/.codex/"],
            "spawn_policy": "shell_true",
            "claude_denied_tools_when_unauthorized": ["execute_bash"],
        },
    },
    {
        "slug": "security-kiro-trust-tools",
        "role": "security_reviewer",
        "family": "family_kiro",
        "title": "Revisão das ferramentas confiáveis do Kiro",
        "statement": (
            "Você atua como security_reviewer na família Kiro e revisa a linha de comando do Kiro CLI montada "
            "por um adaptador. Responda só com o JSON do contrato de saída: `prohibited_flag` (flag que nunca "
            "pode ser usada), `read_only_trust_tools` (argumento exato para uma tarefa somente leitura), "
            "`execute_bash_precondition` (quando `execute_bash` pode entrar na lista) e "
            "`execute_bash_grant_scope` (alcance da concessão de `execute_bash`)."
        ),
        "krs": [
            kr("KR1", "Ferramentas confiáveis do Kiro", ["permissoes"], ["family-kiro-trust-tools"]),
            kr("KR2", "Checklist de revisão de segurança", ["revisao"], ["role-security-reviewer", "family-kiro-security"]),
        ],
        "properties": {
            "prohibited_flag": TXT,
            "read_only_trust_tools": TXT,
            "execute_bash_precondition": enum("command_policy_authorizes", "always_allowed", "never_allowed"),
            "execute_bash_grant_scope": enum("broad_not_prefix_restricted", "prefix_restricted"),
        },
        "checks": [
            equals("CHK-prohibited", "/prohibited_flag", "--trust-all-tools"),
            equals("CHK-read-only", "/read_only_trust_tools", "--trust-tools=fs_read"),
            equals("CHK-precondition", "/execute_bash_precondition", "command_policy_authorizes"),
            equals("CHK-scope", "/execute_bash_grant_scope", "broad_not_prefix_restricted"),
            forbidden(["--dangerously-skip-permissions", "--disallowedTools", "-s read-only", "--mode plan"]),
        ],
        "reference": {
            "prohibited_flag": "--trust-all-tools",
            "read_only_trust_tools": "--trust-tools=fs_read",
            "execute_bash_precondition": "command_policy_authorizes",
            "execute_bash_grant_scope": "broad_not_prefix_restricted",
        },
        "contaminated": {
            "prohibited_flag": "--dangerously-skip-permissions",
            "read_only_trust_tools": "--mode plan",
            "execute_bash_precondition": "always_allowed",
            "execute_bash_grant_scope": "prefix_restricted",
        },
    },
]
