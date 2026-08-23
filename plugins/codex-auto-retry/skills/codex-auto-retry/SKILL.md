---
name: codex-auto-retry
description: 说明、诊断或调整 Codex Auto Retry 插件；适用于 Stop Hook 可见临时错误、重试参数、安全边界和 Codex 0.149.0 Hook 契约，不用于承诺拦截 Provider 网络层硬失败。
---

# Codex Auto Retry

## 能力边界

这个插件是 `Stop` Hook fallback，不是 Codex 底层 HTTP 或 Provider 请求重试器。

- 主数据源是 `last_assistant_message`。
- 当 Provider、network、timeout 或 SSE 错误最终以 sampling `Err` 结束且没有触发 `Stop` 时，插件无法介入。
- `transcript_path` 不是稳定接口；兼容回退默认关闭，只能显式开启。
- Codex 自身的请求恢复和重试仍由客户端负责。

不要向用户承诺它能捕获所有 `429`、`5xx`、timeout 或 stream hard failure。

## 判定与续跑

除少数明确的客户端协议强签名外，插件只在消息同时具有运行时 error envelope、临时失败形态和 Codex/OpenAI/model provider 上下文时续跑，覆盖：

- 官方 high-demand 提示
- Provider `429`、`rate_limit_exceeded`、`Retry-After`
- Provider `500`、`502`、`503`、`504`、`520-524`、`529`
- 明确的 model stream 中断
- 带 Provider 上下文的 DNS、TLS、timeout 和连接错误

认证、权限、billing/quota、上下文长度、无效请求、模型不存在和 policy/safety 错误必须停止。

Codex CLI 0.149.0 的 `Stop` payload 包含 `turn_id`、`stop_hook_active` 和 `last_assistant_message`。插件按 `session_id + turn_id` 对整个 turn 统一计数；同一 turn 的 continuation 共享预算，错误文本或类别变化不会重置次数。

`decision: "block"` 会创建新的 continuation prompt。这个 prompt 必须从失败点恢复，不得盲目重放已经成功或结果未知的写入、push、部署、发送、支付、删除等副作用。插件只能降低重复风险，不能提供事务保证。

## 配置

- `CODEX_AUTO_RETRY_MAX_ATTEMPTS`：默认 `3`，范围 `0-10`；`0` 表示关闭续跑。
- `CODEX_AUTO_RETRY_BASE_DELAY`：默认 `8` 秒。
- `CODEX_AUTO_RETRY_MAX_DELAY`：默认 `60` 秒，实际硬上限 `115` 秒。
- `CODEX_AUTO_RETRY_BACKOFF_FACTOR`：默认 `1.8`，范围 `1-10`。
- `CODEX_AUTO_RETRY_JITTER`：默认 `2` 秒；抖动后仍不会突破最大延迟。
- `CODEX_AUTO_RETRY_STATE_TTL_SECONDS`：默认 `3600` 秒，范围 `300-604800`。
- `CODEX_AUTO_RETRY_STATE_DIR`：显式状态目录，优先于 `PLUGIN_DATA`。
- `CODEX_AUTO_RETRY_MESSAGE_SCAN_CHARS`：默认 `32768`，限制 assistant message 扫描长度。
- `CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK`：默认关闭；设为 `1` 才读取 transcript。
- `CODEX_AUTO_RETRY_TRANSCRIPT_TAIL_BYTES`：fallback 开启时默认读取尾部 `262144` bytes。

数字形式的 `Retry-After` 是最低等待时间；超过配置上限或 Hook 剩余预算时停止 continuation，不得提前重试。

状态默认写入 Codex 提供的 `PLUGIN_DATA`，使用 SQLite 原子计数，只落盘 SHA-256 scope、次数和时间戳，不保存 session、turn 或错误正文。

`stop_hook_active=true` 但当前 turn 状态缺失或已过期时必须停止续跑，不能重新创建预算。`0.1.x` 的旧 `state.json` 可能仍含明文 session 和错误片段，应在完全退出旧 task 后手动清理。

## 排障

如果插件没有触发：

1. 在 Codex 中运行 `/hooks`，确认变更后的 Hook 已审查、信任且未禁用。
2. 确认系统具备 Python 3.10+；Windows 的 Hook 使用 `py -3`，Linux/macOS 使用 `python3`。
3. 确认错误产生了 `Stop`。若 turn 直接以 Provider sampling error 失败，当前公开 Hook 契约没有可用入口。
4. 修改环境变量后完全重启 Codex；安装或更新插件后重新执行 `/hooks`，再新建 task。

官方契约见 [Stop Hook](https://learn.chatgpt.com/docs/hooks#stop) 和 [Plugin-bundled hooks](https://learn.chatgpt.com/docs/hooks#plugin-bundled-hooks)。
