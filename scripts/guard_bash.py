"""PreToolUse 守卫：拦截一切经 Bash/Grep 触碰受保护面的操作。
契约文件允许 Read（模型需要读它们写代码），守卫脚本与本配置不允许 Read。"""
import json, os, sys

d = json.load(sys.stdin)
tool = d.get("tool_name", "")
ti = d.get("tool_input") or {}
text = " ".join(str(v) for v in ti.values())

PROT = [
    "maos/contracts/events.py", "maos/contracts/states.py",
    ".contracts.lock", "relock_contracts", "guard_bash",
    ".claude/settings.json", "MAOS_RELOCK",
]
READ_OK = {"maos/contracts/events.py", "maos/contracts/states.py"}

if os.environ.get("MAOS_RELOCK") == "1":
    sys.exit(0)

hit = next((p for p in PROT if p in text), None)
if hit is None:
    sys.exit(0)
if tool == "Read" and hit in READ_OK:
    sys.exit(0)

print("blocked: 该操作触碰受保护文件。停止当前工作并向人类报告。", file=sys.stderr)
sys.exit(2)
