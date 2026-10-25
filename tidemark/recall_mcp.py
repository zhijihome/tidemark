#!/usr/bin/env python3
"""tidemark recall — stdio MCP server for the handoff/memory library.

Register in .mcp.json:
{"mcpServers":{"tidemark":{"command":"python3",
 "args":["/path/to/tidemark/recall_mcp.py"]}}}

Returns only relevant segments: FTS bm25 ranking + kind/date filters. The
library is fixed to TIDEMARK_HOME (or ~/.tidemark) — it honours no other
env pointer and cannot see outside its own database.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

RECALL_TOOL = {
    "name": "memory_recall",
    "description": "Search the local handoff/memory library (handoffs, open-threads, "
                   "verdicts, memory files, notes). `query` required; `kind` narrows "
                   "(handoff,card,verdict,memory,taskbook,note); after/before are "
                   "YYYY-MM-DD bounds. Returns only the relevant segments.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string", "description": "comma-separated: handoff,card,verdict,memory,taskbook,note"},
            "limit": {"type": "integer", "default": 8},
            "after": {"type": "string"},
            "before": {"type": "string"},
        },
        "required": ["query"],
    },
}

STATS_TOOL = {
    "name": "memory_stats",
    "description": "Segment counts and latest date per kind (diagnostics).",
    "inputSchema": {"type": "object", "properties": {}},
}

EXEC_TOOL = {
    "name": "sandbox_exec",
    "description": "Run a shell command; keep the window clean. Output over "
                   "SANDBOX_INLINE_BYTES (default 6KB) is stored whole in the "
                   "local sandbox library and you get back only the head plus "
                   "a ref — fetch the rest with sandbox_get. Use this instead "
                   "of Bash for builds, test runs, log dumps, file listings.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "integer", "default": 60},
        },
        "required": ["command"],
    },
}

GET_TOOL = {
    "name": "sandbox_get",
    "description": "Fetch a stored sandbox_exec output by ref (supports offset/limit "
                   "in characters for paging through large artifacts).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "ref": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "limit": {"type": "integer", "default": 20000},
        },
        "required": ["ref"],
    },
}


SANDBOX_INLINE_BYTES = int(os.environ.get("TIDEMARK_SANDBOX_INLINE") or "6144")
SANDBOX_MAX_BYTES = int(os.environ.get("TIDEMARK_SANDBOX_MAX") or str(8 * 1024 * 1024))


def _sandbox_exec(args):
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return "command is required"
    try:
        r = subprocess.run(["bash", "-c", cmd],
                           cwd=str(args.get("cwd") or "") or None,
                           capture_output=True, text=True,
                           timeout=min(600, int(args.get("timeout") or 60)))
    except subprocess.TimeoutExpired:
        return "(timed out — nothing stored)"
    body = (r.stdout or "") + (r.stderr or "")
    if r.returncode:
        body += "\n[exit {0}]".format(r.returncode)
    if len(body) <= SANDBOX_INLINE_BYTES:
        return body or "(no output)"
    ref = "exec:{0}".format(time.strftime("%Y-%m-%d-%H%M%S"))
    body = body[:SANDBOX_MAX_BYTES]
    conn = store.connect()
    store.upsert(conn, "sandbox", ref, body,
                 title="{0}…".format(cmd[:80]), at=time.strftime("%Y-%m-%d"))
    conn.commit()
    return ("{0}\n… [{1} chars stored — ref `{2}`, fetch with sandbox_get]"
            .format(body[:SANDBOX_INLINE_BYTES], len(body), ref))


def _sandbox_get(args):
    ref = str(args.get("ref") or "").strip()
    if not ref:
        return "ref is required"
    conn = store.connect()
    row = conn.execute("SELECT text FROM docs WHERE kind='sandbox' AND ref=?",
                       (ref,)).fetchone()
    if not row:
        return "no such ref."
    text = row[0]
    off = max(0, int(args.get("offset") or 0))
    lim = max(1, min(200000, int(args.get("limit") or 20000)))
    chunk = text[off:off + lim]
    if off + lim < len(text):
        chunk += "\n… [{0} more chars — offset {1}]".format(len(text) - off - lim, off + lim)
    return chunk


def _reply(mid, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": mid}
    if error:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _call(name, args):
    if name == "sandbox_exec":
        return _sandbox_exec(args)
    if name == "sandbox_get":
        return _sandbox_get(args)
    conn = store.connect()
    if name == "memory_stats":
        s = store.stats(conn)
        return "\n".join("{0}: {1} segments, latest {2}".format(k, v["rows"], v["latest_at"] or "-")
                         for k, v in s.items()) or "(empty)"
    q = str(args.get("query") or "").strip()
    if not q:
        return "query is required"
    kinds = [k.strip() for k in str(args.get("kind") or "").split(",") if k.strip()] or None
    hits = store.recall(conn, q, kinds=kinds,
                              limit=max(1, min(20, int(args.get("limit") or 8))),
                              after=str(args.get("after") or ""),
                              before=str(args.get("before") or ""))
    if not hits:
        return "no hits."
    return "\n\n".join(
        "[{kind}] {title}\n{snippet}\n— {ref} · {at}".format(**h) for h in hits)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        mid, method = req.get("id"), req.get("method")
        if mid is None:
            continue
        if method == "initialize":
            _reply(mid, {"protocolVersion": (req.get("params") or {}).get("protocolVersion") or "2024-11-05",
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "tidemark", "version": "1"}})
        elif method == "tools/list":
            _reply(mid, {"tools": [RECALL_TOOL, STATS_TOOL, EXEC_TOOL, GET_TOOL]})
        elif method == "tools/call":
            p = req.get("params") or {}
            name = p.get("name")
            if name not in ("memory_recall", "memory_stats", "sandbox_exec", "sandbox_get"):
                _reply(mid, error={"code": -32602, "message": "unknown tool"})
                continue
            try:
                text = _call(name, p.get("arguments") or {})
                _reply(mid, {"content": [{"type": "text", "text": text}]})
            except Exception as exc:  # noqa: BLE001
                _reply(mid, error={"code": -32603, "message": str(exc)})
        elif method == "ping":
            _reply(mid, {})
        else:
            _reply(mid, error={"code": -32601, "message": "unknown method"})


if __name__ == "__main__":
    main()
