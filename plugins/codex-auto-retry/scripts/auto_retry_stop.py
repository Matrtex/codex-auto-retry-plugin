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
from datetime import timezone
from email.utils import parsedate_to_datetime
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


RETRYABLE_HTTP_STATUSES = {408, 409, 429}
HTTP_STATUS_PATTERNS = compile_patterns(
    r"(?<![\w])\"?(?:http(?:/\d(?:\.\d)?)?(?:[\s_-]+status)?|"
    r"status(?:[\s_-]+code)?|error[\s_-]+code)\"?\s*[:=]?\s*(\d{3})\b",
    r"(?<![\w])\"?response[\s_-]+code\"?\s*[:=]?\s*(\d{3})"
    r"(?=\s*(?:$|[,;:.)}\]\-\{\[\"']|bad request|unauthorized|forbidden|not found|"
    r"method not allowed|request timeout|conflict|unprocessable entity|too many requests|"
    r"internal server error|bad gateway|service unavailable|gateway timeout|overloaded|"
    r"(?:(?:[a-z0-9]+[_-])+(?:error|exceeded|limit|unavailable|timeout|conflict|locked)|"
    r"request_timed_out|bad_gateway|connection_timed_out)\b))",
    r"\b(?:request failed(?:\s+with|\s*[:=])|failed with|returned|responded with)\s*[:=]?\s*"
    r"(?:http(?:[\s_-]+status)?\s*)?(\d{3})(?=\s*(?:$|[,;:.)}\]\-\{\[\"']|"
    r"bad request|unauthorized|forbidden|not found|request timeout|conflict|unprocessable entity|"
    r"too many requests|internal server error|bad gateway|service unavailable|gateway timeout|"
    r"overloaded|(?:(?:[a-z0-9]+[_-])+(?:error|exceeded|limit|unavailable|timeout|conflict|locked)|"
    r"request_timed_out|bad_gateway|connection_timed_out)\b))",
)
X_SHOULD_RETRY_FALSE_PATTERN = re.compile(
    r"\bx[\s_-]+should[\s_-]+retry\b[\"']?\s*[:=]\s*[\"']?"
    r"(?:false|0|no)[\"']?(?!\w)",
    re.IGNORECASE,
)
PROVIDER_CONTEXT_PATTERN = re.compile(
    r"\b(?:codex|openai|chatgpt|model provider|model service|responses api|"
    r"api\.openai\.com|chatgpt\.com)\b",
    re.IGNORECASE,
)
RUNTIME_ERROR_PREFIX_FRAGMENT = (
    r"^\s*(?:[#>*`!\-]+\s*)?(?:(?:error|fatal)\s*:\s*)?"
)
GENERIC_PROVIDER_ERROR_PREFIX_FRAGMENT = (
    RUNTIME_ERROR_PREFIX_FRAGMENT + r"provider\s+(?:error|failure)\s*:\s*"
)
TRAILING_PROVIDER_RELATION_FRAGMENT = (
    r"\b(?:(?:from|by|to)\b|(?:while|when)\b.{0,40}\b"
    r"(?:calling|contacting|waiting for|reading from|sending to|connecting to)\b)"
)
PROVIDER_CONNECTION_FAILURE_FRAGMENT = (
    r"(?:(?:could not|cannot|unable to|was unable to)\s+(?:be\s+)?"
    r"(?:contact(?:ed)?|connect(?:ed)?(?:\s+to)?)|"
    r"was not\s+(?:contacted|connected))"
)
PROVIDER_RUNTIME_ENVELOPE_FRAGMENT = (
    r"(?:codex(?:\s+model provider)?|openai(?:\s+(?:api|model provider))?|"
    r"chatgpt|model provider|model service|responses api)\b(?:"
    r"\s*(?:(?::|-)\s*)?(?:error|fatal)\b(?=\s*(?::|-|code\b|[45]\d{2}\b))|"
    r".{0,80}\b(?:request|response|stream|connection|operation|call)\b.{0,40}"
    r"\b(?:error|failed|failure|timed out|timeout)\b|"
    r".{0,80}\b(?:returned|responded(?: with)?|received|got|failed|timed out)\b|"
    r".{0,80}\b"
    + PROVIDER_CONNECTION_FAILURE_FRAGMENT
    + r"\b)"
)
RUNTIME_STATUS_ENVELOPE_PATTERN = re.compile(
    r"^\s*(?:[#>*`!\-]+\s*)?(?:"
    r"(?:error|fatal)\s*:\s*(?:codex|openai(?: api)?|chatgpt|model provider|model service|responses api)\b"
    r".{0,60}\b(?:http(?:/\d(?:\.\d)?)?(?:[\s_-]+status)?|"
    r"status(?:[\s_-]+code)?|error[\s_-]+code|response[\s_-]+code)\b|"
    r"(?:codex|openai(?: api)?|chatgpt|model provider|model service|responses api)\b\s*"
    r"(?:http(?:/\d(?:\.\d)?)?(?:[\s_-]+status)?|status(?:[\s_-]+code)?|"
    r"error[\s_-]+code|response[\s_-]+code)\b\s*[:=]|"
    r"(?:(?:error|fatal)\s*:\s*)?"
    r"(?:codex|openai(?: api)?|chatgpt|model provider|model service|responses api)\b.{0,100}"
    r"\b(?:failed|returned|responded|received|got)\b)",
    re.IGNORECASE,
)
EXPLICIT_PROVIDER_ENDPOINT_PATTERN = re.compile(
    r"\b(?:api\.openai\.com|chatgpt\.com)\b",
    re.IGNORECASE,
)
LOCAL_TARGET_FRAGMENT = (
    r"(?:localhost|127\.0\.0\.1|::1|docker|mcp(?: server)?|database|postgres(?:ql)?|"
    r"mysql|redis|kubernetes|kubectl|github|gitlab|stripe)"
)
LOCAL_SYSTEM_ERROR_PATTERNS = compile_patterns(
    rf"(?<![\w]){LOCAL_TARGET_FRAGMENT}(?![\w])"
    r"(?:(?!\b(?:codex|openai|chatgpt|model provider|responses api)\b).){0,100}"
    r"\b(?:connection failed|connection refused|connection reset|request timed out|timed out|"
    r"timeout|error while reading (?:the )?server response|service unavailable|server error|"
    r"network error|(?:returned|responded with|status(?: code)?)\s*[:=]?\s*"
    r"(?:http\s*)?[45]\d{2})\b",
    r"\b(?:connection failed|connection refused|connection reset|request timed out|timed out|"
    r"error while reading (?:the )?server response|network error)\b\s*"
    rf"(?:to|for|with|from|by|:)\s*.{{0,100}}(?<![\w]){LOCAL_TARGET_FRAGMENT}(?![\w])",
)
PROVIDER_NEGATION_PATTERNS = compile_patterns(
    r"\b(?:codex|openai|chatgpt|model provider|responses api)\b.{0,100}"
    r"\b(?:(?:was not|is not|not|was never)\s+(?:called|involved)|"
    r"is (?:healthy|unrelated)|(?:was|is|were|are) not affected|completed normally|succeeded)\b",
    r"\b(?:not called|not involved|unrelated|before calling)\b.{0,100}"
    r"\b(?:codex|openai|chatgpt|model provider|responses api)\b",
)


RETRYABLE_RULES: tuple[tuple[str, str, tuple[Pattern[str], ...]], ...] = (
    (
        "high_demand",
        "Codex/OpenAI 服务高负载",
        compile_patterns(
            r"\bwe(?:'|’)?re currently experiencing high demand(?:,? which may cause temporary errors?)?",
            r"\bselected model is at capacity\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b"
            r".{0,120}\b(?:high demand|overloaded|over capacity|at capacity|capacity constraints?)\b",
        ),
    ),
    (
        "rate_limit",
        "模型服务限流",
        compile_patterns(
            GENERIC_PROVIDER_ERROR_PREFIX_FRAGMENT
            + r".{0,100}\b(?:429|rate[-_ ]?limit(?:ed|ing|_exceeded)?|"
            r"rate[_ -]?limit[_ -]?error|too many requests|throttled)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b"
            r".{0,180}\b(?:rate[-_ ]?limit(?:ed|ing|_exceeded)?|rate[_ -]?limit[_ -]?error|"
            r"too many requests|throttl(?:ed|ing|e))\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:rate[-_ ]?limit(?:ed|ing|_exceeded)?|rate[_ -]?limit[_ -]?error|"
            r"too many requests|throttl(?:ed|ing|e))\b"
            r".{0,100}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,100}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:http|status(?: code)?)\s*[:=]?\s*429\b.{0,80}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,80}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
            r"\brate_limit_exceeded\b.{0,120}\b(?:openai|codex|model provider|model service|request|retry-after|error)\b",
        ),
    ),
    (
        "server_error",
        "模型上游服务错误",
        compile_patterns(
            GENERIC_PROVIDER_ERROR_PREFIX_FRAGMENT
            + r".{0,100}\b(?:5\d{2}|internal server error|bad gateway|service unavailable|"
            r"gateway timeout|server_error|internal_error|internal[_ -]?server[_ -]?error|"
            r"service_unavailable|overloaded_error)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,180}"
            r"\b(?:internal server error|bad gateway|"
            r"service unavailable|gateway timeout|server had an error|temporar(?:y|ily) unavailable)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,180}"
            r"\b(?:server_error|internal_error|internal[_ -]?server[_ -]?error|"
            r"service_unavailable|temporarily_unavailable|"
            r"overloaded_error)\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:internal server error|bad gateway|"
            r"service unavailable|gateway timeout|server had an error|temporar(?:y|ily) unavailable)\b"
            r".{0,100}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,100}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:http|status(?:[\s_-]+code)?)\s*[:=]?\s*5\d{2}\b.{0,80}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,80}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
        ),
    ),
    (
        "stream_error",
        "模型流式响应中断",
        compile_patterns(
            GENERIC_PROVIDER_ERROR_PREFIX_FRAGMENT
            + r".{0,100}\b(?:stream_error|stream_disconnected|response_stream_disconnected|"
            r"stream(?:ing)? (?:failed|interrupted|ended|closed))\b",
            r"\bstream disconnected before completion\b",
            r"\bresponse stream disconnected\b",
            r"^\s*(?:error\s*:\s*)?response stream (?:connection failed|failed|interrupted)\b",
            r"\bstream closed before response\.completed\b",
            r"\bwebsocket closed by server before response\.completed\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,180}"
            r"\b(?:stream_error|stream_disconnected|response_stream_disconnected)\b",
            r"\bresponse\.completed\b.{0,100}\b(?:missing|not received|never received)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,160}"
            r"\b(?:stream(?:ing)? (?:error|failed|interrupted|ended|closed)|failed to stream|"
            r"response (?:was )?(?:interrupted|truncated|cut off))\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:stream(?:ing)? (?:error|failed|interrupted|ended|closed)|failed to stream|"
            r"response (?:was )?(?:interrupted|truncated|cut off))\b.{0,180}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,100}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:connection|socket)\b.{0,100}\b(?:reset|closed|aborted|interrupted|hang up)\b.{0,80}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,80}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
        ),
    ),
    (
        "network_error",
        "模型请求网络错误",
        compile_patterns(
            GENERIC_PROVIDER_ERROR_PREFIX_FRAGMENT
            + r".{0,100}\b(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|"
            r"ENOTFOUND|request_timeout|request_timed_out|connection_error|connection_timeout|"
            r"api[_ -]?connection[_ -]?error|api[_ -]?timeout[_ -]?error)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,180}"
            r"\b(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,180}"
            r"\b(?:request_timeout|request_timed_out|connection[_ -]?(?:error|timeout)|"
            r"api[_ -]?connection[_ -]?error|api[_ -]?timeout[_ -]?error)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,180}\b"
            + PROVIDER_CONNECTION_FAILURE_FRAGMENT
            + r"\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\bdns.{0,40}(?:timeout|failed|error)\b.{0,40}\bfor\s+"
            r"(?:api\.openai\.com|chatgpt\.com)\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b.{0,180}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,100}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:api[_ -]?connection[_ -]?error|api[_ -]?timeout[_ -]?error)\b.{0,180}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,100}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,180}"
            r"\b(?:network (?:error|failure|timeout)|request (?:timed out|timeout)|"
            r"connection[_ -]?(?:error|timeout)|connection failed|"
            r"error while reading the server response|fetch failed|"
            r"connection (?:timed out|refused|reset|aborted)|tls handshake (?:failed|error)|"
            r"dns.{0,40}(?:timeout|failed|error))\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:network (?:error|failure|timeout)|request (?:timed out|timeout)|connection failed|"
            r"error while reading the server response|fetch failed|"
            r"connection (?:timed out|refused|reset|aborted)|tls handshake (?:failed|error)|"
            r"dns.{0,40}(?:timeout|failed|error))\b.{0,100}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,100}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\brequest failed\b.{0,80}\b(?:while|when)\b.{0,40}"
            r"\b(?:calling|contacting|sending to|connecting to)\b.{0,40}"
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,80}"
            r"\b(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|"
            r"api[_ -]?connection[_ -]?error|api[_ -]?timeout[_ -]?error)\b",
        ),
    ),
    (
        "request_error",
        "模型请求临时失败",
        compile_patterns(
            GENERIC_PROVIDER_ERROR_PREFIX_FRAGMENT
            + r".{0,100}\bconflict[_ -]?error\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b"
            r".{0,180}\bconflict[_ -]?error\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\bconflict[_ -]?error\b.{0,100}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,100}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
        ),
    ),
    (
        "temporary_error",
        "模型服务临时错误",
        compile_patterns(
            GENERIC_PROVIDER_ERROR_PREFIX_FRAGMENT
            + r".{0,100}\b(?:temporary|transient)\b.{0,40}\b(?:error|failure|unavailable)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,180}"
            r"\b(?:temporary|transient)\b.{0,60}\b(?:error|failure|unavailable)\b",
            RUNTIME_ERROR_PREFIX_FRAGMENT
            + r"\b(?:temporary|transient)\b.{0,60}\b(?:error|failure|unavailable)\b.{0,180}"
            + TRAILING_PROVIDER_RELATION_FRAGMENT
            + r".{0,100}\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b",
            r"\b(?:codex|openai|chatgpt|model provider|model service|responses api)\b.{0,180}"
            r"\b(?:please retry|try again (?:later|shortly|in a few)|operation timed out|something went wrong)\b",
        ),
    ),
)

NON_RETRYABLE_PATTERNS = compile_patterns(
    r"\bturn_aborted\b",
    r"\b(?:user|the user) (?:interrupted|cancelled|canceled|aborted)\b",
    r"\binterrupted the previous turn on purpose\b",
    r"\b(?:response|stream|request|turn)\b.{0,80}\b(?:interrupted|cancelled|canceled|aborted)\b"
    r".{0,50}\b(?:by user|user|ctrl[- ]?c|sigint|manually)\b",
    r"\b(?:by user|ctrl[- ]?c|sigint|manually)\b.{0,50}\b(?:interrupted|cancelled|canceled|aborted)\b",
    r"\b(?:http(?:[\s_-]+status)?|status(?:[\s_-]+code)?|error(?:[\s_-]+code)?|code)\s*[:=]?\s*(?:400|401|403|404|405|406|407|410|411|412|413|414|415|416|417|418|421|422|423|424|425|426|428|431|451|499)\b",
    r"\b(?:bad request|not found)\b",
    r"\bbad[_ -]?request[_ -]?error\b",
    r"\bpermission[_ -]?denied[_ -]?error\b",
    r"\bnot[_ -]?found[_ -]?error\b",
    r"\bunprocessable[_ -]?entity[_ -]?error\b",
    r"\bpermission denied\b",
    r"\bforbidden\b",
    r"\bunauthorized\b",
    r"\binvalid[_ -]?api[_ -]?key\b",
    r"\bincorrect api key\b",
    r"\bauthentication (?:failed|required|error)\b",
    r"\bauthentication[_ -]?(?:failed|required|error)\b",
    r"\b(?:failed|could not|unable) to authenticate\b",
    r"\b(?:login required|access denied|account (?:disabled|deactivated|suspended))\b",
    r"\b(?:api key|bearer token|oauth token|access token)\b.{0,40}"
    r"\b(?:expired|invalid|revoked|missing)\b",
    r"\b(?:certificate (?:verify failed|(?:has |is )?expired|revoked|(?:is )?not yet valid)|"
    r"certificate[_ -]?verify[_ -]?failed|cert[_ -]?has[_ -]?expired|self[- _]?signed[- _]?certificate|"
    r"hostname mismatch|ip address mismatch|unknown ca|unable to get local issuer certificate|"
    r"unable to verify the first certificate|unable_to_verify_leaf_signature|"
    r"err_tls_cert_altname_invalid|certificate required)\b",
    r"\bpermission[_ -]denied\b",
    r"\b(?:invalid[_ -]?request(?:[_ -]?error)?|invalid[_ -]?json|malformed[_ -]?request)\b",
    r"\b(?:model[_ -]?not[_ -]?found|unsupported[_ -]?model)\b",
    r"\b(?:content[_ -]?policy[_ -]?violation|safety[_ -]?violation|policy[_ -]?violation)\b",
    r"\b(?:context[_ -]?length[_ -]?exceeded|maximum[_ -]?context[_ -]?length|prompt[_ -]?too[_ -]?long)\b",
    r"\binsufficient[_ -]?quota\b",
    r"\b(?:quota[_ -]?exceeded|usage[_ -]?limit[_ -]?reached|billing[_ -]?hard[_ -]?limit[_ -]?reached)\b",
    r"\b(?:(?:monthly[_ -]?)?budget[_ -]?exceeded|hard[_ -]?limit[_ -]?reached|"
    r"billing[_ -]?not[_ -]?active|(?:account|organization)[_ -]?deactivated|"
    r"insufficient[_ -]?funds)\b",
    r"\b(?:credit[_ -]?balance[_ -]?exhausted|credits? exhausted|organization[_ -]?spend[_ -]?limit[_ -]?exceeded|project[_ -]?spend[_ -]?limit[_ -]?exceeded|organization[_ -]?usage[_ -]?limit[_ -]?exceeded)\b",
    r"\b(?:billing|payment required|payment_required|spend limit|usage limit)\b",
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
    r"\b(?:validation[_ -]?error|unprocessable[_ -]?entity|content[_ -]?filter|refusal)\b",
    r"\bunsupported\b",
)

QUOTE_EXPLANATION_PATTERNS = compile_patterns(
    r"\b(?:means|refers to|for example|examples?|samples?|fixtures?|test cases?|test data|"
    r"diagnosis|diagnostic|reporting|how to)\b",
    r"\berror handling\b",
    r"\b(?:one )?possible (?:error )?(?:string|message|response)\b",
    r"\bexact (?:provider )?(?:message|error|string)\b",
    r"\b(?:is|are|was|were) (?:emitted|returned|shown|reported|documented|used)\b",
    r"\b(?:occurs|happens) when\b",
    r"\b(?:can|may|could)\b.{0,80}"
    r"\b(?:occur|happen|be handled|be mitigated|be resolved|back off)\b",
    r"\bshould\b.{0,80}\b(?:be handled|be mitigated|be resolved|back off)\b",
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
    r"(?:error|fatal)\b|provider\s+(?:error|failure)\s*:|"
    r"(?:an?\s+)?error occurred\b|something went wrong\b|"
    + PROVIDER_RUNTIME_ENVELOPE_FRAGMENT
    + r"|"
    r"(?:the\s+)?(?:request|response|stream|connection|socket)\b.{0,80}"
    r"\b(?:error|failed|failure|interrupted|disconnected|closed|reset|aborted|timed out|timeout)\b|"
    r"we(?:'|’)?re currently experiencing high demand\b|"
    r"selected model is at capacity\b|"
    r"stream disconnected before completion\b|"
    r"stream closed before response\.completed\b|"
    r"websocket closed by server before response\.completed\b|"
    r"connection failed\b|"
    r"error while reading the server response\b|"
    r"request timed out\b|"
    r"response\.completed\b.{0,60}\b(?:missing|not received|never received)\b|"
    r"(?:rate_limit_exceeded|ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b|"
    r"(?:unexpected\s+)?(?:http\s+)?status(?:\s+code)?\s*[:=]?\s*"
    r"(?:4\d{2}|5\d{2})\b|"
    r"http(?:/\d(?:\.\d)?)?\s+(?:4\d{2}|5\d{2})\b)",
    re.IGNORECASE,
)
LEADING_ERROR_ONLY_PATTERN = re.compile(
    r"^\s*(?:[#>*`!\-]+\s*)?(?:error|fatal)\s*:\s*$",
    re.IGNORECASE,
)
MODEL_PROVIDER_AT_MATCH_END_PATTERN = re.compile(
    r"\b(?:codex|openai|chatgpt|model provider|model service|responses api|"
    r"api\.openai\.com|chatgpt\.com)\b\s*$",
    re.IGNORECASE,
)
PROVIDER_EXPLANATION_TAIL_PATTERN = re.compile(
    r"^\s*(?:documentation|docs?|readme|api\s+reference|reference|catalog|"
    r"sdk\s+(?:docs?|documentation|reference)|integration\s+tests?|"
    r"appears?\s+in\s+(?:the\s+)?readme)\b",
    re.IGNORECASE,
)

STRONG_ERROR_ENVELOPE_PATTERN = re.compile(
    r"^\s*(?:[#>*`!\-]+\s*)?(?:"
    r"(?:error|fatal)\b|provider\s+(?:error|failure)\s*:|"
    r"(?:an?\s+)?error occurred\b|something went wrong\b|"
    + PROVIDER_RUNTIME_ENVELOPE_FRAGMENT
    + r"|"
    r"(?:the\s+)?(?:request|response|stream|connection|socket)\b.{0,80}"
    r"\b(?:error|failed|failure|interrupted|disconnected|closed|reset|aborted|timed out|timeout)\b|"
    r"(?:rate_limit_exceeded|ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b|"
    r"(?:selected model is at capacity|stream closed before response\.completed|"
    r"websocket closed by server before response\.completed|connection failed|"
    r"error while reading the server response|request timed out)\b|"
    r"(?:unexpected\s+)?(?:http\s+)?status(?:\s+code)?\s*[:=]?\s*"
    r"(?:4\d{2}|5\d{2})\b|"
    r"http(?:/\d(?:\.\d)?)?\s+(?:4\d{2}|5\d{2})\b)",
    re.IGNORECASE,
)

RETRY_AFTER_NUMBER_PATTERN = re.compile(
    r"\bretry[\s_-]+after(?![\s_-]*ms)\b[\"']?\s*[:=]\s*[\"']?"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)[\"']?(?![\w.])",
    re.IGNORECASE,
)
RETRY_AFTER_MS_PATTERN = re.compile(
    r"\bretry[\s_-]+after[\s_-]+ms\b[\"']?\s*[:=]\s*[\"']?"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)[\"']?(?![\w.])",
    re.IGNORECASE,
)
RETRY_AFTER_DATE_PATTERN = re.compile(
    r"\bretry[\s_-]+after(?![\s_-]*ms)\b[\"']?\s*[:=]\s*[\"']?("
    r"[A-Za-z]{3},\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}\s+(?:GMT|UTC)|"
    r"[A-Za-z]{6,9},\s+\d{1,2}-[A-Za-z]{3}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+(?:GMT|UTC)|"
    r"[A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})[\"']?",
    re.IGNORECASE,
)
RETRY_AFTER_HEADER_PREFIX_PATTERN = re.compile(
    r"^\s*(?:[\{\[]\s*)?[\"']?retry[\s_-]+after(?:[\s_-]+ms)?\b",
    re.IGNORECASE,
)

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
FENCED_CODE_PATTERN = re.compile(r"^\s*```[\s\S]*```\s*$")
INLINE_CODE_PATTERN = re.compile(r"^\s*`[^`\r\n]+`\s*$")
BOLD_ERROR_PATTERN = re.compile(r"^\s*\*\*(?:error|fatal)\s*:?\*\*", re.IGNORECASE)

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
SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?:[.!?;]\s+|[。！？；](?=\S)|[\r\n]+)")

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


def is_markdown_quoted_error(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if FENCED_CODE_PATTERN.fullmatch(stripped) or INLINE_CODE_PATTERN.fullmatch(stripped):
        return True
    lines = [line for line in stripped.splitlines() if line.strip()]
    if lines and all(line.lstrip().startswith(">") for line in lines):
        return True
    return BOLD_ERROR_PATTERN.search(stripped) is not None


def make_snippet(text: str, match: re.Match[str], radius: int = 180) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return normalize_text(text[start:end])


def extract_http_statuses(text: str) -> list[int]:
    statuses: list[int] = []
    for pattern in HTTP_STATUS_PATTERNS:
        for match in pattern.finditer(text):
            try:
                statuses.append(int(match.group(1)))
            except (TypeError, ValueError):
                continue
    return statuses


def split_message_segments(text: str) -> list[str]:
    """按明确语句边界切分，避免前一段错误为后一段说明文字提供信封。"""
    return [
        normalized
        for segment in SENTENCE_BOUNDARY_PATTERN.split(text)
        if (normalized := normalize_text(segment))
    ]


def provider_http_statuses(text: str) -> list[int]:
    statuses: list[int] = []
    for segment in split_message_segments(text):
        statuses.extend(provider_http_statuses_in_segment(segment))
    return statuses


def provider_http_statuses_in_segment(segment: str) -> list[int]:
    if (
        PROVIDER_CONTEXT_PATTERN.search(segment)
        and RUNTIME_STATUS_ENVELOPE_PATTERN.search(segment)
    ):
        return extract_http_statuses(segment)
    return []


def parse_retry_after(text: str) -> float | None:
    values: list[float] = []

    for pattern, divisor in (
        (RETRY_AFTER_MS_PATTERN, 1000.0),
        (RETRY_AFTER_NUMBER_PATTERN, 1.0),
    ):
        for match in pattern.finditer(text):
            try:
                value = float(match.group(1)) / divisor
            except (TypeError, ValueError, OverflowError):
                continue
            if value < 0:
                continue
            values.append(value if math.isfinite(value) else math.inf)

    for match in RETRY_AFTER_DATE_PATTERN.finditer(text):
        try:
            parsed = parsedate_to_datetime(match.group(1))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            value = parsed.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, IndexError):
            continue
        if value >= 0:
            values.append(value if math.isfinite(value) else math.inf)

    return max(values) if values else None


def retry_after_for_segment(segments: list[str], index: int) -> float | None:
    """仅关联命中句段及相邻的独立 Retry-After 字段，避免其它系统污染等待时间。"""
    related = [segments[index]]

    before = index - 1
    while before >= 0 and RETRY_AFTER_HEADER_PREFIX_PATTERN.search(segments[before]):
        related.insert(0, segments[before])
        before -= 1

    after = index + 1
    while after < len(segments) and RETRY_AFTER_HEADER_PREFIX_PATTERN.search(segments[after]):
        related.append(segments[after])
        after += 1

    return parse_retry_after(" ".join(related))


def match_has_runtime_envelope(segment: str, match: re.Match[str]) -> bool:
    """要求命中片段自身具备运行时证据，防止借用同句中的本地 Error。"""
    matched_text = match.group(0)
    if (
        MODEL_PROVIDER_AT_MATCH_END_PATTERN.search(matched_text)
        and PROVIDER_EXPLANATION_TAIL_PATTERN.search(segment[match.end() :])
    ):
        return False
    if ERROR_ENVELOPE_PATTERN.search(matched_text) or provider_http_statuses(matched_text):
        return True
    return LEADING_ERROR_ONLY_PATTERN.fullmatch(segment[: match.start()]) is not None


def classify_retryable_error(text: str) -> Detection | None:
    if is_markdown_quoted_error(text):
        return None

    normalized = normalize_text(text)
    if not normalized or is_self_retry_text(normalized):
        return None

    if X_SHOULD_RETRY_FALSE_PATTERN.search(normalized):
        return None

    if has_any(PROVIDER_NEGATION_PATTERNS, normalized):
        return None

    if (
        has_any(LOCAL_SYSTEM_ERROR_PATTERNS, normalized)
        and EXPLICIT_PROVIDER_ENDPOINT_PATTERN.search(normalized) is None
    ):
        return None

    if has_any(NON_RETRYABLE_PATTERNS, normalized):
        return None

    statuses = extract_http_statuses(normalized)
    if any(400 <= status < 500 and status not in RETRYABLE_HTTP_STATUSES for status in statuses):
        return None
    message_segments = split_message_segments(text)
    provider_status_entries = [
        (index, segment, segment_statuses)
        for index, segment in enumerate(message_segments)
        if (segment_statuses := provider_http_statuses_in_segment(segment))
    ]
    provider_statuses = [
        status
        for _, _, segment_statuses in provider_status_entries
        for status in segment_statuses
    ]
    runtime_segments = [
        (index, segment)
        for index, segment in enumerate(message_segments)
        if (
            ERROR_ENVELOPE_PATTERN.search(segment)
            or provider_http_statuses_in_segment(segment)
        )
    ]

    if not runtime_segments and not provider_statuses:
        return None

    if has_any(QUOTE_EXPLANATION_PATTERNS, normalized):
        return None

    if has_any(GENERIC_EXPLANATION_PATTERNS, normalized) and not STRONG_ERROR_ENVELOPE_PATTERN.search(
        normalized
    ):
        return None

    if any(status == 429 for status in provider_statuses):
        index, segment, _ = next(
            entry for entry in provider_status_entries if 429 in entry[2]
        )
        return Detection(
            category="rate_limit",
            label="模型服务限流",
            snippet=normalize_text(segment[:360]),
            retry_after_seconds=retry_after_for_segment(message_segments, index),
        )
    if any(500 <= status <= 599 for status in provider_statuses):
        index, segment, _ = next(
            entry
            for entry in provider_status_entries
            if any(500 <= status <= 599 for status in entry[2])
        )
        return Detection(
            category="server_error",
            label="模型上游服务错误",
            snippet=normalize_text(segment[:360]),
            retry_after_seconds=retry_after_for_segment(message_segments, index),
        )
    if any(status in {408, 409} for status in provider_statuses):
        index, segment, _ = next(
            entry for entry in provider_status_entries if any(status in {408, 409} for status in entry[2])
        )
        return Detection(
            category="request_error",
            label="模型请求临时失败",
            snippet=normalize_text(segment[:360]),
            retry_after_seconds=retry_after_for_segment(message_segments, index),
        )

    for segment_index, segment in runtime_segments:
        for category, label, patterns in RETRYABLE_RULES:
            for pattern in patterns:
                match = pattern.search(segment)
                if match and match_has_runtime_envelope(segment, match):
                    return Detection(
                        category=category,
                        label=label,
                        snippet=make_snippet(segment, match),
                        retry_after_seconds=retry_after_for_segment(
                            message_segments,
                            segment_index,
                        ),
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
    if not turn_id:
        raise ValueError("turn_id must not be empty")
    source = f"v2\0{session_id}\0{turn_id}"
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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS retry_records_last_seen_idx "
                "ON retry_records(last_seen)"
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


def release_claim(scope_hash: str, attempt: int) -> bool:
    """回滚尚未形成 continuation 的最后一次领取，避免无效消耗预算。"""
    if attempt <= 0:
        return False

    now = int(time.time())
    try:
        connection = sqlite3.connect(
            str(state_database()),
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts FROM retry_records WHERE scope_hash = ?",
                (scope_hash,),
            ).fetchone()
            if row is None or type(row[0]) is not int or row[0] != attempt:
                connection.commit()
                return False

            if attempt == 1:
                connection.execute(
                    "DELETE FROM retry_records WHERE scope_hash = ? AND attempts = ?",
                    (scope_hash, attempt),
                )
            else:
                connection.execute(
                    "UPDATE retry_records SET attempts = ?, last_seen = ? "
                    "WHERE scope_hash = ? AND attempts = ?",
                    (attempt - 1, now, scope_hash, attempt),
                )
            connection.commit()
            return True
        finally:
            connection.close()
    except (OSError, OverflowError, sqlite3.Error, TypeError, ValueError):
        # 并发领取已经推进时不回退，宁可少重试一次也不制造重复 attempt。
        return False


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
    turn_id = payload.get("turn_id")
    stop_hook_active = payload.get("stop_hook_active")
    last_message_value = payload.get("last_assistant_message")
    transcript_path_value = payload.get("transcript_path")

    if not isinstance(session_id, str) or not session_id.strip():
        return 0
    if not isinstance(turn_id, str) or not turn_id.strip():
        return 0
    if not isinstance(stop_hook_active, bool):
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
    scope_hash = retry_scope_hash(session_id.strip(), turn_id.strip(), detection)
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
        release_claim(scope_hash, attempt)
        return 0
    if delay > 0:
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
