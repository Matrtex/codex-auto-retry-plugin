#!/usr/bin/env python3
"""Codex Stop hook: 检测临时性模型/服务错误并触发有限自动重试。"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PLUGIN_NAME = "codex-auto-retry"
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY_SECONDS = 8.0
DEFAULT_MAX_DELAY_SECONDS = 60.0
DEFAULT_BACKOFF_FACTOR = 1.8
DEFAULT_JITTER_SECONDS = 2.0
DEFAULT_STATE_TTL_SECONDS = 60 * 60
DEFAULT_TRANSCRIPT_TAIL_BYTES = 256 * 1024


@dataclass(frozen=True)
class Detection:
    category: str
    pattern: str
    snippet: str


RETRYABLE_PATTERNS: list[tuple[str, str, list[str]]] = [
    (
        "high_demand",
        "Codex/OpenAI 服务高负载",
        [
            r"we['’]?re currently experiencing high demand",
            r"currently experiencing high demand",
            r"\bhigh demand\b",
            r"\bat capacity\b",
            r"\boverloaded\b",
            r"\bover capacity\b",
            r"capacity constraints?",
            r"heavy load",
        ],
    ),
    (
        "rate_limit",
        "限流或排队",
        [
            r"\b429\b",
            r"\brate[- ]?limit(?:ed|ing|s)?\b",
            r"\brate_limit(?:ed|_exceeded)?\b",
            r"\btoo many requests\b",
            r"\bthrottl(?:ed|ing|e)\b",
            r"\bretry-after\b",
            r"requests per minute",
            r"tokens per minute",
        ],
    ),
    (
        "server_error",
        "上游服务错误",
        [
            r"\b5(?:00|02|03|04|20|21|22|23|24|29)\b",
            r"\binternal server error\b",
            r"\bbad gateway\b",
            r"\bservice unavailable\b",
            r"\bgateway timeout\b",
            r"\bupstream (?:server )?error\b",
            r"\bserver had an error\b",
            r"\btemporar(?:y|ily) unavailable\b",
            r"\bmodel provider error\b",
        ],
    ),
    (
        "stream_error",
        "流式响应中断",
        [
            r"\bstream(?:ing)? (?:error|failed|interrupted|ended|closed)\b",
            r"\bfailed to stream\b",
            r"\bsse\b.*\b(error|failed|closed|interrupted)\b",
            r"\beventsource\b.*\b(error|failed|closed)\b",
            r"\bresponse (?:was )?(?:interrupted|truncated|cut off)\b",
            r"\bconnection (?:was )?(?:reset|closed|aborted|interrupted)\b",
            r"\bsocket hang up\b",
        ],
    ),
    (
        "network_error",
        "网络或连接错误",
        [
            r"\bnetwork (?:error|failure|timeout)\b",
            r"\brequest (?:timed out|timeout|failed)\b",
            r"\bfetch failed\b",
            r"\bconnection (?:timed out|refused|reset|aborted)\b",
            r"\bECONNRESET\b",
            r"\bECONNREFUSED\b",
            r"\bECONNABORTED\b",
            r"\bETIMEDOUT\b",
            r"\bEAI_AGAIN\b",
            r"\bENOTFOUND\b",
            r"\bTLS handshake\b",
            r"\bDNS\b.*\b(?:timeout|failed|error)\b",
        ],
    ),
    (
        "temporary_error",
        "临时性错误",
        [
            r"\btemporary errors?\b",
            r"\btransient (?:error|failure)\b",
            r"\btry again (?:later|shortly|in a few)",
            r"\bplease retry\b",
            r"\boperation timed out\b",
            r"\bsomething went wrong\b.*\btry again\b",
            r"\ban error occurred\b.*\btry again\b",
        ],
    ),
]

NON_RETRYABLE_PATTERNS: list[str] = [
    r"\b400\b",
    r"\b401\b",
    r"\b403\b",
    r"\b404\b",
    r"\bpermission denied\b",
    r"\bforbidden\b",
    r"\bunauthorized\b",
    r"\bnot found\b",
    r"\binvalid[_ -]?api[_ -]?key\b",
    r"\bincorrect api key\b",
    r"\bauthentication (?:failed|required|error)\b",
    r"\binsufficient[_ -]?quota\b",
    r"\bquota exceeded\b",
    r"\bbilling\b",
    r"\bpayment required\b",
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
]

SERVICE_CONTEXT_PATTERNS: list[str] = [
    r"\bcodex\b",
    r"\bopenai\b",
    r"\bchatgpt\b",
    r"\bmodel\b",
    r"\bprovider\b",
    r"\bapi\b",
    r"\brequest\b",
    r"\bresponse\b",
    r"\bstream\b",
    r"\bsse\b",
    r"\bupstream\b",
    r"\bserver\b",
]

SERVICE_CONTEXT_REQUIRED = {
    "network_error",
    "stream_error",
    "temporary_error",
}

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u2019", "'")).strip()


def has_any(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def make_snippet(text: str, match: re.Match[str], radius: int = 180) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return normalize_text(text[start:end])


def classify_retryable_error(text: str) -> Detection | None:
    normalized = normalize_text(text)
    if not normalized:
        return None

    if has_any(NON_RETRYABLE_PATTERNS, normalized):
        return None

    for category, label, patterns in RETRYABLE_PATTERNS:
        for pattern in patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if not match:
                continue
            if category in SERVICE_CONTEXT_REQUIRED and not has_any(SERVICE_CONTEXT_PATTERNS, normalized):
                continue
            return Detection(category=category, pattern=label, snippet=make_snippet(normalized, match))
    return None


def collect_strings(value: Any, *, parent_key: str = "") -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if parent_key.lower() in {"content", "text", "message", "error", "reason", "status", "type"}:
            return [value]
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(collect_strings(item, parent_key=parent_key))
        return result
    if isinstance(value, dict):
        role = str(value.get("role", "")).lower()
        event_type = str(value.get("type", "")).lower()
        if role == "user" or event_type in {"user_message", "user_input"}:
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
        handle.seek(max(0, size - limit), os.SEEK_SET)
        return handle.read().decode("utf-8", errors="replace")


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

    fragments: list[str] = []
    for line in tail.splitlines()[-120:]:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            if re.search(r"\b(error|failed|retry|429|5\d\d|high demand)\b", stripped, re.IGNORECASE):
                fragments.append(stripped)
            continue
        fragments.extend(collect_strings(payload))

    return "\n".join(fragment for fragment in fragments if fragment)


def state_directory() -> Path:
    override = os.environ.get("CODEX_AUTO_RETRY_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "CodexAutoRetry"
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / PLUGIN_NAME
    return Path.home() / ".local" / "state" / PLUGIN_NAME


def state_file() -> Path:
    return state_directory() / "state.json"


def load_state() -> dict[str, Any]:
    path = state_file()
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"records": {}}
    if not isinstance(payload, dict):
        return {"records": {}}
    records = payload.get("records")
    if not isinstance(records, dict):
        payload["records"] = {}
    return payload


def save_state(payload: dict[str, Any]) -> None:
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    tmp_path.replace(path)


def retry_key(session_id: str, detection: Detection) -> str:
    fingerprint_source = f"{session_id}\n{detection.category}\n{detection.snippet[:400]}"
    digest = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:24]
    return f"{session_id}:{detection.category}:{digest}"


def next_attempt(session_id: str, detection: Detection, max_attempts: int, ttl_seconds: int) -> int | None:
    if max_attempts <= 0:
        return None

    now = int(time.time())
    payload = load_state()
    records = payload.setdefault("records", {})
    stale = [
        key
        for key, value in records.items()
        if not isinstance(value, dict) or now - int(value.get("last_seen", 0)) > ttl_seconds
    ]
    for key in stale:
        records.pop(key, None)

    key = retry_key(session_id, detection)
    record = records.get(key)
    if not isinstance(record, dict):
        record = {"attempts": 0, "first_seen": now}
    attempts = int(record.get("attempts", 0))
    if attempts >= max_attempts:
        record["last_seen"] = now
        records[key] = record
        save_state(payload)
        return None

    attempts += 1
    record.update(
        {
            "attempts": attempts,
            "last_seen": now,
            "category": detection.category,
            "pattern": detection.pattern,
            "snippet": detection.snippet[:400],
        }
    )
    records[key] = record
    save_state(payload)
    return attempts


def retry_delay(attempt: int) -> float:
    base = env_float("CODEX_AUTO_RETRY_BASE_DELAY", DEFAULT_BASE_DELAY_SECONDS)
    cap = env_float("CODEX_AUTO_RETRY_MAX_DELAY", DEFAULT_MAX_DELAY_SECONDS)
    factor = env_float("CODEX_AUTO_RETRY_BACKOFF_FACTOR", DEFAULT_BACKOFF_FACTOR)
    jitter = env_float("CODEX_AUTO_RETRY_JITTER", DEFAULT_JITTER_SECONDS)
    delay = min(cap, base * (factor ** max(0, attempt - 1)))
    if jitter > 0:
        delay += random.uniform(0, jitter)
    return delay


def build_retry_reason(detection: Detection, attempt: int, max_attempts: int, delay: float) -> str:
    return (
        f"Codex Auto Retry 检测到临时性模型/服务错误：{detection.pattern} "
        f"({detection.category})，自动重试 {attempt}/{max_attempts}，已退避等待 {delay:.1f} 秒。\n\n"
        "请直接重试上一条用户请求，保持原目标、约束和工作目录。"
        "如果已有部分工作完成，优先从失败点继续；如果无法判断失败点，就从上一条用户请求重新执行。"
        "不要向用户索要确认，除非新的错误显示为认证、权限、余额、上下文长度、无效请求或策略类不可恢复问题。"
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    last_message = str(payload.get("last_assistant_message") or "")
    transcript_text = extract_transcript_text(
        payload.get("transcript_path"),
        env_int("CODEX_AUTO_RETRY_TRANSCRIPT_TAIL_BYTES", DEFAULT_TRANSCRIPT_TAIL_BYTES),
    )
    candidate_text = "\n".join(part for part in [last_message, transcript_text] if part)
    detection = classify_retryable_error(candidate_text)
    if detection is None:
        return 0

    session_id = str(payload.get("session_id") or "unknown-session")
    max_attempts = env_int("CODEX_AUTO_RETRY_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
    attempt = next_attempt(
        session_id,
        detection,
        max_attempts,
        env_int("CODEX_AUTO_RETRY_STATE_TTL_SECONDS", DEFAULT_STATE_TTL_SECONDS),
    )
    if attempt is None:
        return 0

    delay = retry_delay(attempt)
    if os.environ.get("CODEX_AUTO_RETRY_DISABLE_SLEEP") != "1" and delay > 0:
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
