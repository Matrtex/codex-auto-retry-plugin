# Codex Auto Retry

这个插件为 Codex 提供 `Stop` 可见临时模型错误的有限 continuation fallback。当前版本按 Codex CLI `0.149.0` 的 `turn_id`、`stop_hook_active` 和 `last_assistant_message` 契约实现。

## 能做什么

插件识别带 Codex/OpenAI/model provider 上下文的 high demand、`429`、`5xx`、临时服务、stream 和 network 错误，也支持少数明确的客户端协议强签名；它按当前 turn 限次并有界退避，然后返回 `decision: "block"`，让 Codex 从失败点继续。

认证、权限、billing/quota、上下文长度、无效请求、模型不存在和 policy/safety 错误不会续跑。项目自身的测试、业务服务器或网络错误也不应触发。

## 不能做什么

它不是底层 HTTP/Provider 请求重试器。Provider sampling 最终以 `Err` 结束而没有触发 `Stop` 时，插件无法介入；当前公开 Hook 也没有 `ProviderError` 或 `TurnError` 事件。

`transcript_path` 不是稳定接口，因此默认只使用 `last_assistant_message`。transcript fallback 默认关闭。

## 安装后必做

1. 确认 Python `3.10+` 可用：Windows 使用 `py -3`，Linux/macOS 使用 `python3`。
2. 在 Codex 中运行 `/hooks`，审查并信任这个插件的 Hook。
3. 新建 task，使新版 Hook 与 Skill 重新加载。

插件启用不会自动信任 Hook；定义更新后 hash 会变化，必须重新审查。

从 Git marketplace 更新时运行：

```powershell
codex plugin marketplace upgrade codex-auto-retry
codex plugin add codex-auto-retry@codex-auto-retry
```

随后重新执行 `/hooks` 审查并新建 task。只拉取仓库不会刷新 Codex 已安装 cache。

## 配置

| 变量 | 默认值 | 有效范围或说明 |
| --- | ---: | --- |
| `CODEX_AUTO_RETRY_MAX_ATTEMPTS` | `3` | `0-10`；`0` 表示关闭 |
| `CODEX_AUTO_RETRY_BASE_DELAY` | `8` | 首次退避秒数 |
| `CODEX_AUTO_RETRY_MAX_DELAY` | `60` | 单次总延迟上限，硬上限 `115` 秒 |
| `CODEX_AUTO_RETRY_BACKOFF_FACTOR` | `1.8` | `1-10` |
| `CODEX_AUTO_RETRY_JITTER` | `2` | jitter 后仍不突破最大延迟 |
| `CODEX_AUTO_RETRY_STATE_TTL_SECONDS` | `3600` | `300-604800` |
| `CODEX_AUTO_RETRY_STATE_DIR` | 空 | 优先于 Codex 提供的 `PLUGIN_DATA` |
| `CODEX_AUTO_RETRY_MESSAGE_SCAN_CHARS` | `32768` | `1024-1048576` |
| `CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK` | `0` | 设为 `1` 才启用不稳定 transcript fallback |
| `CODEX_AUTO_RETRY_TRANSCRIPT_TAIL_BYTES` | `262144` | fallback 尾部 bytes，范围 `4096-4194304` |

Hook timeout 是 `120` 秒，插件会把实际等待限制在 `115` 秒内。数字形式的 `Retry-After` 会被尊重；如果无法在配置和 Hook 剩余时间内完整等待，插件会停止 continuation，不会提前重试。

## 状态与安全

状态默认写入 `PLUGIN_DATA/state-v2.sqlite3`。数据库只保存 SHA-256 scope、次数和时间戳，不保存 session、turn 或错误正文；SQLite 事务保证多个 Windows Hook 进程不会绕过次数上限。

`0.1.x` 默认 `%LOCALAPPDATA%\CodexAutoRetry\state.json` 曾保存明文 session 和错误片段，`0.2.0` 不会在旧 task 仍可能运行时自动删除。完全退出 Codex 后可手动删除该文件及同目录 `state.tmp`；使用过自定义 state dir 时也应检查那里。Linux/macOS 默认旧路径是 `${XDG_STATE_HOME:-$HOME/.local/state}/codex-auto-retry/state.json`。

续跑提示会要求 Codex 先核对已有结果并从失败点恢复，不得重复已经成功或结果未知的 push、部署、发送、支付、删除等副作用。这个机制不能提供事务保证；无法确认外部操作状态时应停止并询问用户。

## 排障

- 完全未触发：先检查 `/hooks` 的信任和启用状态。
- Hook 启动失败：确认 `py -3` 或 `python3` 可执行。
- 修改环境变量：完全重启 Codex 后再新建 task。
- Provider/network 错误直接结束 turn：这是当前 `Stop` Hook 的架构边界，不是匹配规则缺失。
- 必须读取旧客户端 transcript：显式设置 `CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK=1`，但该路径只提供 best-effort 兼容。

官方契约：[Stop Hook](https://learn.chatgpt.com/docs/hooks#stop)、[Hook trust](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)、[Plugin-bundled hooks](https://learn.chatgpt.com/docs/hooks#plugin-bundled-hooks)。
