#!/usr/bin/env python3
"""SessionStart hook: inject the latest handoff + open-threads tail into the
fresh window's context.

Recommended matcher "startup|compact|clear" (skip `resume` — a resumed session
already carries its own context). stdout becomes the injected text; when
nothing exists yet it prints a one-line pointer — never errors, never stalls.
"""
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

MAX_HANDOFF_CHARS = int(os.environ.get("TIDEMARK_INJECT_CHARS") or "2600")
MAX_TAIL_CHARS = 1200


def _latest_handoff(payload):
    d = Path(os.environ.get("TIDEMARK_HANDOFF_DIR")
             or str(payload.get("cwd") or "")
             or (store.default_root() / "handoffs"))
    files = sorted(glob.glob(str(d / "handoff-auto-*.md")),
                   key=os.path.getmtime, reverse=True)
    if not files:  # no auto handoff yet — fall back to any handoff-looking file
        files = sorted(glob.glob(str(d / "*handoff*.md")) +
                       glob.glob(str(d / "*交接*.md")),
                       key=os.path.getmtime, reverse=True)
    return files[0] if files else ""


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
