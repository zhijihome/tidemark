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
import sys
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


def _reply(mid, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": mid}
    if error:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _call(name, args):
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
            _reply(mid, {"tools": [RECALL_TOOL, STATS_TOOL]})
        elif method == "tools/call":
            p = req.get("params") or {}
            name = p.get("name")
            if name not in ("memory_recall", "memory_stats"):
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
