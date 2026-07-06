# Codex Auto Retry Plugin

Codex Auto Retry 是一个本地 Codex 插件，用 `Stop` hook 检测临时性 Codex/OpenAI/model provider 报错，并用有限指数退避自动触发重试。

它主要覆盖这些场景：

- `We're currently experiencing high demand, which may cause temporary errors.`
- `429`、`rate_limit_exceeded`、`too many requests`、`Retry-After`
- `500`、`502`、`503`、`504`、`529`、Cloudflare `520-524`
- `internal server error`、`bad gateway`、`service unavailable`、`gateway timeout`
- SSE/stream 中断、response interrupted、connection reset/closed/aborted
- Codex/OpenAI/API/model provider 上下文里的 network timeout、fetch failed、DNS/TLS/连接错误
- provider 明确提示的 temporary/transient/try again 类错误

它会主动避开这些不可恢复场景：

- `invalid_api_key`、认证失败、`401`、`403`
- billing、余额不足、`insufficient_quota`、`quota exceeded`
- prompt/context/token 长度错误
- `400` invalid request、`404` model not found、unsupported model
- content policy / safety policy 类错误
- 没有 Codex/OpenAI/API/model provider 上下文的项目自身测试超时或业务失败

## 工作原理

插件注册一个 `Stop` hook。配置位于默认 hook 路径 `plugins/codex-auto-retry/hooks/hooks.json`：

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$PLUGIN_ROOT/scripts/auto_retry_stop.py\"",
            "commandWindows": "py -3 ([System.IO.Path]::Combine($env:PLUGIN_ROOT, 'scripts', 'auto_retry_stop.py'))",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

当 hook 检测到临时性错误时，会等待一段退避时间，然后返回：

```json
{
  "decision": "block",
  "reason": "请直接重试上一条用户请求..."
}
```

Codex 会把这个 `reason` 当成继续执行的指令，从而自动重试上一条用户请求。

## 安装

这个仓库是一个 Codex repo marketplace。安装命令：

```powershell
codex plugin marketplace add https://github.com/Matrtex/codex-auto-retry-plugin
codex plugin add codex-auto-retry@codex-auto-retry
```

安装后建议新开一个 Codex thread，让 hook 配置重新加载。

## 配置

通过环境变量调整重试策略：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CODEX_AUTO_RETRY_MAX_ATTEMPTS` | `5` | 同一会话、同一错误指纹的最大重试次数 |
| `CODEX_AUTO_RETRY_BASE_DELAY` | `8` | 首次退避秒数 |
| `CODEX_AUTO_RETRY_MAX_DELAY` | `60` | 单次最大退避秒数 |
| `CODEX_AUTO_RETRY_BACKOFF_FACTOR` | `1.8` | 指数退避倍率 |
| `CODEX_AUTO_RETRY_JITTER` | `2` | 随机抖动秒数 |
| `CODEX_AUTO_RETRY_STATE_DIR` | 系统 state/cache 目录 | 状态文件目录 |
| `CODEX_AUTO_RETRY_TRANSCRIPT_TAIL_BYTES` | `262144` | 读取 transcript 尾部字节数 |

示例：

```powershell
$env:CODEX_AUTO_RETRY_MAX_ATTEMPTS = "8"
$env:CODEX_AUTO_RETRY_BASE_DELAY = "10"
$env:CODEX_AUTO_RETRY_MAX_DELAY = "90"
```

## 边界

这个插件不能拦截所有错误。它只能覆盖 `Stop` hook 能看到的内容：

- `last_assistant_message`
- transcript 尾部里的 assistant/error 片段

如果 Codex 后端请求在形成可 hook 的 turn 之前就硬失败，插件没有入口接管。Codex 自身请求层的内置重试仍然由 Codex 控制。

## 开发

运行测试：

```powershell
python -m unittest discover -s tests
```

校验插件 manifest：

```powershell
python C:\Users\Administrator\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py plugins\codex-auto-retry
```

## License

MIT
