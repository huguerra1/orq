"""CLI sintética de teste, sem rede; recebe o argv oficial depois do nome da CLI."""
import json
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
Path("invocation.json").write_text(json.dumps({"argv": args, "stdin": sys.stdin.read(), "cwd": str(Path.cwd())}), encoding="utf-8")
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
if mode == "login":
    print("Could not load your login: dispatch failure (io error): an i/o error occurred", file=sys.stderr)
    sys.exit(1)
if mode == "empty":
    print("no output produced", file=sys.stderr)
    sys.exit(0)
if mode == "noise_only":
    print("logging before google.Init", file=sys.stderr)
    print("[tool] read_file: usage limit")
    sys.exit(0)
final = 'ORQ_FINAL:{"outputs":{"report":{"ok":true}}}'
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
