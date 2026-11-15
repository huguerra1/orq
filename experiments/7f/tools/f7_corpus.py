"""Corpus do experimento 7F: definição das fontes Markdown (uma fonte = uma unidade).

Toda fonte tem exatamente um título H1, o que gera uma única unidade no Vault.
`synthetic=True` marca regras FICTÍCIAS usadas só como distrator; elas não
descrevem nenhuma CLI real nem regra real do ORQ.
Os fatos reais vêm de docs/phase-7/subscription-adapters.md, docs/phase-6/executor-choice.md,
docs/phase-7/quota-aware-routing.md, docs/phase-7/family-memory.md, docs/phase-7/experiment.md
e do código/schemas do repositório.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

NOTE_SYNTHETIC = (
    "_Unidade sintética fictícia do experimento 7F, usada apenas como distrator. "
    "Não descreve nenhuma CLI real nem regra real do ORQ._"
)


@dataclass(frozen=True)
class Source:
    source_id: str
    group: str  # common | role | family | family_role | synthetic
    title: str
    topics: tuple[str, ...]
    body: str
    families: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    synthetic: bool = False

    @property
    def path(self) -> str:
        return f"content/{self.group}/{self.source_id}.md"

    @property
    def audience(self) -> dict:
        area = "family" if self.families or self.roles else "common"
        return {
            "schema_version": "0.1",
            "area_scope": area,
            "target_families": list(self.families),
            "target_roles": list(self.roles),
        }

    def markdown(self) -> str:
        body = self.body.strip("\n")
        if self.synthetic:
            body += "\n\n" + NOTE_SYNTHETIC
        topics = ", ".join(self.topics)
        return (
            f"---\ntitle: {json.dumps(self.title, ensure_ascii=False)}\nlanguage: pt-BR\n"
            f"topics: [{topics}]\n---\n# {self.title}\n\n{body}\n"
        )


COMMON = [
    Source(
        "common-jcs-digest", "common", "Canonicalização JCS e digests",
        ("jcs", "digest", "canonicalizacao"),
        """Todo contrato JSON do ORQ é canonicalizado conforme a RFC 8785 (JCS) antes de gerar digest.

- O digest é o objeto `{"algorithm": "sha256", "value": "<64 hexadecimais minúsculos>"}`; o `value` casa com `^[a-f0-9]{64}$`.
- O SHA-256 é calculado sobre os bytes JCS (UTF-8) do valor.
- Inteiros fora da faixa segura IEEE 754, de -9007199254740991 a 9007199254740991, são rejeitados (`integer_outside_ieee754_safe_range`).
- Números não finitos (`NaN`, `Infinity`) e o zero negativo são rejeitados.
- Objetos com chave não textual são rejeitados.""",
    ),
    Source(
        "common-identifiers", "common", "Identificadores e famílias do ORQ",
        ("identificador", "schema", "familia"),
        """Identificadores (`identifier`) casam com `^[A-Za-z][A-Za-z0-9._:-]*$`: de 1 a 128 caracteres, começando por letra.

O enum `familyId` aceita `family_claude`, `family_codex`, `family_gemini`, `family_kiro` e `family_simulated`.""",
    ),
    Source(
        "common-content-contract", "common", "Contrato de conteúdo de saída",
        ("contrato-de-conteudo", "digest", "avaliacao"),
        """Cada saída esperada aponta para um contrato de conteúdo por `{id, version, content_digest}`.

- O `content_digest` é o digest JCS do schema JSON (Draft 2020-12) do contrato.
- O registro local de contratos recusa um schema cujo digest difere da referência (`content_contract_digest_mismatch`).
- Uma referência desconhecida nunca é aprovada por heurística, e `format` é apenas anotação.""",
    ),
    Source(
        "common-evaluator-methods", "common", "Métodos do avaliador determinístico",
        ("avaliacao", "metodos-de-avaliacao"),
        """O avaliador determinístico conhece quatro métodos: `output_binding_present`, `digest_matches`, `content_contract_valid` e `simulated_verdict`.

- Um método desconhecido não é executado: o critério fica `not_run`.
- `simulated_verdict` depende do roteiro do executor simulado.
- A métrica primária nunca usa LLM como juiz.""",
    ),
    Source(
        "common-subscription-policy", "common", "Política de uso das assinaturas",
        ("politica-de-assinatura", "seguranca", "assinaturas"),
        """O hub de assinaturas opera sob quatro restrições inegociáveis:

1. Só as CLIs oficiais (`claude`, `codex`, `agy`, `kiro-cli`), em modo não interativo, com stdin fechado.
2. Nunca ler, copiar ou retransmitir tokens ou credenciais das CLIs, como os diretórios `~/.codex/`, `~/.claude/` e `~/.gemini/`.
3. Nunca automatizar interface web (Playwright, Selenium, Puppeteer, scraping).
4. Sem API paga e sem LLM local: não injetar `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` nem `GEMINI_API_KEY`.""",
    ),
    Source(
        "common-cost-honesty", "common", "Custo desconhecido nunca vira zero",
        ("custo", "uso", "assinaturas"),
        """Em assinaturas, o `UsageRecord` mantém `cost.status = "unavailable"` e `cost.amount = null`; o valor nunca é substituído por `"0"`.

O campo `total_cost_usd` do envelope JSON do Claude Code é uma equivalência de API calculada pela CLI, não uma cobrança real. Ele pode ser guardado só como anotação informativa do adaptador.""",
    ),
    Source(
        "common-audience-rules", "common", "Regras de audiência da memória",
        ("audiencia", "familia", "memoria"),
        """Cada unidade de conhecimento tem uma audiência com `area_scope`:

- `common`: `target_families` e `target_roles` vazios; visível a todas as famílias.
- `family`: pelo menos uma das listas é não vazia; com as duas preenchidas, vale a interseção (família listada e papel listado).
- `area_scope`, `target_families` e `target_roles` são atribuídos pelo catálogo, nunca pelo frontmatter. O frontmatter aceita só `title`, `language`, `topics` e `relationships`.""",
    ),
    Source(
        "common-rls-scope", "common", "Escopo de leitura e Row-Level Security",
        ("rls", "escopo", "memoria"),
        """A leitura sob RLS exige quatro variáveis fixadas pelo host em transação `READ ONLY`: `app.catalog_id`, `app.current_project_id`, `app.current_family_id` e `app.current_role_id`.

- Os valores vêm do `RunManifest` e da `RoutingDecision`, nunca dos argumentos do agente.
- Escopo ausente ou incompleto não lê nada.
- As roles de leitura não têm `SUPERUSER` nem `BYPASSRLS`.""",
    ),
    Source(
        "common-output-protocol", "common", "Protocolo de saída final",
        ("saida-estruturada", "protocolo-de-saida", "orq-final"),
        """A resposta final da CLI carrega a linha `ORQ_FINAL:{"outputs":{"<output_id>":<JSON>}}`.

- Alternativa: o marcador com `outputs` vazio e arquivos `.orq-output/<output_id>.json` no workspace.
- Todos os contratos de conteúdo são validados antes de publicar bytes no `ArtifactStore`.""",
    ),
    Source(
        "common-quota-cooldown", "common", "Cooldown de cota e fallback",
        ("cota", "cooldown", "roteamento"),
        """Quando a mensagem de limite não traz horário de reset, `reset_at` fica `null` e vale `blocked_until = observed_at + default_cooldown_ms`.

- O padrão é de 60 minutos: `default_cooldown_ms = 3600000`.
- O registro leva `reset_source: "cooldown_policy"`; as demais origens são `cli_reported` e `clock_expired`.
- Depois de um cooldown por política, o destino só recebe tarefa real após uma sondagem leve.
- Se todas as famílias elegíveis estão bloqueadas, o Router devolve `decision_status: "no_eligible_target"` com o motivo `all_eligible_families_quota_exhausted`.
- Texto livre de stdout não é fonte de cota.""",
    ),
    Source(
        "common-context-budget", "common", "Orçamento de contexto",
        ("orcamento-de-contexto", "tokens"),
        """Os tokens do contexto são estimados por `char4-v1`: `ceil(caracteres / 4)`.

- Cobertura obrigatória não atendida gera `mandatory_requirement_uncovered`; estouro de orçamento gera `context_budget_exceeded`.
- A regra de cobertura é `at_least_one` ou `all_source_refs`.
- Não há truncamento silencioso.""",
    ),
    Source(
        "common-host-process-rules", "common", "Regras de processo do host",
        ("processo", "timeout", "execucao"),
        """O host lança cada CLI sem shell (`shell=False`, `argv` explícito), com stdin fechado e em workspace novo por tentativa.

- O timeout é imposto pelo host (`attempt_timeout_ms`): nenhuma CLI observada encerra sozinha uma tarefa longa.
- Cancelamento e timeout encerram toda a árvore de processos antes de qualquer retry.""",
    ),
]

ROLES = [
    Source(
        "role-software-architect", "role", "Guia do papel software_architect",
        ("arquitetura", "unidade-de-comparacao", "cobertura-de-contexto"),
        """A unidade de comparação declarada no experimento é a família/runtime completo (CLI + modelo + ferramentas).

- Registre `executor_ref`, `runtime_version`, `requested_model_id` e `resolved_model_id`.
- `provider_context_coverage` nunca é `complete`: o máximo honesto é `platform_only` (arquivos locais de instrução sob controle do workspace limpo); sem isso, `unknown`.""",
        roles=("software_architect",),
    ),
    Source(
        "role-backend-engineer", "role", "Guia do papel backend_engineer",
        ("adaptador", "reconciliacao", "digest"),
        """Os adaptadores implementam `describe`, `dispatch`, `observe`, `cancel`, `reconcile` e `collect`.

- O registro durável do processo, com `state_hash` JCS, é gravado antes do spawn.
- `not_found` autoritativo só existe com estado durável que prove que a CLI nunca iniciou; chave desconhecida é `unknown` e nunca autoriza redespacho.""",
        roles=("backend_engineer",),
    ),
    Source(
        "role-tester", "role", "Guia do papel tester",
        ("testes", "avaliacao", "evidencia"),
        """Os testes dos adaptadores usam CLIs falsas (scripts Python sintéticos) e não provam o comportamento das CLIs reais; o smoke V01-V12 continua pendente.

- Não confunda casos de aceitação documentados com testes executados.
- A avaliação é determinística: sem LLM como juiz.""",
        roles=("tester",),
    ),
    Source(
        "role-security-reviewer", "role", "Guia do papel security_reviewer",
        ("revisao", "seguranca", "checklist"),
        """Checklist de revisão de segurança do hub de assinaturas:

- `--dangerously-skip-permissions` (agy) e `--trust-all-tools` (Kiro) nunca são usadas.
- Nenhum adaptador declara `provider_context_coverage: "complete"`.
- A rede é negada fora da política (`WebFetch` e `WebSearch` negados no Claude Code).
- `execute_bash` no Kiro é uma concessão ampla.""",
        roles=("security_reviewer",),
    ),
]

FAMILIES = [
    Source(
        "family-claude-invocation", "family", "Claude Code: invocação não interativa",
        ("invocacao", "claude", "ferramentas", "permissoes", "saida-estruturada", "diretorio-de-trabalho", "modelo"),
        """```text
claude -p "<prompt_envelope>" --output-format json --model <native_model_id> --permission-mode <mode> --allowedTools <lista_fechada> --disallowedTools <lista_negada>
```

- Roda com `cwd=workspace_dir` e stdin fechado.
- A `ExecutionPolicy` vira `--allowedTools` (por exemplo `Read`, `Edit` e `Write` restritos ao workspace) e `--disallowedTools`.
- Quando a tarefa não os autoriza, `--disallowedTools` nega as ferramentas de rede (`WebFetch`, `WebSearch`) e o shell (`Bash`).
- O envelope JSON traz `result`, `session_id` e `usage`, e às vezes `total_cost_usd`.""",
        families=("family_claude",),
    ),
    Source(
        "family-codex-invocation", "family", "Codex CLI: invocação não interativa",
        ("invocacao", "codex", "saida-estruturada", "diretorio-de-trabalho", "modelo", "sandbox", "permissoes"),
        """```text
codex exec -C <workspace_dir> -s <workspace-write|read-only> -m <native_model_id> -c model_reasoning_effort="<effort>" --json -o <last_message_file> "<prompt_envelope>"
```

- `--json` emite eventos JSONL no stdout; `-o` grava a mensagem final em arquivo.
- Quando o workspace da tentativa não é um repositório Git completo, acrescenta `--skip-git-repo-check`.
- O modelo e o esforço padrão vêm de `~/.codex/config.toml` quando não informados.""",
        families=("family_codex",),
    ),
    Source(
        "family-codex-readonly", "family_role", "Codex CLI: modo somente leitura",
        ("somente-leitura", "sandbox", "revisao", "permissoes"),
        """Use `-s read-only` em planejamento, pesquisa e revisão (`code_reviewer`, `security_reviewer`, `researcher` e `software_architect` sem escrita).

`-s workspace-write` só é usado quando a `ExecutionPolicy` autoriza escrita no workspace.""",
        families=("family_codex",),
        roles=("software_architect", "security_reviewer", "code_reviewer"),
    ),
    Source(
        "family-codex-windows-tests", "family_role", "Codex CLI: testes no sandbox do Windows",
        ("testes", "execucao-de-testes", "sandbox", "windows"),
        """Observado no Codex CLI 0.167.0 em Windows 10: no sandbox `workspace-write`, runners de teste com vários processos (vitest, jest) falharam ao criar processos filhos com `spawn EPERM`.

- Funcionaram com `--pool threads` ou `--runInBand`.
- Instrução operacional da família: execute os testes em thread única; no Python, `pytest` sequencial.""",
        families=("family_codex",),
        roles=("backend_engineer", "tester"),
    ),
    Source(
        "family-gemini-invocation", "family", "agy (Gemini): invocação não interativa",
        ("invocacao", "gemini", "diretorio-de-trabalho", "modelo", "timeout", "saida-estruturada"),
        """```text
agy -p "<prompt_envelope>" --model <native_model_id> --mode <accept-edits|plan> --output-format text --print-timeout <timeout_s>
```

- O `agy` não tem flag de diretório de trabalho (`-C`): o adaptador define `cwd=workspace_dir` no subprocesso.
- `--print-timeout` recebe o timeout em segundos (`<timeout_s>`).
- Os modelos são listados por `agy models`.""",
        families=("family_gemini",),
    ),
    Source(
        "family-gemini-readonly", "family_role", "agy (Gemini): modo somente leitura",
        ("somente-leitura", "revisao", "permissoes"),
        """Tarefas somente leitura usam `--mode plan`; escrita no workspace usa `--mode accept-edits`.

A flag `--dangerously-skip-permissions` nunca é utilizada pelo ORQ.""",
        families=("family_gemini",),
        roles=("software_architect", "security_reviewer", "code_reviewer"),
    ),
    Source(
        "family-gemini-permissions", "family", "agy (Gemini): permissões em modo headless",
        ("permissoes", "headless", "ferramentas", "saida-vazia"),
        """Em modo headless (`-p`), qualquer ferramenta sem regra explícita de permissão é negada automaticamente: a execução termina sem saída útil e com a mensagem `no output produced`.

- As regras ficam em `permissions.allow` do `settings.json` do `agy`, por exemplo `read_file(<caminho>)` e `command(<prefixo>)`.
- O adaptador grava as regras no `.gemini/antigravity-cli/settings.json` do workspace e as remove ao final.
- Saída vazia por permissão vira `ErrorRecord` com código `permission_or_empty_output` (`failed`), nunca `completed`.""",
        families=("family_gemini",),
    ),
    Source(
        "family-gemini-noise", "family", "agy (Gemini): ruído de saída",
        ("ruido", "stderr", "saida"),
        """O stderr do `agy` emite a linha de inicialização `logging before google.Init`, que o parser filtra antes de classificar erros.

O artefato de saída deve ser gravado em arquivo determinístico no workspace ou delimitado por marcador canônico validado contra o `OutputSpec`.""",
        families=("family_gemini",),
    ),
    Source(
        "family-kiro-invocation", "family", "Kiro CLI: invocação não interativa",
        ("invocacao", "kiro", "wsl", "diretorio-de-trabalho", "modelo", "permissoes"),
        """```text
wsl.exe --cd <workspace_dir> -- kiro-cli chat --no-interactive --trust-workspace --trust-tools=<lista_fechada> --model <native_model_id> "<prompt_envelope>"
```

- No Linux nativo, executa `kiro-cli` direto; no Windows, usa `wsl.exe` sem shell interativo.
- Os workspaces ficam em `C:\\dev\\...`, vistos no WSL como `/mnt/c/...`.
- O login observado usa o IAM Identity Center, sem API key.""",
        families=("family_kiro",),
    ),
    Source(
        "family-kiro-trust-tools", "family", "Kiro CLI: ferramentas confiáveis",
        ("permissoes", "ferramentas", "trust-tools", "somente-leitura", "revisao"),
        """A flag `--trust-all-tools` é proibida: viola a negação por padrão da `ExecutionPolicy`. O adaptador monta `--trust-tools` só com o que a política autoriza:

- somente leitura: `--trust-tools=fs_read`;
- leitura e escrita sem comandos: `--trust-tools=fs_read,fs_write`;
- `execute_bash` só entra na lista quando a `CommandPolicy` autoriza explicitamente na tarefa. É uma concessão ampla: a flag não prova restrição por prefixo de comando.""",
        families=("family_kiro",),
    ),
    Source(
        "family-kiro-output", "family", "Kiro CLI: saída e créditos",
        ("saida", "creditos", "custo", "ruido"),
        """O stdout do Kiro traz linhas de progresso `[tool] ...` antes da resposta final; o parser separa a trilha de ferramentas da resposta.

- `kiro-cli chat --list-models` mostra o multiplicador de créditos de cada modelo, registrado no `QuotaConsumptionRecord`.
- O custo monetário no `UsageRecord` permanece `unavailable` com `amount: null`.""",
        families=("family_kiro",),
    ),
    Source(
        "family-claude-quota", "family", "Claude Code: detecção de limite de uso",
        ("cota", "deteccao-de-limite"),
        """O detector do Claude Code reconhece padrões de limite de sessão ou janela: `rate limit`, `usage limit`, `limit reached` e `resets at <horário>`.

A detecção só vale quando a invocação falha e o padrão aparece no canal de erro/status da CLI, nunca em conteúdo de arquivos lidos pelo agente.""",
        families=("family_claude",),
    ),
    Source(
        "family-codex-quota", "family", "Codex CLI: detecção de limite de uso",
        ("cota", "deteccao-de-limite"),
        """Mensagem real observada: `You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/plus) or try again at 10:05 PM.` Também ocorre o formato de 24 horas, `try again at 22:05`.

O detector procura `usage limit` e, opcionalmente, `try again at <HH:MM> [AM|PM]` ou data/hora explícita; o horário é resolvido no fuso do host, passando para o dia seguinte quando já passou.""",
        families=("family_codex",),
    ),
    Source(
        "family-gemini-quota", "family", "agy (Gemini): detecção de limite de uso",
        ("cota", "deteccao-de-limite"),
        """O detector do `agy` reconhece mensagens de exaustão de quota: `RESOURCE_EXHAUSTED`, `quota exceeded` e `usage limit`.

A detecção só vale quando a invocação falha e o padrão aparece no canal de erro/status da CLI.""",
        families=("family_gemini",),
    ),
    Source(
        "family-kiro-quota", "family", "Kiro CLI: detecção de limite de uso",
        ("cota", "deteccao-de-limite"),
        """O detector do Kiro reconhece mensagens de limite de créditos ou uso da assinatura: `usage limit`, `credit limit` e `monthly limit`.

A detecção só vale quando a invocação falha e o padrão aparece no canal de erro/status da CLI.""",
        families=("family_kiro",),
    ),
    Source(
        "family-claude-security", "family_role", "Claude Code: revisão de segurança",
        ("revisao", "seguranca", "claude", "custo", "ferramentas"),
        """Na revisão de segurança com o Claude Code, confirme que `--disallowedTools` inclui `WebFetch`, `WebSearch` e `Bash` quando a tarefa não os autoriza.

Trate `total_cost_usd` como informação da CLI, nunca como cobrança.""",
        families=("family_claude",),
        roles=("security_reviewer",),
    ),
    Source(
        "family-gemini-security", "family_role", "agy (Gemini): revisão de segurança",
        ("revisao", "seguranca", "gemini", "permissoes"),
        """Na revisão de segurança com o `agy`, confirme que `--dangerously-skip-permissions` não aparece e que `permissions.allow` restringe `read_file` e `command` ao workspace da tentativa.""",
        families=("family_gemini",),
        roles=("security_reviewer",),
    ),
    Source(
        "family-kiro-security", "family_role", "Kiro CLI: revisão de segurança",
        ("revisao", "seguranca", "kiro", "permissoes"),
        """Na revisão de segurança com o Kiro, recuse `--trust-all-tools` e confirme que `execute_bash` só aparece em `--trust-tools` quando a `CommandPolicy` da tarefa o autoriza, lembrando que a concessão é ampla.""",
        families=("family_kiro",),
        roles=("security_reviewer",),
    ),
]

SYNTHETIC = [
    Source(
        "syn-codex-cooldown", "synthetic", "Cooldown padrão de cota (regra fictícia)",
        ("cota", "cooldown", "roteamento", "sintetico"),
        "Quando a CLI não informa horário de reset, o cooldown padrão é de 30 minutos (`default_cooldown_ms = 1800000`) e a origem registrada é `clock_expired`.",
        families=("family_codex",), synthetic=True,
    ),
    Source(
        "syn-kiro-identifiers", "synthetic", "Formato de identificadores (regra fictícia)",
        ("identificador", "schema", "sintetico"),
        "Identificadores devem começar com dígito e ter no máximo 64 caracteres; o prefixo `family_` é opcional.",
        families=("family_kiro",), synthetic=True,
    ),
    Source(
        "syn-gemini-digest", "synthetic", "Digest de contratos (regra fictícia)",
        ("jcs", "digest", "canonicalizacao", "sintetico"),
        "O digest é SHA-512 em base64 sobre o JSON com chaves na ordem de inserção; o zero negativo é aceito e números acima de 2147483647 são rejeitados.",
        families=("family_gemini",), synthetic=True,
    ),
    Source(
        "syn-claude-marker", "synthetic", "Marcador de saída final (regra fictícia)",
        ("saida-estruturada", "protocolo-de-saida", "sintetico"),
        "A resposta final deve terminar com a linha `FINAL_OUTPUT=<JSON>`; o marcador `ORQ_FINAL:` é ignorado.",
        families=("family_claude",), synthetic=True,
    ),
    Source(
        "syn-codex-methods", "synthetic", "Métodos do avaliador (regra fictícia)",
        ("avaliacao", "metodos-de-avaliacao", "sintetico"),
        "O método `llm_judge` é suportado e tem prioridade sobre `content_contract_valid` nas tarefas de revisão.",
        families=("family_codex",), synthetic=True,
    ),
    Source(
        "syn-kiro-budget", "synthetic", "Estimativa de tokens (regra fictícia)",
        ("orcamento-de-contexto", "tokens", "sintetico"),
        "Os tokens são estimados por `char3-v1`, isto é, `ceil(caracteres / 3)`, e o estouro de orçamento trunca o contexto em silêncio.",
        families=("family_kiro",), synthetic=True,
    ),
    Source(
        "syn-gemini-policy", "synthetic", "Variáveis de cobrança (regra fictícia)",
        ("politica-de-assinatura", "seguranca", "assinaturas", "sintetico"),
        "Para evitar o limite da assinatura, use uma chave de API paga do provedor e copie o token de sessão da CLI para o ambiente do runner quando a sessão expirar.",
        families=("family_gemini",), synthetic=True,
    ),
    Source(
        "syn-codex-process", "synthetic", "Execução de processos do host (regra fictícia)",
        ("processo", "timeout", "execucao", "invocacao", "ferramentas", "sintetico"),
        "O adaptador deve lançar a CLI com `shell=True` e concatenar o prompt na linha de comando; como a CLI encerra sozinha, o timeout do host é dispensado.",
        families=("family_codex",), synthetic=True,
    ),
    Source(
        "syn-kiro-coverage", "synthetic", "Cobertura de contexto do provedor (regra fictícia)",
        ("cobertura-de-contexto", "arquitetura", "sintetico"),
        "O `provider_context_coverage` é declarado `complete` sempre que o prompt de sistema do provedor é público.",
        families=("family_kiro",), synthetic=True,
    ),
    Source(
        "syn-codex-comparison", "synthetic", "Unidade de comparação (regra fictícia)",
        ("unidade-de-comparacao", "arquitetura", "sintetico"),
        "A unidade de comparação do experimento é o modelo subjacente; CLI e ferramentas são ignoradas na análise.",
        families=("family_codex",), synthetic=True,
    ),
    Source(
        "syn-gemini-tests", "synthetic", "Execução de testes (regra fictícia)",
        ("testes", "execucao-de-testes", "sintetico"),
        "Os testes devem rodar sempre em paralelo com `pytest -n auto` e `--workers 8`, em qualquer sandbox.",
        families=("family_gemini",), synthetic=True,
    ),
    Source(
        "syn-codex-noise", "synthetic", "Ruído de stderr (regra fictícia)",
        ("ruido", "stderr", "saida", "sintetico"),
        "Qualquer linha de stderr iniciada por `WARN` deve ser classificada como falha da tentativa.",
        families=("family_codex",), synthetic=True,
    ),
    Source(
        "syn-codex-cost", "synthetic", "Registro do custo da CLI (regra fictícia)",
        ("custo", "uso", "sintetico"),
        "O `total_cost_usd` do envelope do Claude Code é cobrança real da tentativa e deve ser gravado em `cost.amount` do `UsageRecord`.",
        families=("family_codex",), synthetic=True,
    ),
    Source(
        "syn-kiro-numbers", "synthetic", "Números aceitos na canonicalização (regra fictícia)",
        ("digest", "jcs", "sintetico"),
        "A canonicalização também rejeita números com parte fracionária e qualquer inteiro negativo, além dos casos de borda comuns.",
        families=("family_kiro",), synthetic=True,
    ),
    Source(
        "syn-claude-stdout", "synthetic", "Detecção de limite no stdout (regra fictícia)",
        ("cota", "deteccao-de-limite", "sintetico"),
        "O detector deve procurar `usage limit` em todo o texto livre do stdout, inclusive no conteúdo de arquivos lidos pelo agente.",
        families=("family_claude",), synthetic=True,
    ),
    Source(
        "syn-claude-permissions", "synthetic", "Permissões em revisões (regra fictícia)",
        ("permissoes", "sintetico"),
        "Em revisão de segurança, conceder confiança a todas as ferramentas da CLI é aceitável quando o workspace é descartável.",
        families=("family_claude",), synthetic=True,
    ),
]

SOURCES: list[Source] = COMMON + ROLES + FAMILIES + SYNTHETIC
BY_ID: dict[str, Source] = {source.source_id: source for source in SOURCES}
assert len(BY_ID) == len(SOURCES)
