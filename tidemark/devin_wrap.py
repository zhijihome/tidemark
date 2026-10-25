#!/usr/bin/env python3
"""devin_wrap — hook adapter for Devin CLI.

Devin hooks fire with stdin JSON (session_id/prompt_id/…) but no transcript
path — its transcript lives in ~/.local/share/devin/cli/sessions.db. This
wrapper reads the payload, pins it to the right session in that db, and
dispatches to the right tidemark piece:

  python3 devin_wrap.py waterline    # UserPromptSubmit — token sentinel
  python3 devin_wrap.py handoff      # SessionEnd — write the window's handoff
  python3 devin_wrap.py inject       # SessionStart — inject latest handoff
  python3 devin_wrap.py postcompact  # PostCompaction — handoff + re-inject

Wire it in ~/.config/devin/config.json under "hooks".
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drivers  # noqa: E402,F401

DB = str(Path.home() / ".local/share/devin/cli/sessions.db")


def _payload():
    try:
        p = json.load(sys.stdin)
    except (ValueError, OSError):
        p = {}
    p.setdefault("agent", "devin")
    ref = DB
    sid = str(p.get("session_id") or "")
    if sid:
        ref += "#" + sid          # pin this exact session in the db
    p.setdefault("session_db", ref)
    return p


def _feed(p):
    sys.stdin = io.StringIO(json.dumps(p))


def _capture(fn):
    """Run a hook main(); return its stdout text (it may sys.exit — fine)."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        fn()
    except SystemExit:
        pass
    finally:
        sys.stdout = old
    return buf.getvalue()


def _emit(event, text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event,
        "additionalContext": text}}, ensure_ascii=False))


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else ""
    p = _payload()
    _feed(p)

    if which == "waterline":
        import waterline
        waterline.main()                     # emits its own hookSpecificOutput
    elif which == "handoff":
        import handoff
        _feed(p); handoff.main()             # log line only
    elif which == "inject":
        import inject
        _emit("SessionStart", _capture(inject.main))
    elif which == "postcompact":
        import handoff
        _feed(p)
        _capture(handoff.main)               # message_nodes still hold full history
        import inject
        _feed(p)
        _emit("PostCompaction", _capture(inject.main))
    else:
        sys.exit(0)
    sys.exit(0)


if __name__ == "__main__":
    main()
