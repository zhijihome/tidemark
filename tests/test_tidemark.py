#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tidemark tests: store, handoff, inject, waterline, sync, MCP wire."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELAY = ROOT / 'tidemark'
sys.path.insert(0, str(RELAY))
import store  # noqa: E402
import handoff  # noqa: E402
import sync  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "mem"

    def _conn(self):
        return store.connect(root=self.root)

    def test_ingest_recall_paragraphs(self):
        conn = self._conn()
        n = store.ingest_text(conn, "memory", "m:x.md",
                                    "# T\ncall audit found root cause.\n\n## S\nquiet room tiers.", at="2026-10-25")
        conn.commit()
        self.assertGreaterEqual(n, 2)
        self.assertTrue(store.recall(conn, "root cause"))

    def test_cjk_substring_recall(self):
        conn = self._conn()
        store.upsert(conn, "card", "n:x#0", "新版本文字已入库")
        conn.commit()
        hits = store.recall(conn, "新版本")
        self.assertEqual(len(hits), 1)
        self.assertEqual(store.recall(conn, "旧版本"), [])

    def test_kind_filter(self):
        conn = self._conn()
        store.ingest_text(conn, "handoff", "h:1", "acceptance went green")
        store.ingest_text(conn, "memory", "m:1", "acceptance went green")
        conn.commit()
        hits = store.recall(conn, "acceptance", kinds=["memory"])
        self.assertTrue(all(h["kind"] == "memory" for h in hits))

    def test_isolation_resident_guard(self):
        conn = self._conn()
        conn.execute("INSERT INTO docs(resident, kind, ref, text, created, updated)"
                     " VALUES('other','x','x','foreign row',0,0)")
        conn.commit()
        conn.close()
        with self.assertRaises(RuntimeError):
            self._conn()

    def test_stats(self):
        conn = self._conn()
        store.ingest_text(conn, "verdict", "n:p#0", "verdict: per-instance")
        conn.commit()
        self.assertIn("verdict", store.stats(conn))


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "mem"

    def _transcript(self):
        p = Path(self.tmp.name) / "t.jsonl"
        rows = [
            {"type": "user", "message": {"content": "ship the MCP plane"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/mcp.py"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "git commit -m x"}}]}},
        ]
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
        return str(p)

    def test_build_and_ingest(self):
        md = handoff.build(
            {"session_id": "s1", "transcript_path": self._transcript(),
             "trigger": "auto", "cwd": self.tmp.name}, root=self.root)
        self.assertIn("handoff-auto", md)
        self.assertIn("ship the MCP plane", md)
        self.assertIn("/x/mcp.py", md)
        n = handoff.ingest_handoff(md, "2026-10-25-1740", root=self.root)
        conn = store.connect(root=self.root)
        self.assertTrue(store.recall(conn, "MCP plane", kinds=["handoff"]))
        self.assertGreater(n, 0)

    def test_transcript_filters_noise(self):
        p = Path(self.tmp.name) / "t2.jsonl"
        rows = [{"type": "user", "message": {"content": "<command-name>/compact</command-name>"}},
                {"type": "user", "message": {"content": "real question " + "x" * 300}}]
        p.write_text("\n".join(json.dumps(r) for r in rows))
        users, files, cmds = handoff._transcript_facts(str(p))
        self.assertEqual(len(users), 1)
        self.assertLessEqual(len(users[0]), 200)


class InjectTests(unittest.TestCase):
    def test_inject_prints_handoff(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        import shlex, io
        from contextlib import redirect_stdout
        d = Path(tmp.name)
        (d / "handoff-auto-2026-10-25-1740.md").write_text("# HANDOFF BODY AAA")
        env = dict(os.environ, TIDEMARK_HOME=str(d / "mem"), TIDEMARK_HANDOFF_DIR=str(d))
        proc = subprocess.run(
            [sys.executable, str(RELAY / "inject.py")],
            input=json.dumps({"source": "clear", "cwd": str(d)}),
            capture_output=True, text=True, env=env, timeout=30)
        self.assertIn("HANDOFF BODY AAA", proc.stdout)
        self.assertIn("memory_recall", proc.stdout)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "mem"

    def test_sync_dirs_generic(self):
        md = Path(self.tmp.name) / "notes"
        md.mkdir()
        (md / "rule.md").write_text("# rule\nstaging on :18792 first")
        (self.root).mkdir(parents=True, exist_ok=True)
        (self.root / "sync-dirs.conf").write_text(str(md) + "\n")
        conn = store.connect(root=self.root)
        n = sync.sync_dirs(conn, self.root)
        conn.commit()
        self.assertGreater(n, 0)
        self.assertTrue(store.recall(conn, "18792"))

    def test_tail_maintained(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tail-src.md").write_text("x" * 2000)
        n = sync.sync_tail(self.root)
        self.assertEqual(n, 1500)
        self.assertTrue((self.root / "tail.md").is_file())


class McpProtocolTests(unittest.TestCase):
    _td = None

    @classmethod
    def _tmpdir(cls):
        if cls._td is None:
            cls._td = tempfile.TemporaryDirectory()
        return cls._td.name

    def test_stdio_roundtrip(self):
        env = dict(os.environ, TIDEMARK_HOME=self._tmpdir())
        proc = subprocess.run(
            [sys.executable, str(RELAY / "recall_mcp.py")],
            input="\n".join([
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "memory_recall", "arguments": {"query": "x"}}}),
            ]),
            capture_output=True, text=True, env=env, timeout=30)
        lines = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "tidemark")
        names = [t["name"] for t in lines[1]["result"]["tools"]]
        self.assertIn("memory_recall", names)
        self.assertIn("content", lines[2]["result"])


class WaterlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, ctx_tokens):
        import waterline
        tp = Path(self.tmp.name) / "t.jsonl"
        tp.write_text(json.dumps({"type": "assistant", "message": {
            "usage": {"input_tokens": ctx_tokens - 2,
                      "cache_read_input_tokens": 2}}}))
        import io
        from contextlib import redirect_stdout
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(
            {"session_id": "s-wl", "transcript_path": str(tp)}))
        buf = io.StringIO()
        old_state = waterline.STATE
        waterline.STATE = Path(self.tmp.name) / "st.json"
        try:
            with redirect_stdout(buf):
                try:
                    waterline.main()
                except SystemExit:
                    pass
        finally:
            sys.stdin = stdin
            waterline.STATE = old_state
        return buf.getvalue()

    def test_warns_once_over_line(self):
        import waterline
        old = waterline.THRESHOLD
        waterline.THRESHOLD = 1000
        try:
            self.assertIn("waterline", self._run(1500))
            self.assertNotIn("waterline", self._run(1600))
        finally:
            waterline.THRESHOLD = old

    def test_silent_under_line(self):
        import waterline
        old = waterline.THRESHOLD
        waterline.THRESHOLD = 99999
        try:
            self.assertNotIn("waterline", self._run(500))
        finally:
            waterline.THRESHOLD = old


if __name__ == "__main__":
    unittest.main()
