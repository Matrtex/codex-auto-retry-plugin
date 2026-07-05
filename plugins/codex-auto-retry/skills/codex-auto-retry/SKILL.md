---
name: codex-auto-retry
description: Explain or tune the Codex Auto Retry plugin, including retryable error categories, environment variables, and Stop hook limitations.
---

# Codex Auto Retry

Use this skill when the user asks how the Codex Auto Retry plugin works or how to tune it.

## Behavior

The plugin installs a `Stop` hook. When a Codex turn stops, the hook inspects:

- `last_assistant_message`
- relevant assistant/error fragments from the transcript tail

It retries only when the text looks like a transient Codex/OpenAI/model-provider failure:

- high demand or overloaded service
- `429` rate limiting or throttling
- `5xx` server or upstream errors
- interrupted SSE/streaming response
- network, DNS, TLS, timeout, or connection reset errors with model/request context
- temporary "try again" style provider errors

It intentionally does not retry authentication, permission, billing/quota, context length, invalid request, unsupported model, not found, or policy/safety errors.

## Tuning

Environment variables:

- `CODEX_AUTO_RETRY_MAX_ATTEMPTS`: default `5`
- `CODEX_AUTO_RETRY_BASE_DELAY`: default `8`
- `CODEX_AUTO_RETRY_MAX_DELAY`: default `60`
- `CODEX_AUTO_RETRY_BACKOFF_FACTOR`: default `1.8`
- `CODEX_AUTO_RETRY_JITTER`: default `2`
- `CODEX_AUTO_RETRY_STATE_DIR`: custom state directory
- `CODEX_AUTO_RETRY_TRANSCRIPT_TAIL_BYTES`: default `262144`

## Limitation

This plugin cannot intercept failures that happen before Codex creates a hookable turn. It covers failures visible to the `Stop` hook payload or transcript.
