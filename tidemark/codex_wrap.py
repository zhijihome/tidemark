#!/usr/bin/env python3
"""codex_wrap — adapter for OpenAI Codex CLI.

Codex has no stdin hook system like Claude Code / Devin. What it has is
`notify` in ~/.codex/config.toml: a program invoked when an agent turn ends,
with a single JSON argument (agent-turn-complete payload, including
session-id/cwd on recent builds).

Wire it:

    # ~/.codex/config.toml
    notify = ["python3", "/path/to/tidemark/tidemark/codex_wrap.py", "turn-end"]

If the notify slot is already taken (e.g. Codex Computer Use), chain it —
everything after `--` is invoked with the same JSON appended last:

    notify = ["python3", "/path/to/codex_wrap.py", "turn-end", "--",
              "/original/program", "original-arg"]

Modes:
  turn-end    notify callback — refresh this session's handoff file + store
              entry (one per session, overwritten each turn — the newest copy
              is always the complete picture)
  waterline   print the latest session's context fill (manual check)
  inject      print the newest handoff file, for pasting into a new session

Session resolution: `session-id` in the notify payload → rollout-*-<sid>.jsonl
under ~/.codex/sessions; otherwise the newest rollout file wins.
"""
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drivers  # noqa: E402
import store    # noqa: E402

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _rollouts():
    return sorted(glob.glob(str(CODEX_HOME / "sessions/*/*/*/rollout-*.jsonl")),
                  key=os.path.getmtime, reverse=True)


def _notify_json(argv):
    """Codex passes the notification as the last argv element (a JSON string).
    Returns (parsed_dict, raw_json_string)."""
    for a in reversed(argv):
        try:
            p = json.loads(a)
            if isinstance(p, dict):
                return p, a
        except (ValueError, TypeError):
            continue
    return {}, ""


def _forward(argv, raw_json):
    """Everything after `--` names a program to chain: it gets its own args
    plus the notification JSON appended last, like codex would call it."""
    if "--" not in argv:
        return
    forward = argv[argv.index("--") + 1:]
    if not forward:
        return
    try:
        import subprocess
        subprocess.Popen(forward + ([raw_json] if raw_json else []),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _find_rollout(notify):
    sid = str(notify.get("session-id") or notify.get("session_id")
              or notify.get("rollout-id") or "")
    hits = _rollouts()
    if sid:
        for h in hits:
            if h.endswith(sid + ".jsonl"):
                return h, sid
    if hits:
        m = re.search(r"rollout-.*-([0-9a-f-]{36})\.jsonl$", hits[0])
        return hits[0], (m.group(1) if m else "")
    return "", sid


def _turn_end(notify):
    rollout, sid = _find_rollout(notify)
    cwd = str(notify.get("cwd") or "")
    name = "handoff-codex-{0}.md".format(sid or "latest")
    payload = {"agent": "codex", "session_id": sid or "?",
               "transcript_path": rollout, "cwd": cwd,
               "trigger": "turn-end", "handoff_name": name}
    import handoff
    md = handoff.build(payload)
    d = handoff.handoff_dir(payload)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(md, encoding="utf-8")
    conn = store.connect()
    n = store.ingest_text(conn, "handoff", "handoff:codex:{0}".format(sid),
                          md, title=name, at=time.strftime("%Y-%m-%d-%H%M"))
    conn.commit()
    print("[tidemark] {0} refreshed ({1} segments)".format(name, n))


def _waterline():
    rollout, sid = _find_rollout({})
    if not rollout:
        print("[tidemark] no codex rollout found under {0}".format(
            CODEX_HOME / "sessions"))
        return
    ctx = drivers.latest_ctx(rollout, driver="codex")
    win = 0
    for e in drivers._codex_lines(rollout):
        p = e.get("payload") or {}
        if p.get("type") == "token_count":
            win = int((p.get("info") or {}).get("model_context_window") or 0)
    if win:
        print("[tidemark] session {0}: {1} / {2} tokens ({3}%)".format(
            sid or "?", ctx, win, round(100.0 * ctx / win)))
    else:
        print("[tidemark] session {0}: {1} tokens".format(sid or "?", ctx))


def _inject():
    d = Path(os.environ.get("TIDEMARK_HANDOFF_DIR")
             or store.default_root() / "handoffs")
    hits = sorted(d.glob("handoff-*.md"),
                  key=os.path.getmtime, reverse=True)
    if not hits:
        print("[tidemark] no handoffs in {0}".format(d))
        return
    sys.stdout.write(hits[0].read_text(encoding="utf-8", errors="replace"))


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if which == "turn-end":
            notify, raw = _notify_json(sys.argv[2:])
            _turn_end(notify)
            _forward(sys.argv[2:], raw)
        elif which == "waterline":
            _waterline()
        elif which == "inject":
            _inject()
        else:
            print(__doc__)
    except Exception as exc:  # a notify callback must never block the agent
        print("[tidemark] codex_wrap {0} failed (not blocking): {1}"
              .format(which, exc), file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
