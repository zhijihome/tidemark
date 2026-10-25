#!/usr/bin/env bash
# tidemark installer: merges hooks into a project's .claude/settings.json
# (never replaces existing entries) and adds the recall MCP to .mcp.json.
#
#   ./install.sh [project-dir]     # default: current directory
#
# Env knobs (written into the hook commands):
#   TIDEMARK_HOME             library dir            (default ~/.tidemark)
#   TIDEMARK_WATERLINE_TOKENS warn threshold         (default 400000)
#   TIDEMARK_LANG             zh for Chinese output  (default en)
set -euo pipefail

PROJECT_DIR="$(cd "${1:-.}" && pwd)"
TIDEMARK_DIR="$(cd "$(dirname "$0")/tidemark" && pwd)"
SETTINGS="$PROJECT_DIR/.claude/settings.json"
MCP="$PROJECT_DIR/.mcp.json"

mkdir -p "$PROJECT_DIR/.claude"

python3 - "$SETTINGS" "$MCP" "$TIDEMARK_DIR" <<'PY'
import json, sys, pathlib, shutil

settings_p, mcp_p, pkg = map(pathlib.Path, sys.argv[1:4])

s = json.loads(settings_p.read_text()) if settings_p.is_file() else {}
if settings_p.is_file():
    shutil.copy2(settings_p, str(settings_p) + ".bak-tidemark")

hooks = s.setdefault("hooks", {})

def add(event, entry):
    group = hooks.setdefault(event, [])
    if not any(json.dumps(e, sort_keys=True) == json.dumps(entry, sort_keys=True)
               for e in group):
        group.append(entry)

add("PreCompact", {"matcher": "auto|manual", "hooks": [
    {"type": "command", "command": 'python3 "{0}/handoff.py"'.format(pkg), "timeout": 20}]})
add("SessionEnd", {"matcher": "clear", "hooks": [
    {"type": "command", "command": 'python3 "{0}/handoff.py"'.format(pkg), "timeout": 20}]})
add("SessionStart", {"matcher": "startup|compact|clear", "hooks": [
    {"type": "command", "command": 'python3 "{0}/inject.py"'.format(pkg), "timeout": 10}]})
add("UserPromptSubmit", {"hooks": [
    {"type": "command", "command": 'python3 "{0}/waterline.py"'.format(pkg), "timeout": 5}]})

# Optional: raise the auto-compact ceiling toward the real window (1M-class
# models only — comment out or lower for 200k models).
s.setdefault("autoCompactWindow", "600k")

settings_p.write_text(json.dumps(s, ensure_ascii=False, indent=2) + "\n")

m = json.loads(mcp_p.read_text()) if mcp_p.is_file() else {}
m.setdefault("mcpServers", {})["tidemark"] = {
    "command": "python3",
    "args": [str(pkg / "recall_mcp.py")]}
mcp_p.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n")

print("installed into {0}".format(settings_p.parent.parent))
print("  hooks: PreCompact, SessionEnd(clear), SessionStart, UserPromptSubmit")
print("  mcp:   tidemark (memory_recall / memory_stats)")
PY

echo "restart your Claude Code session (or /clear) to activate."
