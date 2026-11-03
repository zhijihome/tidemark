#!/usr/bin/env python3
"""SessionStart hook: inject the latest handoff + open-threads tail into the
fresh window's context.

Recommended matcher "startup|compact|clear" (skip `resume` — a resumed session
already carries its own context). stdout becomes the injected text; when
nothing exists yet it prints a one-line pointer — never errors, never stalls.

Staleness note: a handoff is only written when a window ends. If another window
kept working after it (a long-running session elsewhere, a second terminal),
the new window would wake up behind. So after injecting, we look at transcripts
in the same project (the new session's transcript dir, or TIDEMARK_ACTIVITY_GLOB)
that changed after the handoff, and say how many and when the last one moved.
"""
import glob
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

MAX_HANDOFF_CHARS = int(os.environ.get("TIDEMARK_INJECT_CHARS") or "2600")
MAX_TAIL_CHARS = 1200


def _latest_handoff(payload):
    d = Path(os.environ.get("TIDEMARK_HANDOFF_DIR")
             or (store.default_root() / "handoffs"))
    files = sorted(glob.glob(str(d / "handoff-auto-*.md")),
                   key=os.path.getmtime, reverse=True)
    if not files:  # project-local handoffs if the user opted in to writing there
        cwd = str(payload.get("cwd") or "")
        if cwd:
            files = sorted(glob.glob(str(Path(cwd) / "handoff-auto-*.md")),
                           key=os.path.getmtime, reverse=True)
    if not files:  # no auto handoff yet — fall back to any handoff-looking file
        files = sorted(glob.glob(str(d / "*handoff*.md")) +
                       glob.glob(str(d / "*交接*.md")),
                       key=os.path.getmtime, reverse=True)
    return files[0] if files else ""


def _activity_after(handoff, payload):
    """Transcripts (other than this new session's) that moved after the handoff.

    Returns (count, latest_mtime). Looks at TIDEMARK_ACTIVITY_GLOB if set, else
    the *.jsonl next to payload["transcript_path"]. Anything unreadable → (0, 0).
    """
    pattern = os.environ.get("TIDEMARK_ACTIVITY_GLOB") or ""
    own = str(payload.get("transcript_path") or "")
    if not pattern and own:
        pattern = str(Path(own).parent / "*.jsonl")
    if not pattern:
        return 0, 0.0
    try:
        since = os.path.getmtime(handoff)
        own_real = os.path.realpath(own) if own else ""
        moved = [os.path.getmtime(p) for p in glob.glob(os.path.expanduser(pattern))
                 if os.path.realpath(p) != own_real and os.path.getmtime(p) > since + 1]
    except OSError:
        return 0, 0.0
    return len(moved), max(moved, default=0.0)


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = {}
    source = payload.get("source") or "?"
    root = store.default_root()
    out = ["[tidemark] handoff injected (source={0})".format(source)]
    f = _latest_handoff(payload)
    if f:
        body = Path(f).read_text(encoding="utf-8", errors="replace")[:MAX_HANDOFF_CHARS]
        out.append("— latest handoff `{0}`:\n{1}".format(Path(f).name, body))
        n, last = _activity_after(f, payload)
        if n:
            out.append("— heads-up: {0} other window(s) kept working after this handoff "
                       "(last activity {1}); it may be behind. Check recent work via "
                       "`memory_recall` before relying on it.".format(
                           n, time.strftime("%Y-%m-%d %H:%M", time.localtime(last))))
    else:
        out.append("— no handoff file yet.")
    tail = Path(root) / "tail.md"
    if tail.is_file():
        out.append("— open-threads tail:\n{0}".format(
            tail.read_text(encoding="utf-8", errors="replace").strip()[:MAX_TAIL_CHARS]))
    out.append("— ask the library via `memory_recall` (keywords / date ranges, "
               "returns only the relevant segments).")
    print("\n".join(out))
    sys.exit(0)


if __name__ == "__main__":
    main()
