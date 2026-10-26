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
from handoff_lab.experiment import run_experiment
from handoff_lab.models import BedrockModel, GeminiModel, ModelReply, gemini_request, list_gemini_models
from handoff_lab.runner import execute_action, resume_task, run_task
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
    def test_paired_experiment_uses_identical_prefix_and_two_prompt_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            run_task(TASK, source, "scripted", pause_after=2)
            original = (source / "checkpoint.json").read_text()
            report = run_experiment(source, root / "experiment", repeats=1)
            self.assertEqual((source / "checkpoint.json").read_text(), original)
            self.assertEqual({item["condition"] for item in report["trials"]}, {"checkpoint", "minimal"})
            self.assertTrue((root / "experiment" / "experiment.html").exists())
            self.assertEqual(len(run_experiment(source, root / "experiment", repeats=1)["trials"]), 2)
            for item in report["trials"]:
                self.assertTrue(item["metrics"]["passed"])
                run_dir = root / "experiment" / item["run"]
                events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
                call = next(event for event in events if event["kind"] == "model_call" and event["step"] == 3)
                self.assertEqual(call["data"]["input"]["history"], [])
                self.assertEqual("checkpoint" in call["data"]["input"], item["condition"] == "checkpoint")
                self.assertEqual(events[0]["id"], "evt-0001")

            partial = json.loads((root / "experiment" / "experiment.json").read_text())
            partial["trials"][0]["metrics"]["run_status"] = "ERROR"
            (root / "experiment" / "experiment.json").write_text(json.dumps(partial))
            retried = run_experiment(source, root / "experiment", repeats=1)
            self.assertEqual(len(retried["trials"]), 3)
            self.assertTrue((root / "experiment" / "trial-1-checkpoint-retry-2" / "viewer.html").exists())

    def test_checkpoint_resumes_with_fresh_history_and_agent_selected_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            first = run_task(TASK, run_dir, "scripted", pause_after=2)
            saved = json.loads((run_dir / "checkpoint.json").read_text())
            pause_events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
            handoff_call = next(e for e in pause_events if e["kind"] == "handoff_call")
            self.assertEqual(handoff_call["data"]["input"]["history"][-1]["step"], 2)
            self.assertEqual(handoff_call["data"]["input"]["state"], "IMPLEMENTING")
            self.assertEqual(first["run_status"], "PAUSED")
            self.assertEqual(saved["last_step"], 2)
            self.assertEqual(saved["files_read"][0]["path"], "auth.py")
            self.assertEqual(saved["files_written"][0]["path"], "auth.py")
            self.assertFalse(saved["files_read"][0]["current"])
            self.assertTrue(saved["files_written"][0]["current"])
            self.assertFalse(saved["latest_test"]["current"])
            self.assertEqual(saved["agent_notes"]["status"], "unverified")
            self.assertEqual(saved["agent_notes"]["workspace_digest"], saved["workspace_digest"])
            self.assertTrue(saved["agent_notes"]["current"])

            final = resume_task(run_dir)
            events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
            resumed = next(e for e in events if e["kind"] == "model_call" and e["data"]["session"] == 2)
            self.assertEqual(resumed["data"]["input"]["history"], [])
            self.assertEqual(resumed["data"]["action"], {"action": "read_file", "path": "auth.py"})
            self.assertEqual(resumed["data"]["input"]["checkpoint"]["last_step"], 2)
            self.assertEqual(final["run_status"], "COMPLETED")
            self.assertEqual(final["sessions"], 2)
            self.assertEqual(len({e["id"] for e in events}), len(events))

    def test_external_edit_is_identified_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_task(TASK, run_dir, "scripted", pause_after=3)
            source = run_dir / "workspace" / "auth.py"
            source.write_text(source.read_text() + "\n# external edit\n")
            resume_task(run_dir)
            events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text().splitlines()]
            resume = next(e for e in events if e["kind"] == "session_resume")
            first_call = next(e for e in events if e["kind"] == "model_call" and e["data"]["session"] == 2)
            self.assertEqual(resume["data"]["changed_files"], ["auth.py"])
            self.assertFalse(first_call["data"]["input"]["checkpoint"]["latest_test"]["current"])
            auth_write = next(x for x in first_call["data"]["input"]["checkpoint"]["files_written"] if x["path"] == "auth.py")
            self.assertFalse(auth_write["current"])

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
