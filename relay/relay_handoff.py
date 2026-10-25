#!/usr/bin/env python3
"""Handoff generator — fires on PreCompact and SessionEnd(clear).

Reads the hook stdin JSON (session_id / transcript_path / cwd / trigger|reason),
mechanically extracts what the window did — never calls a model — and writes
`handoff-auto-<ts>.md` plus ingests it into the relay store.

Collected:
- Scene: git branch/HEAD/dirty-count for every dir in `watch-dirs.conf`
  (one per line; defaults to the session cwd), plus service checks from
  `services.conf` (lines "name | url" → curl, or "launchd:<label>").
- This window: recent user messages, Write/Edit file paths, notable Bash
  commands — pulled from the session transcript.
- Open threads: `tail.md` cache maintained by relay_sync (or by hand).
- Sandbox pointer: latest context-mode session db for this project, so the
  next window can keep querying its artifacts.

Every failure exits 0 — a hook must never block compaction or clearing.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay_store  # noqa: E402

CONF_DIRS = "watch-dirs.conf"
CONF_SERVICES = "services.conf"
MAX_USER_LINES = 12
MAX_EDIT_FILES = 12
MAX_CMDS = 15

_ZH = os.environ.get("RELAY_LANG", "").lower().startswith("zh")


def _L(en, zh):
    return zh if _ZH else en


def handoff_dir(payload=None):
    """Where handoff files land: RELAY_HANDOFF_DIR > session cwd > RELAY_HOME/handoffs."""
    env = os.environ.get("RELAY_HANDOFF_DIR")
    if env:
        return Path(env)
    cwd = str((payload or {}).get("cwd") or "")
    return Path(cwd) if cwd else relay_store.default_root() / "handoffs"


def _cfg_dirs(root, cwd):
    p = Path(root) / CONF_DIRS
    if p.is_file():
        return [Path(l.strip()) for l in p.read_text().splitlines()
                if l.strip() and not l.strip().startswith("#")]
    return [Path(cwd)] if cwd else []


def _git(path):
    try:
        branch = subprocess.run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
        head = subprocess.run(["git", "-C", str(path), "log", "--oneline", "-1"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = len([l for l in subprocess.run(
            ["git", "-C", str(path), "status", "-s"],
            capture_output=True, text=True, timeout=5).stdout.splitlines() if l.strip()])
        return "{0} @ {1} (dirty {2})".format(branch or "?", head or "?", dirty)
    except Exception:
        return "(git unreadable)"


def _services(root):
    """services.conf lines: `name | http-url` → curl status; `launchd:<label>` → list."""
    p = Path(root) / CONF_SERVICES
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("launchd:"):
            label = line.split(":", 1)[1]
            try:
                r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5)
                hit = [l.strip() for l in r.stdout.splitlines() if label in l]
                out.append("{0}: {1}".format(label, " | ".join(hit) or "not loaded"))
            except Exception:
                out.append("{0}: launchctl failed".format(label))
        elif "|" in line:
            name, url = [x.strip() for x in line.split("|", 1)]
            try:
                r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                    "--max-time", "3", url],
                                   capture_output=True, text=True, timeout=5)
                out.append("{0}: {1}".format(name, r.stdout.strip() or "?"))
            except Exception:
                out.append("{0}: unreachable".format(name))
    return out


def _transcript_facts(transcript):
    """Mechanical transcript extraction: user text (system-reminder/tool-result
    stripped), edited file paths, notable shell commands."""
    users, files, cmds = [], [], []
    cmd_re = re.compile(r"\bgit\b|ssh |curl |launchctl|npm |pytest|make ", re.I)
    try:
        with open(transcript, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                t = ev.get("type")
                msg = ev.get("message") or {}
                if t == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        text = " ".join(str(c.get("text") or "") for c in content
                                        if isinstance(c, dict) and c.get("type") == "text")
                    else:
                        continue
                    text = re.sub(r"<(system-reminder|ide_)[^>]*>.*?</\\1>", "", text, flags=re.S)
                    text = text.strip()
                    if text and not text.startswith(("<command-", "Caveat:", "This session")):
                        users.append(text[:200])
                elif t == "assistant":
                    for c in (msg.get("content") or []):
                        if not isinstance(c, dict) or c.get("type") != "tool_use":
                            continue
                        name = c.get("name") or ""
                        inp = c.get("input") or {}
                        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                            p = inp.get("file_path") or inp.get("notebook_path") or ""
                            if p:
                                files.append("{0} {1}".format(name, p))
                        elif name == "Bash":
                            cmd = str(inp.get("command") or "")
                            if cmd_re.search(cmd):
                                cmds.append(cmd.split("\n")[0][:160])
    except OSError:
        pass
    return users[-MAX_USER_LINES:], files[-MAX_EDIT_FILES:], cmds[-MAX_CMDS:]


def _ctx_pointer(cwd=""):
    """context-mode sandbox pointer: the project's most recent session db, so the
    next window can keep querying its artifacts with ctx_* tools."""
    root = Path(os.environ.get("RELAY_CTX_SESSIONS")
                or (Path.home() / ".claude" / "context-mode" / "sessions"))
    try:
        import glob
        import sqlite3
        cands = sorted(glob.glob(str(root / "*.db")), key=os.path.getmtime, reverse=True)[:4]
        best = None
        for db in cands:
            try:
                c = sqlite3.connect("file:{0}?mode=ro".format(db), uri=True)
                meta = c.execute(
                    "SELECT project_dir, MAX(last_event_at) FROM session_meta").fetchone()
                n = c.execute("SELECT COUNT(*) FROM session_events").fetchone()[0]
                c.close()
                if not meta or not n:
                    continue
                if cwd and str(meta[0] or "") == cwd:
                    best = (db, meta, n)
                    break
                best = best or (db, meta, n)
            except sqlite3.Error:
                continue
        if best:
            db, meta, n = best
            return _L(
                "context-mode sandbox: latest db `{0}` (project {1}, {2} events) — "
                "ctx_execute / ctx_query can still reach this window's artifacts",
                "context-mode 沙箱：最近库 `{0}`（项目 {1}，{2} 个事件）——"
                "新窗里 ctx_execute / ctx_query 还能查到这次的沙箱产物").format(
                Path(db).name, meta[0] or "?", n)
    except Exception:
        pass
    return ""


def _tail(root):
    p = Path(root) / "tail.md"
    if p.is_file():
        return p.read_text(encoding="utf-8", errors="replace").strip()[:1500]
    return _L("(no tail cache yet — one run of relay_sync creates it)",
              "（tail 缓存还没有——跑过一次 relay_sync 就有了）")


def build(payload, root=None, now=None):
    root = root or relay_store.default_root()
    ts = now or time.strftime("%Y-%m-%d-%H%M")
    trig = payload.get("trigger") or payload.get("reason") or "?"
    lines = ["# handoff-auto ({0} · session {1} · trigger {2})".format(
        ts, str(payload.get("session_id") or "?")[:12], trig), ""]
    lines.append("## " + _L("Scene", "现场"))
    cwd = str(payload.get("cwd") or "")
    for d in _cfg_dirs(root, cwd):
        if d.is_dir():
            lines.append("- `{0}`: {1}".format(d, _git(d)))
    lines += ["- " + s for s in _services(root)]
    lines.append("")
    users, files, cmds = _transcript_facts(payload.get("transcript_path") or "")
    lines.append("## " + _L("What this window did (mechanical)", "本窗口做过的事（机械抽取）"))
    lines.append("### " + _L("Recent user messages", "用户最近的话"))
    lines += ["- " + u.replace("\n", " ") for u in users] or ["- " + _L("(none extracted)", "（抽不到）")]
    lines.append("### " + _L("Files touched", "改过的文件"))
    lines += ["- `{0}`".format(x) for x in files] or ["- " + _L("(none)", "（无）")]
    lines.append("### " + _L("git / ops actions", "git/运维动作"))
    lines += ["- `{0}`".format(c) for c in cmds] or ["- " + _L("(none)", "（无）")]
    lines.append("")
    lines.append("## " + _L("Open threads (tail cache)", "在途与待核（tail 缓存）"))
    lines.append(_tail(root))
    ptr = _ctx_pointer(cwd)
    if ptr:
        lines.append("")
        lines.append("## " + _L("Sandbox link", "沙箱衔接"))
        lines.append(ptr)
    lines.append("")
    lines.append("## " + _L("Usage", "用法"))
    lines.append("- " + _L("Ask the library: `memory_recall` (keywords / dates)",
                         "细节问库：`memory_recall`（关键词 / 日期段）"))
    lines.append("- " + _L("This file: `{0}`", "本份全文：`{0}`").format(
        handoff_dir(payload) / ("handoff-auto-{0}.md".format(ts))))
    return "\n".join(lines)


def ingest_handoff(md, ts, root=None):
    conn = relay_store.connect(root=root)
    n = relay_store.ingest_text(conn, "handoff", "handoff:auto:{0}".format(ts), md,
                                title="handoff-auto-{0}".format(ts), at=ts)
    conn.commit()
    return n


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}
    ts = time.strftime("%Y-%m-%d-%H%M")
    try:
        md = build(payload)
        d = handoff_dir(payload)
        d.mkdir(parents=True, exist_ok=True)
        (d / "handoff-auto-{0}.md".format(ts)).write_text(md, encoding="utf-8")
        n = ingest_handoff(md, ts)
        print("[relay-handoff] handoff-auto-{0}.md written, {1} segments ingested".format(ts, n))
    except Exception as exc:  # a hook must never block
        print("[relay-handoff] handoff failed (not blocking): {0}".format(exc), file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
