#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tidemark tests: store, handoff, inject, waterline, sync, MCP wire."""
import json
import os
import subprocess
import sys
import tempfile
import time
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

    def test_commands_redacted(self):
        """Secrets in argv must never reach a handoff file."""
        p = Path(self.tmp.name) / "t3.jsonl"
        rows = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {
                "command": "curl -H 'Authorization: Bearer abc123SECRET' https://x | git log"}},
            {"type": "tool_use", "name": "Bash", "input": {
                "command": "npm login --password=hunter2"}},
            {"type": "tool_use", "name": "Bash", "input": {
                "command": "curl -H 'x-api-key: sk-1234567890abcdef' https://api.x"}},
        ]}}]
        p.write_text("\n".join(json.dumps(r) for r in rows))
        _, _, cmds = handoff._transcript_facts(str(p))
        joined = " ".join(cmds)
        self.assertNotIn("abc123SECRET", joined)
        self.assertNotIn("hunter2", joined)
        self.assertNotIn("sk-1234567890abcdef", joined)
        self.assertIn("redacted", joined)

    def test_handoff_dir_not_project(self):
        """Default handoff dir must NOT be inside the user's repo."""
        d = handoff.handoff_dir({"cwd": "/some/user/project"})
        self.assertNotEqual(str(d), "/some/user/project")
        self.assertIn("handoffs", str(d))


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
        self.assertIn("sandbox_exec", names)
        self.assertIn("content", lines[2]["result"])

    def test_sandbox_exec_get_roundtrip(self):
        env = dict(os.environ, TIDEMARK_HOME=self._tmpdir())
        big = "seq 1 400"     # ~1600 chars? force bigger:
        big = "python3 -c \"print('x'*20000)\""
        proc = subprocess.run(
            [sys.executable, str(RELAY / "recall_mcp.py")],
            input="\n".join([
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "sandbox_exec",
                                       "arguments": {"command": big}}}),
            ]),
            capture_output=True, text=True, env=env, timeout=30)
        res = json.loads(proc.stdout.splitlines()[0])
        text = res["result"]["content"][0]["text"]
        self.assertIn("stored", text)
        ref = text.split("ref `")[1].split("`")[0]
        proc2 = subprocess.run(
            [sys.executable, str(RELAY / "recall_mcp.py")],
            input=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "sandbox_get",
                                         "arguments": {"ref": ref}}}),
            capture_output=True, text=True, env=env, timeout=30)
        res2 = json.loads(proc2.stdout.splitlines()[0])
        self.assertIn("x" * 100, res2["result"]["content"][0]["text"])

    def _mcp_call(self, env, name, args):
        proc = subprocess.run(
            [sys.executable, str(RELAY / "recall_mcp.py")],
            input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": args}}),
            capture_output=True, text=True, env=env, timeout=30)
        return json.loads(proc.stdout.splitlines()[0])["result"]["content"][0]["text"]

    def test_same_second_two_execs_dont_collide(self):
        """shu R2 regression: two big execs in the same second must get distinct refs."""
        env = dict(os.environ, TIDEMARK_HOME=self._tmpdir())
        big = "python3 -c \"print('A'*8000)\""
        t1 = self._mcp_call(env, "sandbox_exec", {"command": big})
        t2 = self._mcp_call(env, "sandbox_exec", {"command": big})
        r1 = t1.split("ref `")[1].split("`")[0]
        r2 = t2.split("ref `")[1].split("`")[0]
        self.assertNotEqual(r1, r2)
        got = self._mcp_call(env, "sandbox_get", {"ref": r1})
        self.assertIn("A" * 100, got)

    def test_sandbox_output_redacted(self):
        env = dict(os.environ, TIDEMARK_HOME=self._tmpdir())
        t = self._mcp_call(env, "sandbox_exec", {
            "command": "python3 -c \"print('TOKEN=abc999\\n' * 4000)\""})
        self.assertNotIn("abc999", t)
        ref = t.split("ref `")[1].split("`")[0]
        got = self._mcp_call(env, "sandbox_get", {"ref": ref})
        self.assertNotIn("abc999", got)
        self.assertIn("redacted", got)

    def test_timeout_keeps_partial_output(self):
        env = dict(os.environ, TIDEMARK_HOME=self._tmpdir(),
                   TIDEMARK_SANDBOX_INLINE="100")
        t = self._mcp_call(env, "sandbox_exec", {
            "command": "python3 -c \"import time,sys; print('P'*20000); sys.stdout.flush(); time.sleep(30)\"",
            "timeout": 3})
        self.assertIn("timed out", t)
        self.assertIn("ref `", t)

    def test_prune_sandbox(self):
        conn = store.connect(root=Path(self._tmpdir()) / "p")
        store.upsert(conn, "sandbox", "old", "x" * 5000)
        store.upsert(conn, "memory", "m", "keep me")
        conn.execute("UPDATE docs SET created=? WHERE ref='old'", (time.time() - 40 * 86400,))
        conn.commit()
        n = store.prune(conn, days=30)
        conn.commit()
        self.assertEqual(n, 1)
        self.assertTrue(store.recall(conn, "keep me"))


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
