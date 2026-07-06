from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-auto-retry" / "scripts" / "auto_retry_stop.py"
HOOKS = ROOT / "plugins" / "codex-auto-retry" / "hooks" / "hooks.json"
sys.path.insert(0, str(SCRIPT.parent))

import auto_retry_stop  # noqa: E402


class AutoRetryHookTests(unittest.TestCase):
    def test_hook_config_uses_plugin_root(self) -> None:
        payload = json.loads(HOOKS.read_text(encoding="utf-8"))
        hook = payload["hooks"]["Stop"][0]["hooks"][0]
        self.assertIn("PLUGIN_ROOT", hook["command"])
        self.assertIn("$env:PLUGIN_ROOT", hook["commandWindows"])

    def test_detects_high_demand(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "We're currently experiencing high demand, which may cause temporary errors."
        )
        self.assertIsNotNone(detection)
        self.assertEqual(detection.category, "high_demand")

    def test_detects_rate_limit(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "OpenAI API request failed with 429: rate_limit_exceeded. Retry-After: 15."
        )
        self.assertIsNotNone(detection)
        self.assertEqual(detection.category, "rate_limit")

    def test_detects_server_error(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "Codex model provider error: 503 service unavailable from upstream server."
        )
        self.assertIsNotNone(detection)
        self.assertEqual(detection.category, "server_error")

    def test_detects_stream_error_with_service_context(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "Codex response stream interrupted after the upstream request started."
        )
        self.assertIsNotNone(detection)
        self.assertEqual(detection.category, "stream_error")

    def test_ignores_project_timeout_without_service_context(self) -> None:
        detection = auto_retry_stop.classify_retryable_error(
            "The local pytest command timed out while waiting for the application server."
        )
        self.assertIsNone(detection)

    def test_ignores_non_retryable_errors(self) -> None:
        samples = [
            "OpenAI API invalid_api_key: incorrect API key provided.",
            "OpenAI API insufficient_quota: billing quota exceeded.",
            "Codex failed because the prompt exceeded the maximum context length.",
            "The request was blocked by a content policy violation.",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertIsNone(auto_retry_stop.classify_retryable_error(sample))

    def test_transcript_skips_user_prompt_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Path(temp_dir) / "transcript.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "role": "user",
                        "content": "Can you handle We're currently experiencing high demand?",
                    }
                )
                + "\n"
                + json.dumps({"role": "assistant", "content": "Done."})
                + "\n",
                encoding="utf-8",
            )
            text = auto_retry_stop.extract_transcript_text(str(transcript), 1024)
            self.assertNotIn("high demand", text.lower())

    def test_hook_outputs_block_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["CODEX_AUTO_RETRY_STATE_DIR"] = temp_dir
            env["CODEX_AUTO_RETRY_DISABLE_SLEEP"] = "1"
            env["CODEX_AUTO_RETRY_JITTER"] = "0"
            payload = {
                "session_id": "test-session",
                "hook_event_name": "Stop",
                "last_assistant_message": "OpenAI API request failed with 502 bad gateway.",
            }
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
                input=json.dumps(payload),
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=True,
                env=env,
            )
            output = json.loads(result.stdout)
            self.assertEqual(output["decision"], "block")
            self.assertIn("自动重试 1/5", output["reason"])

    def test_max_attempts_stops_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["CODEX_AUTO_RETRY_STATE_DIR"] = temp_dir
            env["CODEX_AUTO_RETRY_DISABLE_SLEEP"] = "1"
            env["CODEX_AUTO_RETRY_JITTER"] = "0"
            env["CODEX_AUTO_RETRY_MAX_ATTEMPTS"] = "1"
            payload = {
                "session_id": "test-session",
                "hook_event_name": "Stop",
                "last_assistant_message": "Codex model provider error: 503 service unavailable.",
            }
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT)],
                    input=json.dumps(payload),
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    check=True,
                    env=env,
                )
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
