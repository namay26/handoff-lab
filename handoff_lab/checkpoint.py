"""Persist versioned task state and evidence for a fresh agent session."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any


def file_manifest(workspace: Path) -> dict[str, str]:
    return {
        str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(workspace.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
    }


def manifest_digest(files: dict[str, str]) -> str:
    encoded = json.dumps(files, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def workspace_digest(workspace: Path) -> str:
    return manifest_digest(file_manifest(workspace))


def checkpoint_for_context(saved: dict[str, Any], workspace: Path) -> dict[str, Any]:
    view = deepcopy(saved)
    files = file_manifest(workspace)
    current = manifest_digest(files)
    view["current_workspace_digest"] = current
    for key in ("files_read", "files_written"):
        for record in view.get(key, []):
            record["current"] = files.get(record["path"]) == record["sha256"]
    if view.get("latest_test"):
        view["latest_test"]["current"] = view["latest_test"]["workspace_digest"] == current
    if view.get("agent_notes"):
        view["agent_notes"]["current"] = view["agent_notes"].get("workspace_digest") == current
    return view


def trace_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def build_checkpoint(
    task: dict[str, Any], workspace: Path, trace_path: Path, step: int, state: str,
    agent_notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    events = trace_events(trace_path)
    files = file_manifest(workspace)
    digest = manifest_digest(files)
    reads: dict[str, dict[str, Any]] = {}
    writes: dict[str, dict[str, Any]] = {}
    latest_test: dict[str, Any] | None = None
    for event in events:
        data = event["data"]
        if event["kind"] == "tool_result":
            result = data["result"]
            if data["action"] in {"read_file", "write_file"}:
                record = {"path": result["path"], "step": event["step"], "sha256": result["sha256"], "evidence": event["id"]}
                record["current"] = files.get(record["path"]) == record["sha256"]
                (reads if data["action"] == "read_file" else writes)[result["path"]] = record
            elif data["action"] == "run_tests":
                latest_test = _test_record(event, "agent")
        elif event["kind"] == "acceptance_check":
            latest_test = _test_record(event, data["phase"])
    if latest_test:
        latest_test["current"] = latest_test["workspace_digest"] == digest
    notes = agent_notes or {"learned": [], "remaining": [], "next_action": "", "status": "unverified"}
    if notes.get("workspace_digest"):
        notes = {**notes, "current": notes["workspace_digest"] == digest}
    return {
        "schema_version": 1,
        "task": {"id": task["id"], "goal": task["goal"], "acceptance_check": task["acceptance_check"]},
        "last_step": step,
        "last_event_id": events[-1]["id"] if events else None,
        "state": state,
        "workspace_files": files,
        "workspace_digest": digest,
        "files_read": list(reads.values()),
        "files_written": list(writes.values()),
        "latest_test": latest_test,
        "agent_notes": notes,
    }


def _test_record(event: dict[str, Any], phase: str) -> dict[str, Any]:
    data = event["data"] if event["kind"] == "acceptance_check" else event["data"]["result"]
    output = data.get("stderr", "") + "\n" + data.get("stdout", "")
    return {
        "step": event["step"],
        "phase": phase,
        "passed": data["passed"],
        "exit_code": data["exit_code"],
        "command": data["command"],
        "workspace_digest": data["workspace_digest"],
        "failures": [line[:200] for line in output.splitlines() if line.startswith(("FAIL:", "ERROR:"))][:10],
        "evidence": event["id"],
    }
