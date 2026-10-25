#!/usr/bin/env python3
"""relay store — standalone recall library for window-rotation handoffs.

Fully self-contained: its own directory, its own SQLite file, a fixed
`resident` tag per row. It never reads another app's database or env vars —
the only pointer it honours is RELAY_HOME (used by tests / custom installs).

Storage: SQLite FTS5 (ships with every modern Python). Documents are stored
in *paragraph-sized segments* — the smallest recall unit. `(kind, ref)` is an
idempotent upsert key, so sync scripts can re-run without duplicating.
"""
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

RESIDENT = "relay"

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  rowid INTEGER PRIMARY KEY,
  resident TEXT NOT NULL DEFAULT 'relay',
  kind TEXT NOT NULL,           -- handoff / card / verdict / memory / taskbook / note …
  ref TEXT NOT NULL,            -- source anchor: path#heading, page#segment …
  title TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  at TEXT NOT NULL DEFAULT '',  -- business date (ISO or file mtime)
  created REAL NOT NULL,
  updated REAL NOT NULL,
  UNIQUE(kind, ref)
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  title, text, content='docs', content_rowid='rowid', tokenize='%(tok)s'
);
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO docs_fts(rowid, title, text) VALUES (new.rowid, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, title, text) VALUES('delete', old.rowid, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, title, text) VALUES('delete', old.rowid, old.title, old.text);
  INSERT INTO docs_fts(rowid, title, text) VALUES (new.rowid, new.title, new.text);
END;
"""


def default_root(env=None):
    """Library root: RELAY_HOME overrides (tests / custom installs)."""
    env = os.environ if env is None else env
    return Path(env.get("RELAY_HOME") or (Path.home() / ".session-relay"))


def db_path(root=None, env=None):
    return (root or default_root(env)) / "relay.sqlite3"


def _tokenizer(conn):
    """trigram (SQLite 3.34+) matches CJK by substring — unicode61 treats a whole
    Chinese paragraph as one token so queries like "新版本" inside "新版本文字"
    never hit. Falls back to unicode61 on ancient SQLite builds."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _tok_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE IF EXISTS _tok_probe")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"


def connect(root=None, env=None):
    p = db_path(root, env)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(SCHEMA % {"tok": _tokenizer(conn)})
    # Isolation red line: only our rows may live here — refuse a polluted db.
    n = conn.execute("SELECT COUNT(*) FROM docs WHERE resident != ?", (RESIDENT,)).fetchone()[0]
    if n:
        raise RuntimeError("relay store contains foreign rows ({0}); refusing to use it".format(n))
    return conn


def _terms(q):
    return [t.replace('"', '""') for t in re.split(r"\s+", q.strip()) if t]


def _fts_query(q):
    """AND across terms, each as a quoted phrase."""
    terms = _terms(q)
    if not terms:
        return ""
    return " AND ".join('"{0}"'.format(t) for t in terms)


def _short_terms(q):
    """trigram index is useless for terms shorter than 3 chars — those go LIKE."""
    return [t for t in _terms(q) if len(t) < 3]


def upsert(conn, kind, ref, text, title="", at=""):
    if not text or not text.strip():
        return False
    now = time.time()
    conn.execute(
        "INSERT INTO docs(resident, kind, ref, title, text, at, created, updated)"
        " VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(kind, ref) DO UPDATE SET text=excluded.text,"
        " title=excluded.title, at=excluded.at, updated=excluded.updated",
        (RESIDENT, kind, ref, title, text.strip(), at, now, now))
    return True


def _split_paragraphs(md, max_chars=2400):
    """Split markdown into segments: break at headings, then blank lines/size."""
    blocks, cur, cur_title = [], [], ""
    for line in md.splitlines():
        if line.startswith("#") and cur:
            blocks.append((cur_title, "\n".join(cur)))
            cur = []
        if line.startswith("#"):
            cur_title = line.lstrip("#").strip()[:120]
        cur.append(line)
        if sum(len(l) for l in cur) > max_chars:
            blocks.append((cur_title, "\n".join(cur)))
            cur = []
    if cur:
        blocks.append((cur_title, "\n".join(cur)))
    return [b for b in blocks if b[1].strip()]


def ingest_text(conn, kind, ref, md, title="", at=""):
    """Ingest a whole document: split into segments, ref gets a #i anchor."""
    n = 0
    for i, (heading, body) in enumerate(_split_paragraphs(md)):
        t = title or heading or Path(ref).name
        if upsert(conn, kind, "{0}#{1}".format(ref, i), body,
                  title="{0}{1}".format(t, " | " + heading if heading and heading != t else ""), at=at):
            n += 1
    return n


def _like_recall(conn, query, kinds=None, limit=8, after="", before=""):
    """LIKE fallback: every term must substring-match (AND). For short terms and
    whenever FTS comes up empty."""
    sql = ("SELECT d.kind, d.ref, d.title,"
           " substr(d.text, max(1, instr(d.text, ?) - 40), 90) AS snip, d.at, 0.0"
           " FROM docs d WHERE 1=1")
    first = _terms(query)[0] if _terms(query) else ""
    args = [first]
    for t in _terms(query):
        sql += " AND (d.text LIKE ? OR d.title LIKE ?)"
        args += ["%" + t + "%", "%" + t + "%"]
    if kinds:
        sql += " AND d.kind IN ({0})".format(",".join("?" * len(kinds)))
        args += list(kinds)
    if after:
        sql += " AND d.at >= ?"
        args.append(after)
    if before:
        sql += " AND d.at <= ?"
        args.append(before)
    sql += " ORDER BY d.updated DESC LIMIT ?"
    args.append(int(limit))
    return [{"kind": k, "ref": r, "title": t, "snippet": s, "at": a}
            for k, r, t, s, a, _ in conn.execute(sql, args).fetchall()]


def recall(conn, query, kinds=None, limit=8, after="", before=""):
    """FTS recall (substring hits via trigram, bm25 ranked); LIKE fallback for
    short terms or empty results."""
    if _short_terms(query):
        return _like_recall(conn, query, kinds=kinds, limit=limit, after=after, before=before)
    fq = _fts_query(query)
    sql = ("SELECT d.kind, d.ref, d.title, snippet(docs_fts,1,'<','>','…',10) AS snip,"
           " d.at, bm25(docs_fts) AS score"
           " FROM docs_fts f JOIN docs d ON d.rowid = f.rowid WHERE 1=1")
    args = []
    if fq:
        sql += " AND docs_fts MATCH ?"
        args.append(fq)
    if kinds:
        sql += " AND d.kind IN ({0})".format(",".join("?" * len(kinds)))
        args += list(kinds)
    if after:
        sql += " AND d.at >= ?"
        args.append(after)
    if before:
        sql += " AND d.at <= ?"
        args.append(before)
    sql += " ORDER BY score LIMIT ?"
    args.append(int(limit))
    try:
        hits = [{"kind": k, "ref": r, "title": t, "snippet": s, "at": a}
                for k, r, t, s, a, _ in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        hits = []
    return hits or _like_recall(conn, query, kinds=kinds, limit=limit,
                                after=after, before=before)


def stats(conn):
    rows = conn.execute(
        "SELECT kind, COUNT(*), MAX(at) FROM docs GROUP BY kind ORDER BY kind").fetchall()
    return {k: {"rows": c, "latest_at": a or ""} for k, c, a in rows}


# ---- CLI: ingest / recall / stats ----

def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="relay_store")
    ap.add_argument("--db", default="", help="library root (default ~/.session-relay)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("kind")
    p.add_argument("ref")
    p.add_argument("--title", default="")
    p.add_argument("--at", default="")
    p.add_argument("--file", default="", help="read body from file; default stdin")
    p = sub.add_parser("recall")
    p.add_argument("query")
    p.add_argument("--kind", default="")
    p.add_argument("--limit", type=int, default=8)
    sub.add_parser("stats")
    a = ap.parse_args(argv)
    root = Path(a.db) if a.db else None
    conn = connect(root=root)
    if a.cmd == "ingest":
        text = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
        n = ingest_text(conn, a.kind, a.ref, text, title=a.title, at=a.at)
        conn.commit()
        print("ingested {0} segments <- {1}".format(n, a.ref))
    elif a.cmd == "recall":
        kinds = [k.strip() for k in a.kind.split(",") if k.strip()] or None
        for h in recall(conn, a.query, kinds=kinds, limit=a.limit):
            print("[{0}] {1} — {2}\n  {3}\n  ({4})".format(
                h["kind"], h["title"], h["ref"], h["snippet"], h["at"]))
    else:
        for k, v in stats(conn).items():
            print("{0}: {1} segments, latest {2}".format(k, v["rows"], v["latest_at"]))


if __name__ == "__main__":
    _cli(sys.argv[1:])
