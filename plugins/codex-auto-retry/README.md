# Codex Auto Retry

这个插件通过 `Stop` hook 为 Codex 提供有限自动重试能力。

核心脚本：`scripts/auto_retry_stop.py`

hook 配置：`hooks.json`

主要能力：

- 检测 high demand、429、5xx、stream/network/temporary provider 错误
- 使用指数退避和最大次数限制避免无限循环
- 过滤认证、余额、权限、上下文长度、策略等不可恢复错误

详细安装和配置见仓库根目录 `README.md`。
