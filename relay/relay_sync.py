#!/usr/bin/env python3
"""relay sync — the write side: periodically feed segments into the library.

Sources (all read-only, idempotent upserts — re-running never duplicates):
  1. Claude Code memory files  ~/.claude/projects/*/memory/*.md   → kind=memory
  2. Handoff files             $RELAY_HANDOFF_DIR or scanned dirs → kind=handoff
     (handoff-auto-*.md plus any *handoff*.md / *交接*.md)
  3. Anything else             via `relay_store.py ingest <kind> <ref> --file f`
     or drop a directory of .md into sync-dirs.conf (one per line) → kind=note

Also maintains `tail.md` — the "open threads" block injected into handoffs and
new windows. Default: keep the last 1500 chars of `tail-src.md` if present,
otherwise leave whatever you wrote there by hand.

Usage: python3 relay_sync.py [--all] [--memories] [--handoffs] [--dirs] [--tail]
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay_store  # noqa: E402

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CONF_SYNC_DIRS = "sync-dirs.conf"
HANDOFF_PATTERNS = ("handoff-auto-*.md", "*handoff*.md", "*交接*.md")


def _mtime_iso(p):
    return datetime.fromtimestamp(Path(p).stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")


def sync_memories(conn):
    """Every project's memory/*.md under ~/.claude/projects/."""
    n = 0
    if not CLAUDE_PROJECTS.is_dir():
        return 0
    for f in sorted(CLAUDE_PROJECTS.glob("*/memory/*.md")):
        n += relay_store.ingest_text(conn, "memory",
                                     "memory:{0}:{1}".format(f.parent.parent.name, f.name),
                                     f.read_text(encoding="utf-8", errors="replace"),
                                     title=f.stem, at=_mtime_iso(f))
    return n


def sync_handoffs(conn, root):
    """Handoff files: RELAY_HANDOFF_DIR, else every dir in watch-dirs.conf."""
    dirs = []
    env = os.environ.get("RELAY_HANDOFF_DIR")
    if env:
        dirs.append(env)
    conf = Path(root) / "watch-dirs.conf"
    if conf.is_file():
        dirs += [l.strip() for l in conf.read_text().splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    n = 0
    for d in dirs:
        for pat in HANDOFF_PATTERNS:
            for f in sorted(glob.glob(str(Path(d) / pat))):
                p = Path(f)
                n += relay_store.ingest_text(conn, "handoff", "handoff:{0}".format(p.name),
                                             p.read_text(encoding="utf-8", errors="replace"),
                                             title=p.stem, at=_mtime_iso(p))
    return n


def sync_dirs(conn, root):
    """Generic .md ingest: sync-dirs.conf lists directories → kind=note."""
    conf = Path(root) / CONF_SYNC_DIRS
    if not conf.is_file():
        return 0
    n = 0
    for line in conf.read_text().splitlines():
        d = line.strip()
        if not d or d.startswith("#"):
            continue
        for f in sorted(Path(d).glob("*.md")):
            n += relay_store.ingest_text(conn, "note", "note:{0}:{1}".format(d, f.name),
                                         f.read_text(encoding="utf-8", errors="replace"),
                                         title=f.stem, at=_mtime_iso(f))
    return n


def sync_tail(root):
    """tail.md = last 1500 chars of tail-src.md (or whatever you put there)."""
    src = Path(root) / "tail-src.md"
    if not src.is_file():
        return 0
    body = src.read_text(encoding="utf-8", errors="replace").strip()[-1500:]
    (Path(root) / "tail.md").write_text(body, encoding="utf-8")
    return len(body)


def main(argv):
    do_all = "--all" in argv or not any(a.startswith("--") for a in argv)
    root = relay_store.default_root()
    conn = relay_store.connect(root=root)
    report = {}
    if do_all or "--memories" in argv:
        report["memories"] = sync_memories(conn)
    if do_all or "--handoffs" in argv:
        report["handoffs"] = sync_handoffs(conn, root)
    if do_all or "--dirs" in argv:
        report["dirs"] = sync_dirs(conn, root)
    if do_all or "--tail" in argv:
        report["tail_chars"] = sync_tail(root)
    conn.commit()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
