# Codex Auto Retry Plugin

Codex Auto Retry 是一个本地 Codex 插件：当 `Stop` Hook 能看到明确的临时模型错误时，它按 turn 执行有限指数退避，并通过 continuation prompt 让 Codex 从失败点安全续跑。

当前 `0.2.0` 已按 2026-08-20 发布的 Codex CLI `0.149.0` Hook 契约完成兼容改造。

## 先看能力边界

这个插件不是底层 HTTP/Provider 重试器，也不能保证捕获所有 `429`、`5xx`、timeout 或 SSE/network hard failure。

- `Stop` 触发时，插件可以检查 `last_assistant_message`。
- 如果 Codex 内置重试后仍以 Provider sampling `Err` 结束，当前客户端不会运行 `Stop`，插件没有入口介入。
- 当前公开 Hook 事件中没有可供普通插件注册的 `ProviderError` 或 `TurnError`。
- Codex 自身的 Provider、stream 和 network 恢复逻辑仍由客户端负责；本插件只是 `Stop` 可见错误的 fallback。

官方说明 `transcript_path` 不是稳定接口，因此 `0.2.0` 默认不读取 transcript。只有显式开启兼容 fallback 时才会 best-effort 解析其尾部。

## 可续跑错误

为降低普通回答误触发，除官方 high-demand、`rate_limit_exceeded`、`stream disconnected`、缺少 `response.completed` 等客户端强签名外，错误必须同时具有明确的运行时 error envelope、临时失败形态和 Codex/OpenAI/model provider 上下文：

- `We're currently experiencing high demand...`
- Provider `429`、`rate_limit_exceeded`、`too many requests`、`Retry-After`
- Provider `500`、`502`、`503`、`504`、`529`、Cloudflare `520-524`
- model stream/SSE 中断或缺少 `response.completed`
- 带 Provider 上下文的 network、DNS、TLS、timeout、connection reset/refused
- Provider 明确给出的 temporary/transient/try again 类错误

这些错误不会续跑：

- 用户主动中断或取消
- `400`、`401`、`403`、`404`
- API key、authentication、permission
- billing、余额、`insufficient_quota`、usage limit
- context/token 长度限制
- invalid request、model not found、unsupported model
- content/safety policy
- 项目自身的业务服务器、测试、命令或网络错误

## 工作原理

Codex CLI `0.149.0` 的 `Stop` payload 包含 `session_id`、`turn_id`、`stop_hook_active` 和 `last_assistant_message`。插件按以下顺序处理：

1. 严格校验 Hook 事件和字段类型。
2. 只扫描受限长度的 `last_assistant_message`；默认不读不稳定的 transcript。
3. 先排除认证、权限、配额、请求和策略类永久错误，再匹配 Provider 临时错误。
4. 用 `session_id + turn_id` 的 SHA-256 作为当前 turn 的重试作用域。
5. 在 `PLUGIN_DATA` 下通过 SQLite 原子领取下一次尝试，确保 Windows 多进程下仍严格执行上限。
6. 等待包含 `Retry-After`、指数退避和 jitter 的有界延迟，然后返回：

```json
{
  "decision": "block",
  "reason": "从当前 turn 的失败点安全继续……"
}
```

`decision: "block"` 不会重放底层请求。Codex 会把 `reason` 创建为新的 continuation prompt，并继续同一个 turn；此时 `stop_hook_active` 会从 `false` 变为 `true`，`turn_id` 保持不变。

如果另一个匹配的 `Stop` Hook 返回 `continue: false`，官方优先级规则会阻止 continuation，本插件不能覆盖该决定。

## 副作用安全

Stop continuation 不是事务。模型响应失败时，之前的工具调用或外部操作可能已经成功。

`0.2.0` 的 continuation prompt 明确要求：

- 先检查工作区、工具结果和外部系统状态，从失败点恢复。
- 不得重复已经成功的写入、commit、push、部署、发送、支付、删除等操作。
- 结果未知时必须先核对；无法确认是否已生效时停止自动操作并向用户说明。

这能降低重复副作用风险，但不能提供事务级保证。涉及不可逆或高价值操作时，不建议提高默认次数。

## 安装

先决条件：

- Codex CLI `0.149.0` 已实测通过；后续版本仍需按当时的 Hook 契约复验。
- Python `3.10+`，仅使用标准库。
- Windows 需要 `py -3`；Linux/macOS 需要 `python3`。

从 GitHub marketplace 安装：

```powershell
codex plugin marketplace add https://github.com/Matrtex/codex-auto-retry-plugin
codex plugin add codex-auto-retry@codex-auto-retry
```

安装或更新后必须：

1. 在 Codex 中运行 `/hooks`。
2. 审查并信任 `codex-auto-retry` 的精确 Hook 定义；Hook hash 变化后旧信任不会继续生效。
3. 新建一个 task，使新版 Hook 和 Skill 完整加载。

插件启用本身不会自动信任 Hook。未信任时，Codex 会跳过它。

更新已配置的 Git marketplace：

```powershell
codex plugin marketplace upgrade codex-auto-retry
codex plugin add codex-auto-retry@codex-auto-retry
```

更新后同样需要在 `/hooks` 重新审查变更后的 Hook，并新建 task。仅拉取仓库或重启 task 不会刷新已安装 cache。

## 配置

环境变量由启动 Codex 的进程捕获。先设置变量，再完全退出并重新启动 Codex，最后新建 task；只新建 task 不会让已运行的 desktop/CLI 进程获得新的 OS 环境变量。

| 变量 | 默认值 | 有效范围或说明 |
| --- | ---: | --- |
| `CODEX_AUTO_RETRY_MAX_ATTEMPTS` | `3` | `0-10`；`0` 表示关闭续跑 |
| `CODEX_AUTO_RETRY_BASE_DELAY` | `8` | 首次退避秒数，最大按 `115` 处理 |
| `CODEX_AUTO_RETRY_MAX_DELAY` | `60` | jitter 后的单次总延迟上限；硬上限 `115` 秒 |
| `CODEX_AUTO_RETRY_BACKOFF_FACTOR` | `1.8` | `1-10` |
| `CODEX_AUTO_RETRY_JITTER` | `2` | 随机抖动秒数；不会突破最大延迟 |
| `CODEX_AUTO_RETRY_STATE_TTL_SECONDS` | `3600` | `300-604800`；清理超过 TTL 未再次出现的重试作用域 |
| `CODEX_AUTO_RETRY_STATE_DIR` | 空 | 显式状态目录，优先级高于 `PLUGIN_DATA` |
| `CODEX_AUTO_RETRY_MESSAGE_SCAN_CHARS` | `32768` | `1024-1048576`；assistant message 尾部扫描长度 |
| `CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK` | `0` | 设为 `1` 才启用不稳定 transcript 兼容回退 |
| `CODEX_AUTO_RETRY_TRANSCRIPT_TAIL_BYTES` | `262144` | fallback 开启时读取的尾部 bytes，范围 `4096-4194304` |

示例：

```powershell
$env:CODEX_AUTO_RETRY_MAX_ATTEMPTS = "3"
$env:CODEX_AUTO_RETRY_BASE_DELAY = "10"
$env:CODEX_AUTO_RETRY_MAX_DELAY = "60"
```

当错误文本包含数字形式的 `Retry-After` 时，插件会把它作为最低等待时间。如果该值超过 `CODEX_AUTO_RETRY_MAX_DELAY`、`115` 秒硬上限或当前 Hook 剩余时间，插件会停止 continuation，而不是提前重试。Hook timeout 是 `120` 秒，预留 `5` 秒用于进程启动、状态事务和输出。

## 状态与隐私

状态目录优先级：

```text
CODEX_AUTO_RETRY_STATE_DIR > PLUGIN_DATA > CLAUDE_PLUGIN_DATA > 操作系统 state 目录
```

`state-v2.sqlite3` 只保存 SHA-256 scope、尝试次数和时间戳，不保存 `session_id`、`turn_id`、错误正文或 assistant message。`0.1.x` 的 `state.json` 不会迁移；升级后旧计数自然重置。

`0.1.x` 的旧文件曾保存明文 `session_id` 和错误片段。`0.2.0` 为避免与仍在运行的旧 task 争用，不会自动删除它。关闭所有旧 task 并完全退出 Codex 后，可手动清理：

```powershell
Remove-Item -LiteralPath "$env:LOCALAPPDATA\CodexAutoRetry\state.json" -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "$env:LOCALAPPDATA\CodexAutoRetry\state.tmp" -ErrorAction SilentlyContinue
```

Linux/macOS 默认旧路径是 `${XDG_STATE_HOME:-$HOME/.local/state}/codex-auto-retry/state.json`；如果曾设置 `CODEX_AUTO_RETRY_STATE_DIR`，还应检查该目录下的旧 `state.json`/`state.tmp`。

## transcript 兼容回退

仅当 `last_assistant_message` 为 `null` 或空字符串，而且 `CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK=1` 时，插件才会读取 transcript 尾部。解析器支持当前常见的嵌套 `event_msg.payload` 和 `response_item.payload` envelope，并隔离 user、Hook prompt 与旧错误。

由于官方明确声明 transcript 格式可能变化，这个路径只能视为 best-effort。默认关闭是预期行为。

## 开发与验证

```powershell
python -m compileall plugins/codex-auto-retry/scripts
python -m unittest discover -s tests -v
```

Windows 上校验中文 Skill 时必须强制 UTF-8：

```powershell
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
python -X utf8 (Join-Path $codexHome "skills\.system\plugin-creator\scripts\validate_plugin.py") "plugins\codex-auto-retry"
python -X utf8 (Join-Path $codexHome "skills\.system\skill-creator\scripts\quick_validate.py") "plugins\codex-auto-retry\skills\codex-auto-retry"
```

CI 覆盖 Ubuntu/Windows 与 Python 3.10/3.13，包括最新 Stop payload、误报、退避边界、SQLite 并发、隐私和插件静态契约。

## 官方资料

- [Stop Hook 契约](https://learn.chatgpt.com/docs/hooks#stop)
- [Common input fields 与 transcript 稳定性](https://learn.chatgpt.com/docs/hooks#common-input-fields)
- [Hook 审查与信任](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)
- [Plugin-bundled hooks 与 PLUGIN_DATA](https://learn.chatgpt.com/docs/hooks#plugin-bundled-hooks)
- [插件构建规则](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks)
- [Codex 更新日志](https://learn.chatgpt.com/docs/changelog)

## License

MIT
