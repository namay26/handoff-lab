"""Checks that trace evidence, not a model's finish claim, controls completion."""

import json
import io
import urllib.error
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from handoff_lab.comparison import summarize_run
from handoff_lab.context import ContextWindow
from handoff_lab.models import BedrockModel, GeminiModel, ModelReply, gemini_request, list_gemini_models
from handoff_lab.runner import execute_action, run_task
from handoff_lab.viewer import render_viewer


PROJECT = Path(__file__).resolve().parents[1]
TASK = PROJECT / "tasks" / "login.json"


class FinishOnlyModel:
    label = "finish-only-test"

    def next_action(self, context):
        return ModelReply(
            action={"action": "finish", "message": "Everything works."},
            raw_text='{"action":"finish","message":"Everything works."}',
            usage={},
        )


class HarnessIntegrationTests(unittest.TestCase):
    def test_compaction_retains_three_recent_steps_and_drops_old_tool_outputs(self):
        history = [
            {"step": step, "action": {"action": "read_file", "path": f"file{step}.py"},
             "observation": {"content": f"old-source-{step}"}}
            for step in range(1, 7)
        ]
        window = ContextWindow("compact", 100)
        self.assertIsNone(window.compact(history, 99))
        event = window.compact(history, 100)
        context = window.context_history(history)
        self.assertEqual([turn["step"] for turn in context["history"]], [4, 5, 6])
        self.assertEqual(context["summary"]["files_read"], ["file1.py", "file2.py", "file3.py"])
        self.assertEqual(event["retained_steps"], [4, 5, 6])
        self.assertNotIn("old-source-1", json.dumps(context))
        self.assertIn("old-source-4", json.dumps(context))

    def test_compaction_carries_verified_test_outcome(self):
        history = [
            {"step": 1, "action": {"action": "run_tests"},
             "observation": {"passed": False, "exit_code": 1, "stderr": "FAIL: test_login\nlong log", "stdout": ""}},
            *({"step": step, "action": {"action": "read_file", "path": "auth.py"},
               "observation": {"content": "source"}} for step in range(2, 6)),
        ]
        window = ContextWindow("compact", 1)
        window.compact(history, 1)
        summary = window.context_history(history)["summary"]
        self.assertEqual(summary["latest_test"], {"step": 1, "passed": False, "exit_code": 1, "failures": ["FAIL: test_login"]})
        self.assertNotIn("long log", json.dumps(summary))

    def test_compact_run_keeps_complete_trace_and_acceptance_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            with patch("handoff_lab.runner.ScriptedModel", FinishOnlyModel):
                result = run_task(TASK, run_dir, "scripted", context_policy="compact", context_threshold=1)
            events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
            compacted = [event for event in events if event["kind"] == "context_compaction"]
            calls = [event for event in events if event["kind"] == "model_call"]
            self.assertTrue(compacted)
            self.assertEqual([turn["step"] for turn in calls[4]["data"]["input"]["history"]], [2, 3, 4])
            self.assertEqual(len([event for event in events if event["kind"] == "tool_result"]), 8)
            self.assertEqual(result["state"], "NEEDS_CHANGES")
            self.assertFalse(result["passed"])
            comparison = summarize_run(run_dir)
            self.assertEqual(comparison["compactions"], result["compactions"])
            self.assertEqual(comparison["model_calls"], 8)

    def test_gemini_timeout_retries_then_recovers(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret"}):
            with patch("handoff_lab.models.urllib.request.urlopen", side_effect=[TimeoutError("stalled"), io.BytesIO(b'{"ok":true}')]) as send:
                with patch("handoff_lab.models.time.sleep"):
                    self.assertEqual(gemini_request("models", timeout=15, retries=1), {"ok": True})
                self.assertEqual(send.call_count, 2)
                self.assertEqual(send.call_args.kwargs["timeout"], 15)

    def test_gemini_retries_are_bounded(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret"}):
            with patch("handoff_lab.models.urllib.request.urlopen", side_effect=TimeoutError("stalled")) as send:
                with patch("handoff_lab.models.time.sleep"), self.assertRaisesRegex(RuntimeError, "attempt 2"):
                    gemini_request("models", retries=1)
                self.assertEqual(send.call_count, 2)

    def test_gemini_daily_quota_error_is_not_retried(self):
        quota = urllib.error.HTTPError("https://example.test", 429, "quota", {}, io.BytesIO(b'{"error":{"message":"Daily quota exceeded"}}'))
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret"}):
            with patch("handoff_lab.models.urllib.request.urlopen", side_effect=quota) as send:
                with self.assertRaisesRegex(RuntimeError, "Daily quota exceeded"):
                    gemini_request("models", retries=1)
                self.assertEqual(send.call_count, 1)

    def test_provider_error_is_distinct_from_task_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("handoff_lab.runner.ScriptedModel.next_action", side_effect=RuntimeError("HTTP 429: quota exceeded")):
                result = run_task(TASK, Path(tmp) / "run", "scripted")
            self.assertEqual(result["run_status"], "ERROR")
            self.assertEqual(result["state"], "READY")
            self.assertIn("quota exceeded", result["error"])

    def test_gemini_header_auth_and_action_parsing(self):
        response = {
            "candidates": [{"content": {"parts": [{"text": '{"action":"read_file","path":"auth.py"}'}]}}],
            "usageMetadata": {"promptTokenCount": 10},
        }
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-gemini-secret"}):
            model = GeminiModel("models/example-gemini")
            with patch("handoff_lab.models.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as send:
                reply = model.next_action({"task": "login"})
        request = send.call_args.args[0]
        self.assertEqual(reply.action["path"], "auth.py")
        self.assertEqual(reply.usage["promptTokenCount"], 10)
        self.assertEqual(request.get_header("X-goog-api-key"), "test-gemini-secret")
        self.assertNotIn("test-gemini-secret", request.full_url)
        self.assertNotIn(b"test-gemini-secret", request.data)

    def test_gemini_discovery_filters_and_paginates(self):
        pages = [
            {"models": [{"name": "models/text-model", "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": "next"},
            {"models": [{"name": "models/embed-model", "supportedGenerationMethods": ["embedContent"]}]},
        ]
        with patch("handoff_lab.models.gemini_request", side_effect=pages) as request:
            self.assertEqual(list_gemini_models(), ["text-model"])
            self.assertIn("pageToken=next", request.call_args.args[0])

    def test_model_cannot_modify_acceptance_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "editable_files"):
                execute_action({"action": "write_file", "path": "test_auth.py", "content": ""}, {"editable_files": ["auth.py"]}, Path(tmp))

    def test_scripted_run_records_failure_then_verified_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            result = run_task(TASK, run_dir, "scripted")
            viewer = render_viewer(run_dir)
            events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
            checks = [e["data"] for e in events if e["kind"] == "acceptance_check"]
            self.assertEqual([check["passed"] for check in checks], [False, True])
            self.assertEqual(result["state"], "DONE")
            self.assertTrue(viewer.exists())
            self.assertEqual(sum(e["kind"] == "model_call" for e in events), 4)

    def test_finish_claim_cannot_override_failing_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            with patch("handoff_lab.runner.ScriptedModel", FinishOnlyModel):
                result = run_task(TASK, run_dir, "scripted")
            events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
            self.assertEqual(result["state"], "NEEDS_CHANGES")
            self.assertFalse(result["passed"])
            self.assertFalse(any(e["kind"] == "state" and e["data"]["to_state"] == "DONE" for e in events))

    def test_bedrock_adapter_uses_bearer_key_without_putting_it_in_context(self):
        response = {
            "output": {"message": {"content": [{"text": '{"action":"read_file","path":"auth.py"}'}]}},
            "usage": {"inputTokens": 12, "outputTokens": 8},
        }
        with patch.dict("os.environ", {"AWS_BEARER_TOKEN_BEDROCK": "test-secret"}):
            model = BedrockModel("us.example.model", "us-east-1")
            with patch("handoff_lab.models.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as send:
                reply = model.next_action({"task": {"goal": "Fix login"}})
        request = send.call_args.args[0]
        self.assertEqual(reply.action["action"], "read_file")
        self.assertEqual(reply.usage["inputTokens"], 12)
        self.assertIn("/model/us.example.model/converse", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertNotIn(b"test-secret", request.data)


if __name__ == "__main__":
    unittest.main()
