#!/usr/bin/env python3
"""drivers — transcript-format adapters so tidemark works beyond Claude Code.

Each driver knows two things:
  latest_ctx(ref) -> int          # current context size in tokens, 0 if unknown
  extract_facts(ref) -> (users, files, cmds)   # mechanical handoff material

`ref` is whatever the caller passes: a transcript path (Claude Code .jsonl,
any generic .jsonl), a path to a sessions database (Devin sessions.db), or a
uri like devin:/path/to/sessions.db#session_id.

Detection: explicit `agent`/`driver` in the payload wins; otherwise we sniff —
.db file → devin-style; .jsonl whose lines carry {"type","message"} → claude;
anything else → generic jsonl (best-effort).
"""
import glob
import json
import os
import re
import sqlite3
from pathlib import Path

MAX_USER_LINES = 12
MAX_EDIT_FILES = 12
MAX_CMDS = 15
_CMD_RE = re.compile(r"\bgit\b|ssh |curl |launchctl|npm |pytest|make ", re.I)
_EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit",
               "write", "edit", "notebook_edit", "apply_patch")
_SHELL_TOOLS = ("Bash", "exec", "shell", "run_command")


# ---------- claude-code ----------

def _cc_ctx(transcript):
    try:
        size = os.path.getsize(transcript)
    except OSError:
        return 0
    span = 64 * 1024
    while True:
        ctx = _cc_ctx_in_tail(transcript, size, span)
        if ctx or span >= size:
            return ctx
        span *= 8


def _cc_ctx_in_tail(transcript, size, span):
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, size - span))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    ctx = 0
    for line in tail.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        u = ((ev.get("message") or {}).get("usage") or {})
        if ev.get("type") == "assistant" and u:
            ctx = int(u.get("input_tokens") or 0) \
                + int(u.get("cache_read_input_tokens") or 0) \
                + int(u.get("cache_creation_input_tokens") or 0)
    return ctx


def _cc_facts(transcript):
    users, files, cmds = [], [], []
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
                        _push_tool(c.get("name") or "", c.get("input") or {}, files, cmds)
    except OSError:
        pass
    return users[-MAX_USER_LINES:], files[-MAX_EDIT_FILES:], cmds[-MAX_CMDS:]


def _push_tool(name, inp, files, cmds):
    if name in _EDIT_TOOLS:
        p = inp.get("file_path") or inp.get("notebook_path") or ""
        if p:
            files.append("{0} {1}".format(name, p))
    elif name in _SHELL_TOOLS:
        cmd = str(inp.get("command") or "")
        if _CMD_RE.search(cmd):
            cmds.append(cmd.split("\n")[0][:160])


# ---------- devin ----------

def _devin_db(ref):
    """ref may be a sessions.db path or 'devin:path#session_id'."""
    ref = str(ref or "")
    ref = ref[len("devin:"):] if ref.startswith("devin:") else ref
    path, _, sid = ref.partition("#")
    return (path or str(Path.home() / ".local/share/devin/cli/sessions.db"),
            sid or None)


def _devin_ctx(ref):
    db, sid = _devin_db(ref)
    try:
        conn = sqlite3.connect("file:{0}?mode=ro".format(db), uri=True)
        try:
            where = "session_id=?" if sid else \
                "session_id=(SELECT id FROM sessions ORDER BY last_activity_at DESC LIMIT 1)"
            row = conn.execute(
                "SELECT json_extract(metadata,'$.num_tokens_preceding') FROM message_nodes"
                " WHERE {0} AND metadata IS NOT NULL ORDER BY row_id DESC LIMIT 1"
                .format(where), (sid,) if sid else ()).fetchone()
            if row and row[0]:
                return int(row[0])
            # fallback: latest assistant metrics input+cache
            row = conn.execute(
                "SELECT json_extract(chat_message,'$.metadata.metrics.input_tokens'),"
                " json_extract(chat_message,'$.metadata.metrics.cache_read_tokens')"
                " FROM message_nodes WHERE {0}"
                " AND json_extract(chat_message,'$.role')='assistant'"
                " ORDER BY row_id DESC LIMIT 1".format(where),
                (sid,) if sid else ()).fetchone()
            if row:
                return int(row[0] or 0) + int(row[1] or 0)
        finally:
            conn.close()
    except (sqlite3.Error, ValueError):
        pass
    return 0


def _devin_facts(ref):
    db, sid = _devin_db(ref)
    users, files, cmds = [], [], []
    try:
        conn = sqlite3.connect("file:{0}?mode=ro".format(db), uri=True)
        try:
            where = "session_id=?" if sid else \
                "session_id=(SELECT id FROM sessions ORDER BY last_activity_at DESC LIMIT 1)"
            rows = conn.execute(
                "SELECT chat_message FROM message_nodes WHERE {0} ORDER BY row_id"
                .format(where), (sid,) if sid else ())
            for (raw,) in rows:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                role = m.get("role")
                if role == "user":
                    text = m.get("content") or ""
                    if isinstance(text, list):
                        text = " ".join(str(c.get("text") or "") for c in text
                                        if isinstance(c, dict))
                    text = str(text).strip()
                    if text and not text.startswith(("<command-", "Caveat:", "<system_", "<conversation_")):
                        users.append(text[:200])
                elif role == "assistant":
                    for tc in (m.get("tool_calls") or []):
                        _push_tool(tc.get("name") or "", tc.get("arguments") or {},
                                   files, cmds)
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return users[-MAX_USER_LINES:], files[-MAX_EDIT_FILES:], cmds[-MAX_CMDS:]


# ---------- generic jsonl (best effort) ----------

def _generic_facts(transcript):
    users, files, cmds = [], [], []
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
                role = ev.get("role") or ev.get("type")
                if role == "user":
                    text = ev.get("content") or ev.get("text") or ""
                    if isinstance(text, list):
                        text = " ".join(str(c.get("text") or "") for c in text
                                        if isinstance(c, dict))
                    if str(text).strip():
                        users.append(str(text)[:200])
                for tc in (ev.get("tool_calls") or []):
                    _push_tool(tc.get("name") or "", tc.get("arguments") or {},
                               files, cmds)
    except OSError:
        pass
    return users[-MAX_USER_LINES:], files[-MAX_EDIT_FILES:], cmds[-MAX_CMDS:]


def _generic_ctx(transcript):
    return 0  # unknown usage shape — sentinel stays silent rather than lying


# ---------- dispatch ----------

_DRIVERS = {
    "claude": (_cc_ctx, _cc_facts),
    "devin": (_devin_ctx, _devin_facts),
    "generic": (_generic_ctx, _generic_facts),
}


def detect(ref="", payload=None):
    payload = payload or {}
    explicit = str(payload.get("agent") or payload.get("driver") or "").lower()
    if explicit in _DRIVERS:
        return explicit
    r = str(ref or payload.get("transcript_path") or payload.get("session_db") or "")
    if r.startswith("devin:") or r.endswith((".db", ".sqlite", ".sqlite3")):
        return "devin"
    if r.endswith(".jsonl"):
        return "claude"   # CC transcripts are the common case for this shape
    return "generic"


def latest_ctx(ref, driver=""):
    ctx_fn, _ = _DRIVERS.get(driver or detect(ref), _DRIVERS["generic"])
    try:
        return int(ctx_fn(ref) or 0)
    except Exception:
        return 0


def extract_facts(ref, driver=""):
    _, facts_fn = _DRIVERS.get(driver or detect(ref), _DRIVERS["generic"])
    try:
        return facts_fn(ref)
    except Exception:
        return [], [], []


def find_latest_transcript(payload=None):
    """Best-effort transcript discovery when the caller gives no path."""
    r = str((payload or {}).get("transcript_path") or "")
    if r and Path(r).is_file():
        return r
    hits = sorted(glob.glob(str(Path.home() / ".claude/projects/*/*.jsonl")),
                  key=os.path.getmtime, reverse=True)
    return hits[0] if hits else ""
