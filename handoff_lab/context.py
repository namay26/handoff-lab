"""Build full or compact model context from completed agent steps."""

from __future__ import annotations

import copy
import json
import math
from typing import Any


RECENT_STEPS = 3


def prompt_tokens(usage: dict[str, Any]) -> int | None:
    for key in ("promptTokenCount", "inputTokens", "input_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def estimate_tokens(value: Any) -> int:
    return math.ceil(len(json.dumps(value, ensure_ascii=False)) / 4)


class ContextWindow:
    def __init__(self, policy: str = "full", threshold: int = 1500) -> None:
        if policy not in {"full", "compact"}:
            raise ValueError("context policy must be full or compact")
        if threshold <= 0:
            raise ValueError("context threshold must be positive")
        self.policy = policy
        self.threshold = threshold
        self.compacted_count = 0
        self.summary: dict[str, Any] = {
            "through_step": 0,
            "files_read": [],
            "files_written": [],
            "latest_test": None,
            "note": "Older tool outputs were removed. Read workspace files again if their contents are needed.",
        }

    def compact(self, history: list[dict[str, Any]], last_prompt_tokens: int | None) -> dict[str, Any] | None:
        if self.policy != "compact" or last_prompt_tokens is None or last_prompt_tokens < self.threshold:
            return None
        end = len(history) - RECENT_STEPS
        if end <= self.compacted_count:
            return None
        old = history[self.compacted_count:end]
        before = estimate_tokens({"summary": self.summary if self.compacted_count else None, "history": history[self.compacted_count:]})
        for turn in old:
            action = turn["action"]
            observation = turn["observation"]
            name = action.get("action")
            if name == "read_file":
                path = action["path"]
                if path not in self.summary["files_read"]:
                    self.summary["files_read"].append(path)
            elif name == "write_file":
                path = action["path"]
                if path not in self.summary["files_written"]:
                    self.summary["files_written"].append(path)
            elif name == "run_tests":
                output = observation.get("stderr", "") + "\n" + observation.get("stdout", "")
                failures = [line[:200] for line in output.splitlines() if line.startswith(("FAIL:", "ERROR:"))]
                self.summary["latest_test"] = {
                    "step": turn["step"],
                    "passed": observation.get("passed"),
                    "exit_code": observation.get("exit_code"),
                    "failures": failures[:10],
                }
            self.summary["through_step"] = turn["step"]
        from_step = old[0]["step"]
        self.compacted_count = end
        after = estimate_tokens({"summary": self.summary, "history": history[end:]})
        return {
            "from_step": from_step,
            "through_step": old[-1]["step"],
            "retained_steps": [turn["step"] for turn in history[end:]],
            "trigger_prompt_tokens": last_prompt_tokens,
            "threshold": self.threshold,
            "estimated_tokens_before": before,
            "estimated_tokens_after": after,
            "summary": copy.deepcopy(self.summary),
        }

    def context_history(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        if self.policy == "full":
            return {"history": history}
        result: dict[str, Any] = {"history": history[self.compacted_count:]}
        if self.compacted_count:
            result["summary"] = copy.deepcopy(self.summary)
        return result
