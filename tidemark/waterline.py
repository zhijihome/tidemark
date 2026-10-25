#!/usr/bin/env python3
"""UserPromptSubmit hook: context waterline sentinel.

After every prompt it scans the session transcript for the most recent usage
record (input + cache_read + cache_write = current context size). Past the
waterline it injects a one-line reminder — hooks cannot press /clear for you
(the API gives no such door), but they can speak up: "over the line, wrap up
and /clear — your handoff is already prepared".

Fires once per crossing per window; rearms after usage drops below 80% of the
threshold.

Threshold: TIDEMARK_WATERLINE_TOKENS (default 400k — sane for 1M windows; for
200k windows use ~140k).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store  # noqa: E402

THRESHOLD = int(os.environ.get("TIDEMARK_WATERLINE_TOKENS") or "400000")
STATE = store.default_root() / "waterline-state.json"
TAIL_BYTES = 64 * 1024

_ZH = os.environ.get("TIDEMARK_LANG", "").lower().startswith("zh")


def _latest_ctx(transcript):
    # The transcript tail is often hundreds of KB of prompt_snapshot lines with
    # no usage record — widen the window until one shows up.
    try:
        size = os.path.getsize(transcript)
    except OSError:
        return 0
    span = TAIL_BYTES
    while True:
        ctx = _ctx_in_tail(transcript, size, span)
        if ctx or span >= size:
            return ctx
        span *= 8


def _ctx_in_tail(transcript, size, span):
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, size - span))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    ctx = 0
    for line in tail.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        u = ((ev.get("message") or {}).get("usage") or {})
        if ev.get("type") == "assistant" and u:
            ctx = int(u.get("input_tokens") or 0) \
                + int(u.get("cache_read_input_tokens") or 0) \
                + int(u.get("cache_creation_input_tokens") or 0)
    return ctx


def _state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(s):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(s))
    except OSError:
        pass


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        sys.exit(0)
    sid = str(payload.get("session_id") or "?")
    ctx = _latest_ctx(str(payload.get("transcript_path") or ""))
    st = _state()
    armed = st.get("session") != sid or st.get("ctx", 0) < int(THRESHOLD * 0.8)
    st.update({"session": sid, "ctx": ctx})
    _save(st)
    if ctx >= THRESHOLD and armed:
        if _ZH:
            msg = ("〔水位线〕上下文已到约 {0}k token（线 {1}k）。建议收尾后 /clear 换新窗——"
                   "最新交接已备好，开窗会自动注入；沙箱产物仍在 context-mode 库可查。"
                   .format(ctx // 1000, THRESHOLD // 1000))
        else:
            msg = ("[waterline] context is ~{0}k tokens (line {1}k). Wrap up and "
                   "/clear — the latest handoff is ready and will be injected into "
                   "the new window; sandbox artifacts stay queryable in context-mode."
                   .format(ctx // 1000, THRESHOLD // 1000))
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg}}, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
