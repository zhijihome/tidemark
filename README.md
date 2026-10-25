# tidemark

**Window rotation for Claude Code — hand the next window only a summary, keep
everything else in the sandbox.**

[中文说明](README.zh-CN.md)

tidemark is a small hook + MCP bundle that pairs with
[context-mode](https://github.com/mksglu/context-mode) (or stands alone):

- **context-mode** keeps bulk output out of your context (it lives in a SQLite
  sandbox, queryable via `ctx_*` tools).
- **tidemark** handles the *rotation ritual*: notice the waterline →
  mechanically write a handoff → `/clear` → new window starts already carrying
  the summary, plus a pointer to the sandbox DB so `ctx_*` keeps working.

No model calls anywhere. Every piece is stdlib-only Python.

## What you get

| Piece | Hook / surface | What it does |
|---|---|---|
| `waterline.py` | `UserPromptSubmit` | Reads real token usage off the transcript each turn; once past the line, injects a one-line "wrap up and `/clear`" reminder — once per crossing, rearms below 80%. |
| `handoff.py` | `PreCompact` + `SessionEnd(clear)` | Mechanically extracts the window's work (recent user asks, files touched, git/ops commands, watch-dir git status, service checks) into `handoff-auto-<ts>.md` and ingests it. |
| `inject.py` | `SessionStart` | On startup/compact/clear, injects the latest handoff + open-threads tail into the fresh window. |
| `recall_mcp.py` | stdio MCP | `memory_recall` / `memory_stats` over the library, plus a self-contained artifact sandbox: `sandbox_exec` runs a command and stores big output in the db (you get back a head + ref), `sandbox_get` pages it back. Standalone-complete — context-mode optional, not required. |
| `store.py` | library + CLI | SQLite FTS5 (**trigram** — CJK substring search actually works), paragraph-sized segments, idempotent upserts, hard `resident` isolation guard. |
| `sync.py` | cron / manual | Feeds the library: Claude Code memory files, handoff files, arbitrary `.md` dirs, and the `tail.md` open-threads cache. |

## Why not fully automatic?

Claude Code hooks cannot press `/clear` for you — no documented event can
clear the conversation. So the ritual is: **waterline speaks up → you `/clear`
→ SessionStart injects the summary**. The window only ever carries a ~3k-token
summary; artifacts stay in the context-mode sandbox, queryable on demand. That
also makes prompt-cache expiry irrelevant — you never pay to re-warm 700k
tokens of stale context.

## Install

```bash
git clone <this repo> && cd tidemark
./install.sh /path/to/your/project
```

The installer *merges* into `.claude/settings.json` (existing hooks untouched,
`.bak-tidemark` backup made) and adds `tidemark` to `.mcp.json`.
Restart the session or `/clear` to activate.

Optional cron feeder:

```cron
*/30 * * * * python3 /path/to/tidemark/tidemark/sync.py --all >/dev/null 2>&1
```

## Configure

All via env or files under `TIDEMARK_HOME` (default `~/.tidemark/`):

| Knob | Default | Meaning |
|---|---|---|
| `TIDEMARK_WATERLINE_TOKENS` | `400000` | warn line. For 200k windows use ~140k. |
| `autoCompactWindow` (settings.json) | `600k` | auto-compact ceiling the installer seeds — raise/lower to taste, or delete to use the model default. |
| `TIDEMARK_HANDOFF_DIR` | `~/.tidemark/handoffs` | where `handoff-auto-*.md` files land. Kept out of your repo on purpose — handoffs quote your own messages; a project-dir file is one `git add .` from public. Set this to opt back in. |
| `TIDEMARK_SANDBOX_INLINE` / `TIDEMARK_SANDBOX_MAX` | `6144` / `8MB` | inline threshold and storage cap for `sandbox_exec` output. |

Sandbox housekeeping: `python3 tidemark/store.py prune --days 30` drops stored
outputs older than N days (sandbox kind only — memory/handoffs are kept).
| `TIDEMARK_LANG` | `en` | `zh` for Chinese hook/handoff output. |
| `watch-dirs.conf` | session cwd | extra dirs whose git state appears in handoffs (one per line). |
| `services.conf` | — | `name \| url` → curl check, or `launchd:<label>` → launchctl status. |
| `sync-dirs.conf` | — | extra `.md` dirs synced into the library as `kind=note`. |
| `tail-src.md` | — | source for the open-threads `tail.md` cache. |
| `TIDEMARK_CTX_SESSIONS` | `~/.claude/context-mode/sessions` | where context-mode session DBs live. |

## Privacy / scope

The library only ever touches its own directory. `resident='tidemark'` is a hard
guard — foreign rows in the db make it refuse to open. Nothing is sent
anywhere; there is no telemetry, no network call except optional `services.conf`
health checks you write yourself. Handoffs redact anything that smells like a
credential (`Bearer …`, `*key=…`, `sk-…`, `ghp_…`, …) before writing, and land
outside your repo by default.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT — see [LICENSE](LICENSE).
