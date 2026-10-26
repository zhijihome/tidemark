# tidemark

**Claude Code 自动换窗套件——新窗只带一份总结，其余全留在沙箱里。**

配合 [context-mode](https://github.com/mksglu/context-mode) 用（也可单用）。

**不止 Claude Code**：MCP 那半任何吃 MCP 的 agent 都能挂；钩子那半是
stdin-JSON 小协议，`drivers.py` 管 transcript 格式适配——现成驱动有
Claude Code、Devin CLI 和 Codex，通用 jsonl 兜底。没钩子的 agent 直接跑
`bin/tidemark handoff|inject|waterline` 手动走仪式。

- **context-mode** 管「东西不进窗」：大输出进它的 SQLite 沙箱，`ctx_*` 工具随查随取。
- **tidemark** 管「换窗仪式」：水位线到点提醒 → 机械生成交接 → 你 `/clear` → 新窗打开就带着总结，外加沙箱库指针，`ctx_*` 接着查上一窗的产物。

全程不调模型，纯 Python 标准库。

## 组成

| 件 | 钩子/入口 | 干啥 |
|---|---|---|
| `waterline.py` | `UserPromptSubmit` | 每轮扫 transcript 真实 token 用量，过线就往上下文塞一句「该 /clear 了」——每窗只喊一次，跌回 80% 重新武装 |
| `handoff.py` | `PreCompact` + `SessionEnd(clear)` | 机械抽取本窗做的事（用户话/改的文件/git 现场/服务探活）写成 `handoff-auto-<ts>.md` 并入库 |
| `inject.py` | `SessionStart` | 开窗/压缩/clear 后把最新交接+在途尾段塞进新窗上下文 |
| `recall_mcp.py` | stdio MCP | `memory_recall` / `memory_stats` 查库，外加自带沙箱：`sandbox_exec` 跑命令、大输出整存进库只回前段+引用，`sandbox_get` 按需取回。**单装即完整**，context-mode 是选配不是依赖 |
| `store.py` | 库+CLI | SQLite FTS5 **trigram 分词**——中文子串真能查（unicode61 查不到「新版本」）；段落级存储、幂等、隔离硬闸 |
| `sync.py` | cron/手动 | 喂库：CC 记忆文件、交接档、任意 md 目录、tail 尾段缓存 |

## 为什么不能全自动？

CC 钩子按不了 `/clear`——官方没有「程序化清空会话」这个门。所以形态是：
**哨兵张嘴提醒 → 你敲 `/clear` → 新窗自动带交接**。新窗上下文永远只有 ~3k
总结；顺带把 prompt 缓存一小时过期的问题也解了——你根本不用背着 70 万
token 的旧窗硬续。

## 安装

```bash
git clone <本 repo> && cd tidemark
./install.sh /你的/项目目录
```

安装器只**合并** `.claude/settings.json`（现有钩子女好端端留着，备份
`.bak-tidemark`），并往 `.mcp.json` 加 `tidemark`。重启会话或
`/clear` 生效。

可选定时喂库：

```cron
*/30 * * * * python3 /path/to/tidemark/tidemark/sync.py --all >/dev/null 2>&1
```

## 配置

全走 env 或 `TIDEMARK_HOME`（缺省 `~/.tidemark/`）下的配置文件：

| 开关 | 缺省 | 含义 |
|---|---|---|
| `TIDEMARK_WATERLINE_TOKENS` | `400000` | 提醒线。200k 窗口建议 ~140k |
| `autoCompactWindow` | `600k` | 安装器写入的自动压缩兜底；删掉就走模型默认 |
| `TIDEMARK_HANDOFF_DIR` | `~/.tidemark/handoffs` | 交接档落点——故意不落项目目录：交接会引用你的原话，放 repo 里一个 `git add .` 就出去了，要改回自己设 |
| `TIDEMARK_SANDBOX_INLINE` / `TIDEMARK_SANDBOX_MAX` | `6144` / `8MB` | `sandbox_exec` 输出的内联阈值和存储上限 |

沙箱清理：`python3 tidemark/store.py prune --days 30` 删掉超过 N 天的沙箱输出
（只清 sandbox 类，记忆/交接不动）。
| `TIDEMARK_LANG` | `en` | `zh` 出中文钩子输出 |
| `watch-dirs.conf` | 会话 cwd | 交接里要盯 git 现场的目录（一行一个） |
| `services.conf` | — | `名字 \| url` 探活，或 `launchd:<label>` |
| `sync-dirs.conf` | — | 额外 md 目录同步进库（kind=note） |
| `tail-src.md` | — | 「在途与待核」尾段的源 |
| `TIDEMARK_CTX_SESSIONS` | `~/.claude/context-mode/sessions` | context-mode 沙箱库目录 |

## 其它 agent

| Agent | 能用什么 | 怎么接 |
|---|---|---|
| Claude Code | 全部 | `./install.sh`（hooks + MCP） |
| Devin CLI | MCP + `bin/tidemark` 手动仪式 | `mcp_config.json` 挂 tidemark；transcript 读 `sessions.db`（`--agent devin` 或 `.db` 路径自动识别） |
| Codex | `notify` 驱动的轮末交接 | `~/.codex/config.toml` 加 `notify = ["python3", "/path/to/tidemark/tidemark/codex_wrap.py", "turn-end"]`——每轮结束刷新 `handoff-codex-<sid>.md` 并入库；`codex_wrap.py inject` 打印最新交接供粘贴，`waterline` 模式看用量。`~/.codex/sessions/**/rollout-*.jsonl` 自动识别 |
| 其它 | 库 + CLI | `store.py ingest/recall`、`bin/tidemark sync` |

## 边界

库只碰自己的目录；`resident='tidemark'` 硬闸——库里混进别家行直接拒用。
除了你自己写进 `services.conf` 的探活 URL，没有任何网络访问。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 许可

MIT，见 [LICENSE](LICENSE)。
