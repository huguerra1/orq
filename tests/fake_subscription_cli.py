"""CLI sintética de teste, sem rede; recebe o argv oficial depois do nome da CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


if sys.argv[1] == "child":
    target = Path(sys.argv[2])
    while True:
        target.write_text(str(time.time()), encoding="utf-8")
        time.sleep(.03)

cli, mode, *args = sys.argv[1:]
invocation_path = Path("invocation.json")
while args and args[0].startswith("--fake-"):
    opt = args.pop(0)
    if opt.startswith("--fake-invocation-out="):
        invocation_path = Path(opt.split("=", 1)[1])
if not mode.startswith("judge_") or invocation_path != Path("invocation.json"):
    invocation_path.write_text(
        json.dumps({"argv": args, "stdin": sys.stdin.read(), "cwd": str(Path.cwd()),
                    "environment_names": sorted(os.environ)}),
        encoding="utf-8",
    )
else:
    sys.stdin.read()

if mode.startswith("judge_"):
    if mode == "judge_hang":
        child = subprocess.Popen([sys.executable, __file__, "child", str(invocation_path.parent / "heartbeat")], stdin=subprocess.DEVNULL)
        (invocation_path.parent / "child.pid").write_text(str(child.pid), encoding="utf-8")
        while True:
            time.sleep(.05)
    if mode == "judge_quota":
        message = {
            "codex": "You’ve hit your usage limit. Try again at 10:05 PM.",
            "claude": "Rate limit reached.",
            "agy": "RESOURCE_EXHAUSTED: quota exceeded",
            "kiro-cli": "Monthly limit exceeded.",
        }[cli]
        print("ERROR: " + message, file=sys.stderr)
        sys.exit(1)
    if mode == "judge_mutate":
        Path("unauthorized-edit.txt").write_text("mutated by rogue judge\n", encoding="utf-8")
    if mode == "judge_escape":
        if cli == "claude":
            print(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/home/user/.claude/credentials.json"}}
            ]}}))
        elif cli == "codex":
            print(json.dumps({"type": "item.completed", "item": {
                "type": "file_read", "path": "/home/user/.codex/auth.json"
            }}))
        else:
            print("[tool] read_file ../journal.jsonl")
    input_files = sorted((Path.cwd() / "inputs").glob("*")) if (Path.cwd() / "inputs").is_dir() else []
    raw_bytes = input_files[0].read_bytes() if input_files else b""
    import hashlib
    artifact_digest = {"algorithm": "sha256", "value": hashlib.sha256(raw_bytes).hexdigest()}
    first_line = raw_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n").splitlines()[0] if raw_bytes else "line"
    output_id = input_files[0].stem if input_files else "security_report"
    artifact_id = f"artifact-{output_id}"
    if mode == "judge_invalid_citation":
        excerpt = "TEXTO_INEXISTENTE_CITADO_PELO_JUIZ"
        s1, s2 = 4, 4
    elif mode == "judge_blocking_fail":
        excerpt = first_line[:120]
        s1, s2 = 2, 4
    else:
        excerpt = first_line[:120]
        s1, s2 = 4, 3
    if mode == "judge_invalid_output":
        final = "Parecer textual livre sem marcador ORQ_FINAL estruturado."
    else:
        evidence = {
            "dimension_assessments": [
                {
                    "dimension_id": "auth-and-secrets-boundary",
                    "raw_score": s1,
                    "rationale": f"Avaliação objetiva da dimensão auth-and-secrets-boundary ({s1}).",
                    "citations": [{
                        "output_id": output_id,
                        "artifact_id": artifact_id,
                        "content_digest": artifact_digest,
                        "locator": {"kind": "line_range", "start_line": 1, "end_line": max(1, len(raw_bytes.splitlines()))},
                        "verbatim_excerpt": excerpt,
                    }],
                },
                {
                    "dimension_id": "contract-completeness",
                    "raw_score": s2,
                    "rationale": f"Avaliação objetiva da dimensão contract-completeness ({s2}).",
                    "citations": [{
                        "output_id": output_id,
                        "artifact_id": artifact_id,
                        "content_digest": artifact_digest,
                        "locator": {"kind": "line_range", "start_line": 1, "end_line": max(1, len(raw_bytes.splitlines()))},
                        "verbatim_excerpt": first_line[:120],
                    }],
                },
            ],
            "overall_summary": "Síntese técnica emitida pela CLI avaliadora em modo somente leitura.",
        }
        final = "ORQ_FINAL:" + json.dumps({"outputs": {"rubric_evidence": evidence}}, ensure_ascii=False, separators=(",", ":"))
    if cli == "claude":
        print(json.dumps({"type": "result", "subtype": "success", "result": final, "session_id": "judge-session-claude",
                          "model": "claude-opus-4", "usage": {"input_tokens": 120, "output_tokens": 45}, "total_cost_usd": 99.99}))
    elif cli == "codex":
        print(json.dumps({"type": "thread.started", "thread_id": "judge-session-codex"}))
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": final}}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 45}, "model": "gpt-5-codex"}))
    else:
        print("logging before google.Init", file=sys.stderr)
        print("[tool] read_file inputs/security_report.json")
        print(final)
    sys.exit(0)
if mode == "hang":
    child = subprocess.Popen([sys.executable, __file__, "child", str(Path.cwd() / "heartbeat")], stdin=subprocess.DEVNULL)
    Path("child.pid").write_text(str(child.pid), encoding="utf-8")
    while True:
        time.sleep(.05)
if mode == "quota":
    message = {"codex": "You’ve hit your usage limit. Try again at 10:05 PM.",
               "claude": "Rate limit reached.", "agy": "RESOURCE_EXHAUSTED: quota exceeded", "kiro-cli": "Monthly limit exceeded."}[cli]
    print("ERROR: " + message, file=sys.stderr)
    sys.exit(1)
if mode == "quota_absolute":
    # Formato real de 14/11/2026 (codex-quota-v2): data absoluta no fuso local.
    print("ERROR: You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
          "https://chatgpt.com/settings/usage to purchase more credits or try again at Nov 15th, 2026 2:00 AM.", file=sys.stderr)
    sys.exit(1)
if mode == "quota_no_reset":
    print("usage limit", file=sys.stderr)
    sys.exit(1)
if mode == "login":
    print("Could not load your login: dispatch failure (io error): an i/o error occurred", file=sys.stderr)
    sys.exit(1)
if mode == "network":
    print("network error: DNS unavailable", file=sys.stderr)
    sys.exit(1)
if mode in {"empty", "agy_permission"}:
    if mode == "agy_permission":
        print("logging before google.Init", file=sys.stderr)
    print("no output produced", file=sys.stderr)
    sys.exit(0)
if mode == "noise_only":
    print("logging before google.Init", file=sys.stderr)
    print("[tool] read_file: usage limit")
    sys.exit(0)
final = 'ORQ_FINAL:{"outputs":{"report":{"ok":true}}}'
if mode == "literal_success":
    if cli == "claude":
        print(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_result", "content": "You've hit your usage limit"}]}}))
    elif cli == "codex":
        print(json.dumps({"type": "item.completed", "item": {"type": "command_execution",
              "aggregated_output": "You've hit your usage limit"}}))
    else:
        print("[tool] read_file: You've hit your usage limit")
if mode == "bad_contract":
    final = 'ORQ_FINAL:{"outputs":{"report":{"ok":"yes"}}}'
if mode == "file":
    Path(".orq-output/report.json").write_text('{"ok":true}', encoding="utf-8")
    final = 'ORQ_FINAL:{"outputs":{}}'
if mode == "project_quota":
    print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "You've hit your usage limit"}}))
    sys.exit(1)
if mode == "json_quota":
    print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}))
    sys.exit(0)  # ausência de resposta final também aciona o detector
if mode in {"intermediate_quota", "final_then_quota", "intermediate_stderr_quota"}:
    text = final if mode == "final_then_quota" else "Vou conferir os arquivos."
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    Path(".orq-last-message").write_text(final, encoding="utf-8")
    if mode == "intermediate_stderr_quota":
        print("You've hit your usage limit", file=sys.stderr)
    else:
        print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}))
    sys.exit(0)
if mode == "project_json_error":
    print(json.dumps({"type": "error", "message": "usage limit reached"}))
    sys.exit(1)
if mode == "json_prefix_noise":
    print('["tool"] usage limit', file=sys.stderr)
    print('{"type":"error","message":"usage limit"} usage limit', file=sys.stderr)
    sys.exit(1)
if mode == "error_then_final":
    print(json.dumps({"type": "error", "message": "connection failed, retrying..."}))
if mode == "mcp_roundtrip":
    import asyncio
    from mcp import Client, StdioServerParameters

    if cli == "claude":
        cfg = json.loads(Path(".orq-mcp.json").read_text(encoding="utf-8"))["mcpServers"]["orq-knowledge"]
    elif cli == "agy":
        cfg = json.loads(Path(".gemini/antigravity-cli/settings.json").read_text(encoding="utf-8"))["mcpServers"]["orq-knowledge"]
    elif cli == "kiro-cli":
        cfg = json.loads(Path(".kiro/settings/mcp.json").read_text(encoding="utf-8"))["mcpServers"]["orq-knowledge"]
    else:
        cfg = {}
        for index, item in enumerate(args):
            if item == "-c" and index + 1 < len(args):
                kv = args[index + 1]
                if kv.startswith("mcp_servers.orq_knowledge.command="):
                    cfg["command"] = json.loads(kv.split("=", 1)[1])
                elif kv.startswith("mcp_servers.orq_knowledge.args="):
                    cfg["args"] = json.loads(kv.split("=", 1)[1])
                elif kv.startswith("mcp_servers.orq_knowledge.env={PYTHONPATH="):
                    cfg["env"] = {"PYTHONPATH": json.loads(kv.split("PYTHONPATH=", 1)[1][:-1])}

    async def _run_mcp():
        params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=cfg.get("env"))
        async with Client(params, read_timeout_seconds=15) as client:
            res = await client.call_tool("search_family_knowledge", {"requirement_id": "K1", "query": "reconciliação"})
            cands = json.loads(res.content[0].text)["candidates"]
            refs = [{k: c[k] for k in ("source_id", "revision", "unit_id")} for c in cands]
            sel = await client.call_tool("select_family_knowledge", {"selections": {"K1": refs}})
            data = json.loads(sel.content[0].text)
            assert "error" not in data, data

    asyncio.run(_run_mcp())
print("progresso sem JSON")
if cli == "claude":
    print(json.dumps({"type": "result", "subtype": "success", "result": final, "session_id": "session-claude",
                      "model": "resolved-model", "usage": {"input_tokens": 12, "output_tokens": 7}, "total_cost_usd": 99.99},
                     indent=2 if mode == "pretty" else None))
elif cli == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "session-codex"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": final}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 7}, "model": "resolved-model"}))
else:
    print("logging before google.Init", file=sys.stderr)
    print("[tool] read_file ...")
    print(final)
