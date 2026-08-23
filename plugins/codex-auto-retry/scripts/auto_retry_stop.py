#!/usr/bin/env python3
"""Codex Stop Hook：识别可见的临时服务错误并进行有限、安全的续跑。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Pattern


PLUGIN_NAME = "codex-auto-retry"
DEFAULT_MAX_ATTEMPTS = 3
MAX_MAX_ATTEMPTS = 10
DEFAULT_BASE_DELAY_SECONDS = 8.0
DEFAULT_MAX_DELAY_SECONDS = 60.0
DEFAULT_BACKOFF_FACTOR = 1.8
DEFAULT_JITTER_SECONDS = 2.0
HOOK_TIMEOUT_SECONDS = 120.0
HOOK_TIMEOUT_RESERVE_SECONDS = 5.0
MAX_SLEEP_SECONDS = HOOK_TIMEOUT_SECONDS - HOOK_TIMEOUT_RESERVE_SECONDS
DEFAULT_STATE_TTL_SECONDS = 60 * 60
DEFAULT_TRANSCRIPT_TAIL_BYTES = 256 * 1024
DEFAULT_MESSAGE_SCAN_CHARS = 32 * 1024
MAX_MESSAGE_SCAN_CHARS = 1024 * 1024
SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Detection:
    category: str
    label: str
    snippet: str
    retry_after_seconds: float | None = None


def compile_patterns(*patterns: str) -> tuple[Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE | re.DOTALL) for pattern in patterns)


RETRYABLE_RULES: tuple[tuple[str, str, tuple[Pattern[str], ...]], ...] = (
    (
        "high_demand",
        "Codex/OpenAI 服务高负载",
        compile_patterns(
            r"\bwe(?:'|’)?re currently experiencing high demand(?:,? which may cause temporary errors?)?",
            r"\b(?:codex|openai|chatgpt|model provider|provider service|upstream service|model service)\b"
            r".{0,120}\b(?:high demand|overloaded|over capacity|at capacity|capacity constraints?)\b",
        ),
    ),
    (
        "rate_limit",
        "模型服务限流",
        compile_patterns(
            r"\b(?:codex|openai|model provider|provider|upstream|responses api)\b"
            r".{0,180}\b(?:429|rate[-_ ]?limit(?:ed|ing|_exceeded)?|too many requests|throttl(?:ed|ing|e))\b",
            r"\b(?:429|rate[-_ ]?limit(?:ed|ing|_exceeded)?|too many requests|throttl(?:ed|ing|e))\b"
            r".{0,180}\b(?:codex|openai|model provider|provider|upstream|responses api)\b",
            r"\b(?:http|status(?: code)?)\s*[:=]?\s*429\b"
            r".{0,160}\b(?:codex|openai|model provider|provider|upstream|responses api)\b",
            r"\brate_limit_exceeded\b.{0,120}\b(?:openai|codex|provider|request|retry-after|error)\b",
        ),
    ),
    (
        "server_error",
        "模型上游服务错误",
        compile_patterns(
            r"\b(?:codex|openai|model provider|provider|upstream)\b.{0,180}"
            r"\b(?:500|502|503|504|520|521|522|523|524|529|internal server error|bad gateway|"
            r"service unavailable|gateway timeout|server had an error|temporar(?:y|ily) unavailable)\b",
            r"\b(?:500|502|503|504|520|521|522|523|524|529|internal server error|bad gateway|"
            r"service unavailable|gateway timeout|server had an error|temporar(?:y|ily) unavailable)\b"
            r".{0,180}\b(?:codex|openai|model provider|provider|upstream)\b",
            r"\b(?:http|status(?: code)?)\s*[:=]?\s*(?:500|502|503|504|520|521|522|523|524|529)\b"
            r".{0,180}\b(?:codex|openai|model provider|provider|upstream)\b",
        ),
    ),
    (
        "stream_error",
        "模型流式响应中断",
        compile_patterns(
            r"\bstream disconnected before completion\b",
            r"\bresponse\.completed\b.{0,100}\b(?:missing|not received|never received)\b",
            r"\b(?:codex|openai|model provider|provider|upstream|sse|eventsource)\b.{0,160}"
            r"\b(?:stream(?:ing)? (?:error|failed|interrupted|ended|closed)|failed to stream|"
            r"response (?:was )?(?:interrupted|truncated|cut off))\b",
            r"\b(?:stream(?:ing)? (?:error|failed|interrupted|ended|closed)|failed to stream|"
            r"response (?:was )?(?:interrupted|truncated|cut off))\b.{0,180}"
            r"\b(?:codex|openai|model provider|provider|upstream|sse|eventsource)\b",
            r"\b(?:connection|socket)\b.{0,100}\b(?:reset|closed|aborted|interrupted|hang up)\b"
            r".{0,120}\b(?:codex|openai|model provider|provider|upstream)\b",
        ),
    ),
    (
        "network_error",
        "模型请求网络错误",
        compile_patterns(
            r"\b(?:codex|openai|model provider|provider|upstream|responses api)\b.{0,180}"
            r"\b(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b",
            r"\b(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b.{0,180}"
            r"\b(?:codex|openai|model provider|provider|upstream|responses api)\b",
            r"\b(?:codex|openai|model provider|provider|upstream|responses api)\b.{0,180}"
            r"\b(?:network (?:error|failure|timeout)|request (?:timed out|timeout|failed)|fetch failed|"
            r"connection (?:timed out|refused|reset|aborted)|tls handshake (?:failed|error)|"
            r"dns.{0,40}(?:timeout|failed|error))\b",
            r"\b(?:network (?:error|failure|timeout)|request (?:timed out|timeout|failed)|fetch failed|"
            r"connection (?:timed out|refused|reset|aborted)|tls handshake (?:failed|error)|"
            r"dns.{0,40}(?:timeout|failed|error))\b.{0,180}"
            r"\b(?:codex|openai|model provider|provider|upstream|responses api)\b",
        ),
    ),
    (
        "temporary_error",
        "模型服务临时错误",
        compile_patterns(
            r"\b(?:codex|openai|model provider|provider|upstream service|model service)\b.{0,180}"
            r"\b(?:temporary|transient)\b.{0,60}\b(?:error|failure|unavailable)\b",
            r"\b(?:temporary|transient)\b.{0,60}\b(?:error|failure|unavailable)\b.{0,180}"
            r"\b(?:codex|openai|model provider|provider|upstream service|model service)\b",
            r"\b(?:codex|openai|model provider|provider|upstream)\b.{0,180}"
            r"\b(?:please retry|try again (?:later|shortly|in a few)|operation timed out|something went wrong)\b",
        ),
    ),
)

NON_RETRYABLE_PATTERNS = compile_patterns(
    r"\bturn_aborted\b",
    r"\b(?:user|the user) (?:interrupted|cancelled|canceled|aborted)\b",
    r"\binterrupted the previous turn on purpose\b",
    r"\b(?:http(?: status)?|status(?: code)?|error(?: code)?|code)\s*[:=]?\s*(?:400|401|403|404)\b",
    r"\b(?:bad request|not found)\b",
    r"\bpermission denied\b",
    r"\bforbidden\b",
    r"\bunauthorized\b",
    r"\binvalid[_ -]?api[_ -]?key\b",
    r"\bincorrect api key\b",
    r"\bauthentication (?:failed|required|error)\b",
    r"\binsufficient[_ -]?quota\b",
    r"\bquota exceeded\b",
    r"\b(?:billing|payment required|usage limit|credits? exhausted)\b",
    r"\bcontext length\b",
    r"\bmaximum context\b",
    r"\bprompt too long\b",
    r"\btoken limit\b",
    r"\bcontent policy\b",
    r"\bsafety policy\b",
    r"\bpolicy violation\b",
    r"\bmalformed\b",
    r"\binvalid request\b",
    r"\bvalidation error\b",
    r"\bunsupported\b",
    r"\bmodel not found\b",
)

QUOTE_EXPLANATION_PATTERNS = compile_patterns(
    r"\b(?:means|refers to|for example|example|how to)\b",
    r"\berror handling\b",
    r"\b(?:one )?possible (?:error )?(?:string|message|response)\b",
    r"\bexact (?:provider )?(?:message|error|string)\b",
    r"\b(?:is|are|was|were) (?:emitted|returned|shown|reported|documented|used)\b",
    r"\b(?:occurs|happens) when\b",
    r"\b(?:can|may|could|should)\b.{0,80}"
    r"\b(?:occur|happen|be handled|be mitigated|be resolved|retry|back off)\b",
    r"\bto (?:resolve|troubleshoot|handle|mitigate|fix)\b",
    r"\bduring (?:scheduled )?maintenance\b",
    r"(?:例如|示例|如何处理|处理方法|解决方法|可通过|应该|可能发生)",
)

GENERIC_EXPLANATION_PATTERNS = compile_patterns(
    r"\b(?:indicates|documentation|guide|typically)\b",
    r"(?:表示|意味着|文档|说明|解释|通常)",
)

ERROR_ENVELOPE_PATTERN = re.compile(
    r"^\s*(?:[#>*`!\-]+\s*)?(?:"
    r"(?:error|fatal)\b|(?:an?\s+)?error occurred\b|something went wrong\b|"
    r"(?:codex|openai(?: api)?|model provider|provider|upstream)\b.{0,100}"
    r"\b(?:error|failed|failure|timed out|timeout|"
    r"rate_limit_exceeded|too many requests|throttled|ECONNRESET|ECONNREFUSED|ECONNABORTED|"
    r"ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b|"
    r"(?:the\s+)?(?:request|response|stream|connection|socket)\b.{0,80}"
    r"\b(?:error|failed|failure|interrupted|disconnected|closed|reset|aborted|timed out|timeout)\b|"
    r"we(?:'|’)?re currently experiencing high demand\b|"
    r"stream disconnected before completion\b|"
    r"response\.completed\b.{0,60}\b(?:missing|not received|never received)\b|"
    r"(?:rate_limit_exceeded|ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b|"
    r"(?:unexpected\s+)?(?:http\s+)?status(?:\s+code)?\s*[:=]?\s*"
    r"(?:429|500|502|503|504|520|521|522|523|524|529)\b)",
    re.IGNORECASE,
)

STRONG_ERROR_ENVELOPE_PATTERN = re.compile(
    r"^\s*(?:[#>*`!\-]+\s*)?(?:"
    r"(?:error|fatal)\b|(?:an?\s+)?error occurred\b|something went wrong\b|"
    r"(?:codex|openai(?: api)?|model provider|provider|upstream)\b.{0,100}"
    r"\b(?:error|failed|failure|timed out|timeout|rate_limit_exceeded|too many requests|throttled|"
    r"ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b|"
    r"(?:the\s+)?(?:request|response|stream|connection|socket)\b.{0,80}"
    r"\b(?:error|failed|failure|interrupted|disconnected|closed|reset|aborted|timed out|timeout)\b|"
    r"(?:rate_limit_exceeded|ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b|"
    r"(?:unexpected\s+)?(?:http\s+)?status(?:\s+code)?\s*[:=]?\s*"
    r"(?:429|500|502|503|504|520|521|522|523|524|529)\b)",
    re.IGNORECASE,
)

RETRY_AFTER_PATTERN = re.compile(r"\bretry-after\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)

CONTROL_EVENT_TYPES = {
    "hook_prompt",
    "user_message",
    "user_input",
    "turn_aborted",
}

SELF_RETRY_PATTERNS = compile_patterns(
    r"Codex Auto Retry (?:检测到|在 Stop 可见消息中识别到)",
    r"请直接重试上一条用户请求",
    r"hook_run_id=.*codex-auto-retry",
    r"<hook_prompt\b",
)

TEXT_CONTAINER_KEYS = {
    "content",
    "text",
    "message",
    "error",
    "reason",
    "status",
    "detail",
    "details",
}

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


def env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    return min(maximum, max(minimum, value))


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u2019", "'")).strip()


def has_any(patterns: tuple[Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def is_self_retry_text(text: str) -> bool:
    return has_any(SELF_RETRY_PATTERNS, text)


def make_snippet(text: str, match: re.Match[str], radius: int = 180) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return normalize_text(text[start:end])


def parse_retry_after(text: str) -> float | None:
    match = RETRY_AFTER_PATTERN.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if value < 0:
        return None
    return value if math.isfinite(value) else math.inf


def classify_retryable_error(text: str) -> Detection | None:
    normalized = normalize_text(text)
    if not normalized or is_self_retry_text(normalized):
        return None

    if has_any(NON_RETRYABLE_PATTERNS, normalized):
        return None

    if not ERROR_ENVELOPE_PATTERN.search(normalized):
        return None

    if has_any(QUOTE_EXPLANATION_PATTERNS, normalized):
        return None

    if has_any(GENERIC_EXPLANATION_PATTERNS, normalized) and not STRONG_ERROR_ENVELOPE_PATTERN.search(
        normalized
    ):
        return None

    for category, label, patterns in RETRYABLE_RULES:
        for pattern in patterns:
            match = pattern.search(normalized)
            if match:
                return Detection(
                    category=category,
                    label=label,
                    snippet=make_snippet(normalized, match),
                    retry_after_seconds=parse_retry_after(normalized),
                )
    return None


def is_control_event(value: Any) -> bool:
    if not isinstance(value, dict):
        return False

    role = str(value.get("role", "")).lower()
    event_type = str(value.get("type", "")).lower()
    if role == "user" or event_type in CONTROL_EVENT_TYPES or "hook" in event_type:
        return True

    for key in ("payload", "item", "message"):
        nested = value.get(key)
        if isinstance(nested, dict) and is_control_event(nested):
            return True

    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return is_self_retry_text(serialized)


def collect_strings(value: Any, *, parent_key: str = "") -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if is_self_retry_text(value):
            return []
        return [value] if parent_key.lower() in TEXT_CONTAINER_KEYS else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(collect_strings(item, parent_key=parent_key))
        return result
    if isinstance(value, dict):
        if is_control_event(value):
            return []
        result: list[str] = []
        for key, item in value.items():
            result.extend(collect_strings(item, parent_key=str(key)))
        return result
    return []


def read_text_tail(path: Path, limit: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - limit)
        handle.seek(start, os.SEEK_SET)
        data = handle.read()
    if start > 0:
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    return data.decode("utf-8", errors="replace")


def extract_transcript_text(transcript_path: str | None, tail_limit: int) -> str:
    if not transcript_path:
        return ""
    path = Path(transcript_path).expanduser()
    if not path.exists() or not path.is_file():
        return ""

    try:
        tail = read_text_tail(path, tail_limit)
    except OSError:
        return ""

    parsed_lines: list[dict[str, Any]] = []
    for line in tail.splitlines()[-160:]:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            parsed_lines.append(payload)

    last_control_index = -1
    for index, item in enumerate(parsed_lines):
        if is_control_event(item):
            last_control_index = index

    # transcript 不是稳定接口，只取最后一个含文本的非控制事件，避免拾取旧错误。
    for item in reversed(parsed_lines[last_control_index + 1 :]):
        fragments = [fragment for fragment in collect_strings(item) if fragment.strip()]
        if fragments:
            return "\n".join(fragments)
    return ""


def state_directory() -> Path:
    for variable in ("CODEX_AUTO_RETRY_STATE_DIR", "PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        value = os.environ.get(variable)
        if value and value.strip():
            return Path(value).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "CodexAutoRetry"
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / PLUGIN_NAME
    return Path.home() / ".local" / "state" / PLUGIN_NAME


def state_database() -> Path:
    return state_directory() / "state-v2.sqlite3"


def retry_scope_hash(session_id: str, turn_id: str, detection: Detection) -> str:
    if turn_id:
        source = f"v2\0{session_id}\0{turn_id}"
    else:
        legacy_error_hash = hashlib.sha256(detection.snippet.encode("utf-8")).hexdigest()
        source = f"legacy\0{session_id}\0{detection.category}\0{legacy_error_hash}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def claim_next_attempt(
    scope_hash: str,
    max_attempts: int,
    ttl_seconds: int,
    *,
    allow_create: bool = True,
) -> int | None:
    if max_attempts <= 0:
        return None

    now = int(time.time())
    try:
        path = state_database()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        try:
            connection.execute(f"PRAGMA busy_timeout = {int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS retry_records (
                    scope_hash TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL CHECK(
                        attempts >= 0 AND attempts <= {MAX_MAX_ATTEMPTS}
                    ),
                    first_seen INTEGER NOT NULL CHECK(first_seen >= 0),
                    last_seen INTEGER NOT NULL CHECK(last_seen >= 0)
                )
                """
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM retry_records WHERE last_seen < ?",
                (now - ttl_seconds,),
            )
            row = connection.execute(
                "SELECT attempts, first_seen, last_seen FROM retry_records WHERE scope_hash = ?",
                (scope_hash,),
            ).fetchone()

            if row is None and not allow_create:
                connection.commit()
                return None

            if row:
                attempts, first_seen, last_seen = row
                if (
                    type(attempts) is not int
                    or type(first_seen) is not int
                    or type(last_seen) is not int
                    or attempts < 0
                    or attempts > MAX_MAX_ATTEMPTS
                    or first_seen < 0
                    or last_seen < 0
                ):
                    connection.commit()
                    return None
            else:
                attempts = 0
                first_seen = now
            if attempts >= max_attempts:
                connection.execute(
                    "UPDATE retry_records SET last_seen = ? WHERE scope_hash = ?",
                    (now, scope_hash),
                )
                connection.commit()
                return None

            attempts += 1
            connection.execute(
                """
                INSERT INTO retry_records (scope_hash, attempts, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_hash) DO UPDATE SET
                    attempts = excluded.attempts,
                    last_seen = excluded.last_seen
                """,
                (scope_hash, attempts, first_seen, now),
            )
            connection.commit()
            return attempts
        finally:
            connection.close()
    except (OSError, OverflowError, sqlite3.Error, TypeError, ValueError):
        # 状态不可用时不自动续跑，避免失去次数上限。
        return None


def configured_max_delay() -> float:
    return env_float(
        "CODEX_AUTO_RETRY_MAX_DELAY",
        DEFAULT_MAX_DELAY_SECONDS,
        minimum=0.0,
        maximum=MAX_SLEEP_SECONDS,
    )


def retry_delay(attempt: int, retry_after_seconds: float | None = None) -> float:
    base = env_float(
        "CODEX_AUTO_RETRY_BASE_DELAY",
        DEFAULT_BASE_DELAY_SECONDS,
        minimum=0.0,
        maximum=MAX_SLEEP_SECONDS,
    )
    cap = configured_max_delay()
    factor = env_float(
        "CODEX_AUTO_RETRY_BACKOFF_FACTOR",
        DEFAULT_BACKOFF_FACTOR,
        minimum=1.0,
        maximum=10.0,
    )
    jitter = env_float(
        "CODEX_AUTO_RETRY_JITTER",
        DEFAULT_JITTER_SECONDS,
        minimum=0.0,
        maximum=MAX_SLEEP_SECONDS,
    )
    if cap <= 0:
        return 0.0

    exponential = min(cap, base * (factor ** max(0, attempt - 1)))
    retry_after = retry_after_seconds if retry_after_seconds is not None else 0.0
    bounded_retry_after = min(cap, max(0.0, retry_after))
    delay = max(exponential, bounded_retry_after)
    if jitter > 0:
        delay += random.uniform(0.0, jitter)
    return min(cap, MAX_SLEEP_SECONDS, delay)


def fit_delay_to_hook_budget(delay: float, started_at: float) -> float:
    elapsed = max(0.0, time.monotonic() - started_at)
    remaining = max(0.0, MAX_SLEEP_SECONDS - elapsed)
    return min(delay, remaining)


def build_retry_reason(detection: Detection, attempt: int, max_attempts: int, delay: float) -> str:
    return (
        f"Codex Auto Retry 在 Stop 可见消息中识别到临时错误：{detection.label} "
        f"({detection.category})。这是当前 turn 的安全续跑 {attempt}/{max_attempts}，已等待 {delay:.1f} 秒。\n\n"
        "继续完成当前用户目标，但先检查工作区、工具结果和外部系统中已经成功的步骤，并从失败点恢复。"
        "不要重复已经成功的写入、提交、推送、部署、发送、支付、删除或其他有副作用的操作。"
        "只有确认失败步骤尚未生效或该步骤可安全幂等重试时，才重新执行；"
        "如果无法确认副作用是否已经发生，停止并向用户说明当前状态和需要确认的事项。"
        "若新错误属于认证、权限、配额、上下文长度、无效请求或策略限制，停止自动续跑。"
    )


def read_hook_payload() -> dict[str, Any] | None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def main() -> int:
    started_at = time.monotonic()
    payload = read_hook_payload()
    if payload is None or payload.get("hook_event_name") != "Stop":
        return 0

    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id", "")
    stop_hook_active = payload.get("stop_hook_active")
    last_message_value = payload.get("last_assistant_message")
    transcript_path_value = payload.get("transcript_path")

    if not isinstance(session_id, str) or not session_id.strip():
        return 0
    if not isinstance(turn_id, str):
        return 0
    if stop_hook_active is not None and not isinstance(stop_hook_active, bool):
        return 0
    if stop_hook_active is not None and not turn_id.strip():
        return 0
    if last_message_value is not None and not isinstance(last_message_value, str):
        return 0
    if transcript_path_value is not None and not isinstance(transcript_path_value, str):
        return 0

    scan_chars = env_int(
        "CODEX_AUTO_RETRY_MESSAGE_SCAN_CHARS",
        DEFAULT_MESSAGE_SCAN_CHARS,
        minimum=1024,
        maximum=MAX_MESSAGE_SCAN_CHARS,
    )
    last_message = (last_message_value or "")[-scan_chars:]
    detection = classify_retryable_error(last_message)

    if detection is None and not last_message.strip() and env_bool("CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK"):
        tail_limit = env_int(
            "CODEX_AUTO_RETRY_TRANSCRIPT_TAIL_BYTES",
            DEFAULT_TRANSCRIPT_TAIL_BYTES,
            minimum=4096,
            maximum=4 * 1024 * 1024,
        )
        transcript_text = extract_transcript_text(transcript_path_value, tail_limit)
        detection = classify_retryable_error(transcript_text[-scan_chars:])
    if detection is None:
        return 0

    if (
        detection.retry_after_seconds is not None
        and detection.retry_after_seconds > configured_max_delay()
    ):
        # 无法在用户配置和 Hook timeout 内遵守 Retry-After 时，不提前重试。
        return 0

    max_attempts = env_int(
        "CODEX_AUTO_RETRY_MAX_ATTEMPTS",
        DEFAULT_MAX_ATTEMPTS,
        minimum=0,
        maximum=MAX_MAX_ATTEMPTS,
    )
    ttl_seconds = env_int(
        "CODEX_AUTO_RETRY_STATE_TTL_SECONDS",
        DEFAULT_STATE_TTL_SECONDS,
        minimum=300,
        maximum=7 * 24 * 60 * 60,
    )
    scope_hash = retry_scope_hash(session_id, turn_id.strip(), detection)
    attempt = claim_next_attempt(
        scope_hash,
        max_attempts,
        ttl_seconds,
        allow_create=stop_hook_active is not True,
    )
    if attempt is None:
        return 0

    delay = fit_delay_to_hook_budget(
        retry_delay(attempt, detection.retry_after_seconds),
        started_at,
    )
    if detection.retry_after_seconds is not None and delay < detection.retry_after_seconds:
        return 0
    if not env_bool("CODEX_AUTO_RETRY_DISABLE_SLEEP") and delay > 0:
        time.sleep(delay)

    print(
        json.dumps(
            {
                "decision": "block",
                "reason": build_retry_reason(detection, attempt, max_attempts, delay),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
