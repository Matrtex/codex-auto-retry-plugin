from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-auto-retry"
SCRIPT = PLUGIN / "scripts" / "auto_retry_stop.py"
HOOKS = PLUGIN / "hooks" / "hooks.json"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
sys.path.insert(0, str(SCRIPT.parent))

import auto_retry_stop  # noqa: E402


RETRY_ENV_VARS = {
    "CODEX_AUTO_RETRY_MAX_ATTEMPTS",
    "CODEX_AUTO_RETRY_BASE_DELAY",
    "CODEX_AUTO_RETRY_MAX_DELAY",
    "CODEX_AUTO_RETRY_BACKOFF_FACTOR",
    "CODEX_AUTO_RETRY_JITTER",
    "CODEX_AUTO_RETRY_STATE_TTL_SECONDS",
    "CODEX_AUTO_RETRY_STATE_DIR",
    "CODEX_AUTO_RETRY_MESSAGE_SCAN_CHARS",
    "CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK",
    "CODEX_AUTO_RETRY_TRANSCRIPT_TAIL_BYTES",
    "PLUGIN_DATA",
    "CLAUDE_PLUGIN_DATA",
}


def latest_stop_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "session-current",
        "turn_id": "turn-current",
        "transcript_path": None,
        "cwd": str(ROOT),
        "hook_event_name": "Stop",
        "model": "gpt-5.6",
        "permission_mode": "default",
        "stop_hook_active": False,
        "last_assistant_message": "Codex model provider error: 503 service unavailable from upstream.",
    }
    payload.update(updates)
    return payload


def hook_environment(state_dir: str | Path, **updates: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in RETRY_ENV_VARS:
        env.pop(name, None)
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "CODEX_AUTO_RETRY_STATE_DIR": str(state_dir),
            "CODEX_AUTO_RETRY_BASE_DELAY": "0",
            "CODEX_AUTO_RETRY_JITTER": "0",
        }
    )
    env.update(updates)
    return env


def run_hook(
    payload: object,
    state_dir: str | Path,
    *,
    raw_input: str | None = None,
    env_updates: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = hook_environment(state_dir, **(env_updates or {}))
    input_text = raw_input if raw_input is not None else json.dumps(payload, ensure_ascii=False)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=60,
        env=env,
    )


def output_attempt(result: subprocess.CompletedProcess[str]) -> int | None:
    if not result.stdout:
        return None
    output = json.loads(result.stdout)
    match = re.search(r"安全续跑 (\d+)/(\d+)", output["reason"])
    if not match:
        raise AssertionError(f"输出缺少尝试次数：{output}")
    return int(match.group(1))


class DetectionTests(unittest.TestCase):
    def test_detects_supported_provider_failures(self) -> None:
        samples = {
            "high_demand": "We're currently experiencing high demand, which may cause temporary errors.",
            "rate_limit": "OpenAI API request failed with 429: rate_limit_exceeded. Retry-After: 15.",
            "server_error": "Codex model provider error: 503 service unavailable from upstream.",
            "stream_error": "stream disconnected before completion: response.completed was not received",
            "network_error": "OpenAI model provider request failed with ECONNRESET.",
            "request_error": "Error: OpenAI API returned status 409 conflict.",
            "temporary_error": "OpenAI model provider returned a transient error; please retry.",
        }
        for category, sample in samples.items():
            with self.subTest(category=category):
                detection = auto_retry_stop.classify_retryable_error(sample)
                self.assertIsNotNone(detection)
                self.assertEqual(detection.category, category)

    def test_ignores_permanent_failures(self) -> None:
        samples = [
            "OpenAI API invalid_api_key: incorrect API key provided.",
            "OpenAI API 429 rate_limit_exceeded and insufficient_quota.",
            "OpenAI API authentication failed with status 401.",
            "OpenAI API request failed with status 405 method not allowed.",
            "OpenAI API request failed with status 422 validation_error.",
            "OpenAI API request failed with status 400 invalid_request_error.",
            "OpenAI API request failed with status 400 content_policy_violation.",
            "OpenAI API request failed with status 400 context_length_exceeded.",
            "OpenAI API request failed with status 429 quota_exceeded.",
            "OpenAI API request failed with status 429 billing_hard_limit_reached.",
            "OpenAI API request failed with status 429 credit_balance_exhausted.",
            "OpenAI API request failed with status 429 organization_spend_limit_exceeded.",
            "OpenAI API returned 429 monthly_budget_exceeded.",
            "OpenAI API returned 429 hard_limit_reached.",
            "OpenAI API billing_not_active.",
            "OpenAI API request failed with status 503; \"x-should-retry\": \"false\".",
            "Codex failed because the prompt exceeded the maximum context length.",
            "OpenAI model not found: status 404.",
            "The request was blocked by a content policy violation.",
            "The user interrupted the previous turn on purpose.",
            "OpenAI API request failed with status 400 invalid_request_error; x-should-retry: true.",
            "OpenAI API request failed with status 425 too_early; x-should-retry: true.",
            "Error: OpenAI API BadRequestError with transient wording.",
            "Error: OpenAI API PermissionDeniedError; please retry.",
            "Error: OpenAI API NotFoundError; temporary failure.",
            "Error: OpenAI API UnprocessableEntityError; server_error.",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNone(auto_retry_stop.classify_retryable_error(sample))

    def test_ignores_explanations_and_project_errors(self) -> None:
        samples = [
            "OpenAI API 429 表示限流，以下是排查方法。",
            "HTTP 503 means service unavailable; for example, retry with backoff.",
            "文档示例：We're currently experiencing high demand, which may cause temporary errors.",
            "The checkout service is overloaded. Please retry the local request.",
            "The local pytest command timed out while waiting for the application server.",
            "Our integration test contains an OpenAI 429 example in a code block.",
            "OpenAI returns HTTP 429 when rate limits are exceeded.",
            "Codex may return 503 during maintenance.",
            "The exact provider message is: We're currently experiencing high demand, which may cause temporary errors.",
            "Codex error handling should back off after HTTP 429.",
            "OpenAI request failed with 429 is one possible error string.",
            "stream disconnected before completion is emitted when Codex closes an SSE stream early.",
            "OpenAI high demand periods can cause temporary errors.",
            "Codex is often overloaded at peak hours.",
            "OpenAI provider error 503 can occur during maintenance.",
            "OpenAI provider error 503 should be handled with backoff.",
            "OpenAI rate_limit_exceeded error can be mitigated by retrying.",
            "Error: OpenAI 429. To resolve it, use exponential backoff.",
            "Connection failed to localhost:5432.",
            "Error: Docker daemon returned status 503 service unavailable.",
            "Error: OpenAI response interrupted by user.",
            "Error: OpenAI request failed with 503 tokens in prompt.",
            "Error: OpenAI API response code 408 bytes read.",
            "Error: OpenAI API response code 429 examples appear in this test.",
            "Error while reading the server response from localhost; this project also uses OpenAI.",
            "Error: MCP connection failed before calling OpenAI.",
            "GitHub connection failed; OpenAI is unrelated.",
            "Connection failed: SSL certificate has expired for api.openai.com.",
            "OpenAI connection failed to authenticate.",
            "Connection failed to Stripe payment gateway. OpenAI summaries were not affected.",
            "database driver connection reset. The OpenAI integration completed normally.",
            "OpenAI returned 503 server_error_example.",
            "Database connection failed before OpenAI call.",
            "Connection failed to Redis before OpenAI call.",
            "GitHub connection failed before OpenAI call.",
            "Database connection failed; see OpenAI documentation.",
            "Docker returned HTTP 503; OpenAI docs follow.",
            "Error: Slack connection failed while OpenAI request was running.",
            "Error: S3 timed out while waiting for OpenAI cleanup.",
            "Error: Webhook failed when OpenAI result was saved.",
            "Error: Payment gateway failed when OpenAI completed.",
            "OpenAI docs mention ECONNRESET as a retryable error.",
            "OpenAI README lists ETIMEDOUT.",
            "OpenAI can produce ECONNRESET.",
            "OpenAI SDK raises APIConnectionError for ECONNRESET.",
            "OpenAI code handles ECONNRESET with retries.",
            "OpenAI README lists service unavailable errors.",
            "OpenAI SDK may report service unavailable.",
            "OpenAI code handles bad gateway responses.",
            "OpenAI docs discuss gateway timeout.",
            "OpenAI supports rate_limit_exceeded handling.",
            "OpenAI uses service_unavailable as an enum.",
            "OpenAI HTTP status 503 reference entry.",
            "OpenAI status code 429 table row.",
            "OpenAI error code 503 documentation.",
            "OpenAI error catalog contains service unavailable.",
            "Provider error documentation: bad gateway.",
            "OpenAI error reference: rate_limit_exceeded.",
            "Error: local request failed; OpenAI docs discuss bad gateway.",
            "Error: local operation timed out; OpenAI error catalog says service unavailable.",
            "Error: Redis unavailable; OpenAI supports rate_limit_exceeded handling.",
            "Error: build failed. OpenAI README lists service unavailable errors.",
            "Error: build failed.\nOpenAI README lists service unavailable errors.",
            "Error: local task failed, OpenAI docs discuss bad gateway.",
            "Error: local task failed: OpenAI docs discuss bad gateway.",
            "Error: local task failed - OpenAI docs discuss bad gateway.",
            "Error: Slack request failed, OpenAI uses service_unavailable as an enum.",
            "Error: Slack request failed and OpenAI README lists ETIMEDOUT.",
            "HTTP 503 for OpenAI integration test.",
            "Status 429 for OpenAI API.",
            "Error: HTTP 503 for OpenAI integration test.",
            "Error: Status 429 for OpenAI API.",
            "Error: payment provider service unavailable.",
            "Error: payment provider error: 503 service unavailable.",
            "Error: identity provider returned 503 service unavailable.",
            "Error: cloud provider request timed out with ETIMEDOUT.",
            "HTTP 503 from OpenAI documentation.",
            "HTTP 503 from OpenAI docs.",
            "HTTP 503 from OpenAI API reference.",
            "Status 429 from OpenAI SDK docs.",
            "HTTP 503 from OpenAI appears in the README.",
            "`Error: OpenAI API returned status 503 service unavailable.`",
            "> Error: OpenAI API returned status 503 service unavailable.",
            "```text\nError: OpenAI API returned status 503 service unavailable.\n```",
            "**Error:** OpenAI API returned status 503 service unavailable.",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNone(auto_retry_stop.classify_retryable_error(sample))

    def test_retry_after_is_parsed_without_premature_truncation(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "OpenAI API request failed with 429 rate_limit_exceeded. Retry-After: 15.5"
        )
        self.assertIsNotNone(detection)
        self.assertEqual(detection.retry_after_seconds, 15.5)
        self.assertEqual(auto_retry_stop.parse_retry_after("Retry-After: 999999"), 999999.0)
        self.assertEqual(
            auto_retry_stop.parse_retry_after("Retry-After: 5; Retry-After: 60"),
            60.0,
        )
        self.assertEqual(auto_retry_stop.parse_retry_after("retry-after-ms: 2500"), 2.5)
        self.assertEqual(auto_retry_stop.parse_retry_after("retry_after: 1e9"), 1e9)
        self.assertEqual(auto_retry_stop.parse_retry_after('{"retry_after":"7"}'), 7.0)
        self.assertEqual(auto_retry_stop.parse_retry_after('{"retry-after-ms":2500}'), 2.5)
        self.assertIsNone(auto_retry_stop.parse_retry_after("Retry-After: 90seconds"))
        self.assertGreater(
            auto_retry_stop.parse_retry_after("Retry-After: Wed, 21 Oct 2099 07:28:00 GMT"),
            0.0,
        )
        unrelated = auto_retry_stop.classify_retryable_error(
            "OpenAI API returned status 503 service unavailable. "
            "Redis cache asked for Retry-After: 90."
        )
        self.assertIsNotNone(unrelated)
        self.assertIsNone(unrelated.retry_after_seconds)
        adjacent = auto_retry_stop.classify_retryable_error(
            "OpenAI API returned status 503 service unavailable. "
            "Retry-After: 15. Redis cache asked for Retry-After: 90."
        )
        self.assertIsNotNone(adjacent)
        self.assertEqual(adjacent.retry_after_seconds, 15.0)

    def test_detects_natural_language_errors_with_trailing_provider_context(self) -> None:
        samples = {
            "rate_limit": [
                "Error: too many requests from the OpenAI provider.",
                "Error: throttled by the OpenAI Responses API.",
                "Error: rate limited by model provider.",
                "OpenAI status code: 429 too many requests.",
                "Provider error: 429 rate_limit_exceeded.",
                "Error: OpenAI API RateLimitError.",
            ],
            "server_error": [
                "Error: service unavailable from OpenAI provider.",
                "Error: bad gateway returned by the model provider.",
                "Error: OpenAI API Error code: 503 - server_error.",
                "OpenAI HTTP status: 503 service unavailable.",
                "OpenAI HTTP status: 503 service unavailable; x-should-retry: true.",
                "OpenAI returned 522 connection_timed_out.",
                "Error: OpenAI API server_error.",
                "OpenAI model provider returned service unavailable while diagnosing PostgreSQL database.",
                "Error: OpenAI API InternalServerError.",
                "Error: OpenAI API request failed: 503.",
            ],
            "network_error": [
                "Error: TLS handshake failed while contacting OpenAI provider.",
                "Error: DNS lookup failed for api.openai.com.",
                "Error: request timed out while waiting for the OpenAI provider.",
                "Error: fetch failed while calling the OpenAI Responses API.",
                "Error: OpenAI API connection_error.",
                "Provider error: connection_error.",
                "Error: OpenAI API APIConnectionError.",
                "Error: openai.APIConnectionError: Connection error.",
                "Error: APIConnectionError from OpenAI.",
                "Error: OpenAI API APITimeoutError.",
                "Error: APITimeoutError from OpenAI.",
                "Error: OpenAI API connection error.",
                "Error: OpenAI API connection timeout.",
                "Error: Request failed while calling OpenAI: ECONNRESET.",
                "OpenAI provider request timed out while answering a database migration question.",
                "OpenAI API could not connect to the upstream service.",
                "OpenAI API was unable to be contacted.",
                "connection failed to api.openai.com via localhost proxy.",
                "OpenAI API was not contacted due to DNS timeout.",
            ],
            "stream_error": [
                "Error: stream interrupted while reading from the OpenAI provider.",
                "Error: response was truncated by the OpenAI upstream.",
                "Error: failed to stream a response from the model provider.",
                "stream closed before response.completed",
                "websocket closed by server before response.completed",
                "Error: OpenAI API response_stream_disconnected.",
                "Provider error: response_stream_disconnected.",
            ],
            "request_error": [
                "Error: OpenAI API returned status 409 conflict.",
                "OpenAI returned 408 request_timeout.",
                "OpenAI returned 409 resource_locked.",
                "OpenAI response code: 408 request_timed_out.",
                "Error: OpenAI API ConflictError.",
            ],
            "temporary_error": [
                "Error: temporary failure from the OpenAI provider.",
                "Error: transient error from model provider.",
                "Provider error: transient failure.",
            ],
        }
        for category, category_samples in samples.items():
            for sample in category_samples:
                with self.subTest(category=category, sample=sample):
                    detection = auto_retry_stop.classify_retryable_error(sample)
                    self.assertIsNotNone(detection)
                    self.assertEqual(detection.category, category)

    def test_latency_numbers_do_not_suppress_real_server_errors(self) -> None:
        for sample in (
            "Codex model provider error: 503 service unavailable after 400 ms.",
            "Codex model provider error: 503 service unavailable; request took 404 ms.",
        ):
            with self.subTest(sample=sample):
                detection = auto_retry_stop.classify_retryable_error(sample)
                self.assertIsNotNone(detection)
                self.assertEqual(detection.category, "server_error")

    def test_strong_runtime_errors_can_include_followup_guidance(self) -> None:
        samples = [
            "OpenAI API request failed with 429 rate_limit_exceeded. See documentation for retry guidance.",
            "Codex model provider error: 503 service unavailable. See documentation.",
            "Provider error: 503 service unavailable; this indicates a temporary outage.",
            "Error: OpenAI provider error: 503 service unavailable. You can retry.",
            "Error: OpenAI API request failed with 429 rate_limit_exceeded. Should retry later.",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNotNone(auto_retry_stop.classify_retryable_error(sample))


class PayloadAndTurnTests(unittest.TestCase):
    def test_latest_stop_payload_outputs_valid_block_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_hook(latest_stop_payload(), temp_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        output = json.loads(result.stdout)
        self.assertEqual(output["decision"], "block")
        self.assertEqual(output_attempt(result), 1)

    def test_same_turn_shares_budget_across_continuations_and_error_changes(self) -> None:
        messages = [
            "We're currently experiencing high demand, which may cause temporary errors.",
            "OpenAI API request failed with 429 rate_limit_exceeded.",
            "Codex model provider error: 503 service unavailable.",
            "stream disconnected before completion: response.completed was not received",
        ]
        active_values = [False, True, True, True]
        attempts: list[int | None] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            for message, active in zip(messages, active_values):
                result = run_hook(
                    latest_stop_payload(
                        turn_id="same-turn",
                        stop_hook_active=active,
                        last_assistant_message=message,
                    ),
                    temp_dir,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                attempts.append(output_attempt(result))
        self.assertEqual(attempts, [1, 2, 3, None])

    def test_different_turns_and_sessions_have_independent_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = run_hook(latest_stop_payload(session_id="s1", turn_id="t1"), temp_dir)
            second_turn = run_hook(latest_stop_payload(session_id="s1", turn_id="t2"), temp_dir)
            second_session = run_hook(latest_stop_payload(session_id="s2", turn_id="t1"), temp_dir)
        self.assertEqual([output_attempt(first), output_attempt(second_turn), output_attempt(second_session)], [1, 1, 1])

    def test_active_continuation_without_state_does_not_reset_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_hook(
                latest_stop_payload(stop_hook_active=True),
                temp_dir,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_legacy_payload_without_new_turn_fields_fails_open(self) -> None:
        payload = {
            "session_id": "legacy-session",
            "hook_event_name": "Stop",
            "last_assistant_message": "Codex model provider error: 503 service unavailable.",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_hook(payload, temp_dir)
        self.assertEqual(result.stdout, "")

    def test_invalid_payloads_fail_open_without_stdout(self) -> None:
        invalid_cases: list[tuple[object, str | None]] = [
            ([], None),
            (None, None),
            ("text", None),
            ({"hook_event_name": "Stop"}, None),
            (
                {
                    "session_id": "session",
                    "turn_id": "turn",
                    "hook_event_name": "Stop",
                    "last_assistant_message": (
                        "Codex model provider error: 503 service unavailable."
                    ),
                },
                None,
            ),
            (latest_stop_payload(stop_hook_active="false"), None),
            (latest_stop_payload(turn_id=123), None),
            (latest_stop_payload(last_assistant_message={"error": "503"}), None),
            (latest_stop_payload(hook_event_name="SessionEnd"), None),
            ({**latest_stop_payload(), "turn_id": ""}, None),
            ({}, "{truncated"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            for payload, raw_input in invalid_cases:
                with self.subTest(payload=payload, raw_input=raw_input):
                    result = run_hook(payload, temp_dir, raw_input=raw_input)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, "")

    def test_zero_attempts_disables_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_hook(
                latest_stop_payload(),
                temp_dir,
                env_updates={"CODEX_AUTO_RETRY_MAX_ATTEMPTS": "0"},
            )
        self.assertEqual(result.stdout, "")

    def test_retry_after_above_delay_cap_stops_instead_of_retrying_early(self) -> None:
        payload = latest_stop_payload(
            last_assistant_message=(
                "OpenAI API request failed with 429 rate_limit_exceeded. Retry-After: 0.2"
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_hook(
                payload,
                temp_dir,
                env_updates={"CODEX_AUTO_RETRY_MAX_DELAY": "0.1"},
            )
            self.assertEqual(result.stdout, "")
            self.assertFalse((Path(temp_dir) / "state-v2.sqlite3").exists())

            allowed = run_hook(
                payload,
                temp_dir,
                env_updates={"CODEX_AUTO_RETRY_MAX_DELAY": "0.3"},
            )
        self.assertEqual(output_attempt(allowed), 1)
        self.assertIn("已等待 0.2 秒", json.loads(allowed.stdout)["reason"])

        with tempfile.TemporaryDirectory() as temp_dir:
            above_hard_cap = run_hook(
                latest_stop_payload(
                    last_assistant_message=(
                        "OpenAI API request failed with 429 rate_limit_exceeded. Retry-After: 116"
                    )
                ),
                temp_dir,
                env_updates={"CODEX_AUTO_RETRY_MAX_DELAY": "999"},
            )
        self.assertEqual(above_hard_cap.stdout, "")

    def test_lock_time_can_make_retry_after_unfulfillable(self) -> None:
        payload = latest_stop_payload(
            last_assistant_message=(
                "OpenAI API request failed with 429 rate_limit_exceeded. Retry-After: 0.2"
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {
                "CODEX_AUTO_RETRY_STATE_DIR": temp_dir,
                "CODEX_AUTO_RETRY_BASE_DELAY": "0",
                "CODEX_AUTO_RETRY_JITTER": "0",
                "CODEX_AUTO_RETRY_MAX_DELAY": "0.3",
            },
            clear=False,
        ), mock.patch.object(
            auto_retry_stop.sys,
            "stdin",
            io.StringIO(json.dumps(payload)),
        ), mock.patch.object(
            auto_retry_stop.time,
            "monotonic",
            side_effect=[0.0, 114.9],
        ):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(auto_retry_stop.main(), 0)
            self.assertEqual(output.getvalue(), "")
            database = Path(temp_dir) / "state-v2.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                self.assertIsNone(connection.execute("SELECT * FROM retry_records").fetchone())

    def test_message_scan_limit_does_not_pick_stale_prefix(self) -> None:
        message = "Codex model provider error: 503 service unavailable. " + ("正常内容 " * 500)
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_hook(
                latest_stop_payload(last_assistant_message=message),
                temp_dir,
                env_updates={"CODEX_AUTO_RETRY_MESSAGE_SCAN_CHARS": "1024"},
            )
        self.assertEqual(result.stdout, "")


class TranscriptFallbackTests(unittest.TestCase):
    @staticmethod
    def write_nested_transcript(path: Path, *, add_latest_success: bool = False) -> None:
        items: list[dict[str, object]] = [
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Run the task."},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Codex model provider error: 503 service unavailable.",
                        }
                    ],
                },
            },
        ]
        if add_latest_success:
            items.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "任务已正常完成。"}],
                    },
                }
            )
        path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
            encoding="utf-8",
        )

    def test_transcript_fallback_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "transcript.jsonl"
            self.write_nested_transcript(transcript)
            result = run_hook(
                latest_stop_payload(last_assistant_message=None, transcript_path=str(transcript)),
                temp_dir,
            )
        self.assertEqual(result.stdout, "")

    def test_enabled_fallback_parses_latest_nested_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "transcript.jsonl"
            self.write_nested_transcript(transcript)
            result = run_hook(
                latest_stop_payload(last_assistant_message=None, transcript_path=str(transcript)),
                temp_dir,
                env_updates={"CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK": "1"},
            )
        self.assertEqual(output_attempt(result), 1)

    def test_fallback_uses_latest_event_and_ignores_stale_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "transcript.jsonl"
            self.write_nested_transcript(transcript, add_latest_success=True)
            result = run_hook(
                latest_stop_payload(last_assistant_message="", transcript_path=str(transcript)),
                temp_dir,
                env_updates={"CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK": "1"},
            )
        self.assertEqual(result.stdout, "")

    def test_nonempty_last_message_never_uses_stale_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "transcript.jsonl"
            self.write_nested_transcript(transcript)
            result = run_hook(
                latest_stop_payload(last_assistant_message="任务已正常完成。", transcript_path=str(transcript)),
                temp_dir,
                env_updates={"CODEX_AUTO_RETRY_TRANSCRIPT_FALLBACK": "1"},
            )
        self.assertEqual(result.stdout, "")

    def test_tail_discards_partial_line_and_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "transcript.jsonl"
            prefix = json.dumps({"role": "assistant", "content": "x" * 5000})
            error = json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "message": "OpenAI API request failed with 429 rate_limit_exceeded.",
                    },
                }
            )
            transcript.write_text(prefix + "\n{bad json\n" + error + "\n", encoding="utf-8")
            extracted = auto_retry_stop.extract_transcript_text(str(transcript), 4096)
        self.assertIn("rate_limit_exceeded", extracted)

    def test_nested_hook_prompt_is_a_control_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "hook_prompt",
                            "message": "Codex Auto Retry 在 Stop 可见消息中识别到临时错误",
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "content": "正常完成。"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            extracted = auto_retry_stop.extract_transcript_text(str(transcript), 4096)
        self.assertEqual(extracted, "正常完成。")


class StateAndBackoffTests(unittest.TestCase):
    def test_sqlite_claim_is_atomic_across_real_processes(self) -> None:
        payload = latest_stop_payload(session_id="concurrent-session", turn_id="concurrent-turn")
        with tempfile.TemporaryDirectory() as temp_dir:
            def invoke(_: int) -> subprocess.CompletedProcess[str]:
                return run_hook(
                    payload,
                    temp_dir,
                    env_updates={"CODEX_AUTO_RETRY_MAX_ATTEMPTS": "5"},
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
                results = list(executor.map(invoke, range(24)))

            self.assertTrue(all(result.returncode == 0 for result in results))
            self.assertTrue(all(result.stderr == "" for result in results))
            attempts = sorted(attempt for result in results if (attempt := output_attempt(result)) is not None)
            self.assertEqual(attempts, [1, 2, 3, 4, 5])

            database = Path(temp_dir) / "state-v2.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as connection:
                stored_attempts = connection.execute("SELECT attempts FROM retry_records").fetchone()[0]
            self.assertEqual(stored_attempts, 5)

    def test_state_contains_no_session_turn_or_error_text(self) -> None:
        payload = latest_stop_payload(
            session_id="PRIVATE-SESSION-MARKER",
            turn_id="PRIVATE-TURN-MARKER",
            last_assistant_message=(
                "Codex model provider error: 503 service unavailable. PRIVATE-ERROR-MARKER"
            ),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_hook(payload, temp_dir)
            self.assertEqual(output_attempt(result), 1)
            database = Path(temp_dir) / "state-v2.sqlite3"
            raw = database.read_bytes()
            for marker in (b"PRIVATE-SESSION-MARKER", b"PRIVATE-TURN-MARKER", b"PRIVATE-ERROR-MARKER"):
                self.assertNotIn(marker, raw)
            with contextlib.closing(sqlite3.connect(database)) as connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(retry_records)")]
            self.assertEqual(columns, ["scope_hash", "attempts", "first_seen", "last_seen"])

    def test_corrupt_database_fails_open_without_unbounded_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "state-v2.sqlite3").write_bytes(b"not-a-sqlite-database")
            result = run_hook(latest_stop_payload(), temp_dir)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_semantically_corrupt_attempt_count_fails_closed(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "Codex model provider error: 503 service unavailable."
        )
        self.assertIsNotNone(detection)
        scope = auto_retry_stop.retry_scope_hash("session", "turn", detection)
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {"CODEX_AUTO_RETRY_STATE_DIR": temp_dir},
            clear=False,
        ):
            self.assertEqual(auto_retry_stop.claim_next_attempt(scope, 3, 300), 1)
            with contextlib.closing(sqlite3.connect(auto_retry_stop.state_database())) as connection:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute("UPDATE retry_records SET attempts = -100")
                connection.commit()
            self.assertIsNone(
                auto_retry_stop.claim_next_attempt(scope, 3, 300, allow_create=False)
            )

    def test_legacy_v2_schema_rejects_non_integer_state_values(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "Codex model provider error: 503 service unavailable."
        )
        self.assertIsNotNone(detection)
        scope = auto_retry_stop.retry_scope_hash("session", "turn", detection)
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {"CODEX_AUTO_RETRY_STATE_DIR": temp_dir},
            clear=False,
        ):
            database = auto_retry_stop.state_database()
            with contextlib.closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE retry_records (
                        scope_hash TEXT PRIMARY KEY,
                        attempts INTEGER NOT NULL,
                        first_seen INTEGER NOT NULL,
                        last_seen INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO retry_records VALUES (?, ?, ?, ?)",
                    (scope, math.inf, 1, int(auto_retry_stop.time.time())),
                )
                connection.commit()

            self.assertIsNone(
                auto_retry_stop.claim_next_attempt(scope, 3, 300, allow_create=False)
            )

            with contextlib.closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE retry_records SET attempts = ?, first_seen = ?",
                    (-0.5, "invalid"),
                )
                connection.commit()
            self.assertIsNone(
                auto_retry_stop.claim_next_attempt(scope, 3, 300, allow_create=False)
            )

    def test_state_directory_prefers_override_then_plugin_data(self) -> None:
        with tempfile.TemporaryDirectory() as override, tempfile.TemporaryDirectory() as plugin_data:
            with mock.patch.dict(os.environ, {"PLUGIN_DATA": plugin_data}, clear=True):
                self.assertEqual(auto_retry_stop.state_directory(), Path(plugin_data))
            with mock.patch.dict(
                os.environ,
                {"PLUGIN_DATA": plugin_data, "CODEX_AUTO_RETRY_STATE_DIR": override},
                clear=True,
            ):
                self.assertEqual(auto_retry_stop.state_directory(), Path(override))

    def test_active_continuation_cannot_recreate_a_stale_record(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "Codex model provider error: 503 service unavailable."
        )
        self.assertIsNotNone(detection)
        scope = auto_retry_stop.retry_scope_hash("session", "turn", detection)
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            os.environ,
            {"CODEX_AUTO_RETRY_STATE_DIR": temp_dir},
            clear=False,
        ):
            self.assertEqual(auto_retry_stop.claim_next_attempt(scope, 3, 300), 1)
            with contextlib.closing(sqlite3.connect(auto_retry_stop.state_database())) as connection:
                connection.execute("UPDATE retry_records SET last_seen = 0")
                connection.commit()
            self.assertIsNone(
                auto_retry_stop.claim_next_attempt(scope, 3, 300, allow_create=False)
            )
            self.assertEqual(auto_retry_stop.claim_next_attempt(scope, 3, 300), 1)

    def test_jitter_retry_after_and_invalid_env_never_break_cap(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "CODEX_AUTO_RETRY_BASE_DELAY": "8",
                "CODEX_AUTO_RETRY_MAX_DELAY": "10",
                "CODEX_AUTO_RETRY_BACKOFF_FACTOR": "1.8",
                "CODEX_AUTO_RETRY_JITTER": "5",
            },
            clear=False,
        ), mock.patch.object(auto_retry_stop.random, "uniform", return_value=5.0):
            self.assertEqual(auto_retry_stop.retry_delay(3, 9.0), 10.0)

        with mock.patch.dict(
            os.environ,
            {
                "CODEX_AUTO_RETRY_BASE_DELAY": "1e308",
                "CODEX_AUTO_RETRY_MAX_DELAY": "300",
                "CODEX_AUTO_RETRY_BACKOFF_FACTOR": "inf",
                "CODEX_AUTO_RETRY_JITTER": "nan",
            },
            clear=False,
        ):
            delay = auto_retry_stop.retry_delay(10)
            self.assertTrue(math.isfinite(delay))
            self.assertLessEqual(delay, 115.0)

        with mock.patch.dict(
            os.environ,
            {"CODEX_AUTO_RETRY_MAX_DELAY": "0"},
            clear=False,
        ):
            self.assertEqual(auto_retry_stop.retry_delay(1), 0.0)

        with mock.patch.object(auto_retry_stop.time, "monotonic", return_value=30.0):
            self.assertEqual(auto_retry_stop.fit_delay_to_hook_budget(115.0, 0.0), 85.0)

    def test_retry_reason_requires_side_effect_checks(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "Codex model provider error: 503 service unavailable."
        )
        self.assertIsNotNone(detection)
        reason = auto_retry_stop.build_retry_reason(detection, 1, 3, 8.0)
        self.assertIn("不要重复已经成功", reason)
        self.assertIn("外部系统", reason)
        self.assertIn("无法", reason)
        self.assertNotIn("请直接重试上一条用户请求", reason)
        self.assertNotIn("不要向用户索要确认", reason)


class PluginContractTests(unittest.TestCase):
    def test_hook_config_uses_default_path_and_cross_platform_commands(self) -> None:
        payload = json.loads(HOOKS.read_text(encoding="utf-8"))
        group = payload["hooks"]["Stop"][0]
        hook = group["hooks"][0]
        self.assertNotIn("matcher", group)
        self.assertIn("PLUGIN_ROOT", hook["command"])
        self.assertIn("$env:PLUGIN_ROOT", hook["commandWindows"])
        self.assertEqual(hook["timeout"], int(auto_retry_stop.HOOK_TIMEOUT_SECONDS))
        self.assertLess(auto_retry_stop.HOOK_TIMEOUT_RESERVE_SECONDS, hook["timeout"])
        self.assertNotIn("async", hook)
        self.assertTrue(hook["statusMessage"])

    def test_manifest_and_marketplace_contract(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
        entry = marketplace["plugins"][0]

        self.assertEqual(manifest["name"], PLUGIN.name)
        self.assertRegex(
            manifest["version"],
            r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$",
        )
        self.assertNotIn("hooks", manifest)
        self.assertTrue(HOOKS.is_file())
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/codex-auto-retry"})
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual(entry["category"], "Productivity")

    def test_hook_command_runs_from_unicode_path(self) -> None:
        hook = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0]
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_root = Path(temp_dir) / "插件 路径"
            scripts_dir = plugin_root / "scripts"
            scripts_dir.mkdir(parents=True)
            shutil.copy2(SCRIPT, scripts_dir / SCRIPT.name)
            env = hook_environment(Path(temp_dir) / "data")
            env["PLUGIN_ROOT"] = str(plugin_root)
            payload = json.dumps(latest_stop_payload(), ensure_ascii=False)

            if os.name == "nt":
                powershell = shutil.which("pwsh") or shutil.which("powershell")
                if not powershell or not shutil.which("py"):
                    self.skipTest("Windows PowerShell 或 Python launcher 不可用")
                command = [powershell, "-NoProfile", "-NonInteractive", "-Command", hook["commandWindows"]]
            else:
                shell = shutil.which("sh")
                if not shell or not shutil.which("python3"):
                    self.skipTest("sh 或 python3 不可用")
                command = [shell, "-c", hook["command"]]

            result = subprocess.run(
                command,
                input=payload,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=60,
                env=env,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["decision"], "block")


if __name__ == "__main__":
    unittest.main()
