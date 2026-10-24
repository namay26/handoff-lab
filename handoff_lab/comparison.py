"""Read comparable outcome and context metrics from saved runs."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .context import prompt_tokens


def summarize_run(run_dir: Path) -> dict[str, Any]:
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    calls = [event["data"] for event in events if event["kind"] == "model_call"]
    reads = Counter(call["action"].get("path") for call in calls if call["action"].get("action") == "read_file")
    all_calls = [event["data"] for event in events if event["kind"] in {"model_call", "handoff_call"}]
    tokens = [prompt_tokens(call.get("usage", {})) for call in all_calls]
    measured = [count for count in tokens if count is not None]
    return {
        "run": str(run_dir.resolve()),
        "model": result["model"],
        "context_policy": result.get("context_policy", "full"),
        "run_status": result.get("run_status"),
        "state": result["state"],
        "passed": result["passed"],
        "steps": result["steps"],
        "model_calls": len(calls),
        "handoff_calls": sum(event["kind"] == "handoff_call" for event in events),
        "sessions": result.get("sessions", 1),
        "prompt_tokens": sum(measured) if len(measured) == len(all_calls) else None,
        "last_prompt_tokens": prompt_tokens(calls[-1].get("usage", {})) if calls else None,
        "repeated_file_reads": sum(count - 1 for count in reads.values()),
        "compactions": sum(event["kind"] == "context_compaction" for event in events),
        "acceptance_checks": [
            {"phase": event["data"]["phase"], "passed": event["data"]["passed"]}
            for event in events if event["kind"] == "acceptance_check"
        ],
    }
