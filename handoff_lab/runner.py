"""Single-agent loop, fixed task checks, and append-only trace recording."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context import ContextWindow, estimate_tokens, prompt_tokens
from .models import BedrockModel, GeminiModel, OpenAIModel, ScriptedModel


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Trace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.count = 0

    def add(self, kind: str, step: int, **data: Any) -> str:
        self.count += 1
        event_id = f"evt-{self.count:04d}"
        event = {
            "id": event_id,
            "time": timestamp(),
            "step": step,
            "kind": kind,
            "data": data,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event_id


def safe_file(workspace: Path, relative: str) -> Path:
    path = (workspace / relative).resolve()
    if not path.is_relative_to(workspace.resolve()) or path == workspace.resolve():
        raise ValueError(f"Path is outside the workspace: {relative}")
    return path


def run_check(task: dict[str, Any], workspace: Path) -> dict[str, Any]:
    command = [sys.executable if part == "{python}" else part for part in task["acceptance_check"]]
    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return {
            "command": command,
            "exit_code": result.returncode,
            "passed": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "exit_code": None,
            "passed": False,
            "stdout": str(exc.stdout or ""),
            "stderr": "Acceptance check timed out after 30 seconds",
        }


def execute_action(
    action: dict[str, Any], task: dict[str, Any], workspace: Path
) -> dict[str, Any]:
    name = action.get("action")
    if name == "read_file":
        path = safe_file(workspace, str(action["path"]))
        return {"path": action["path"], "content": path.read_text(encoding="utf-8")}
    if name == "write_file":
        if action.get("path") not in task.get("editable_files", []):
            raise ValueError("File is not in the task's editable_files list")
        path = safe_file(workspace, str(action["path"]))
        content = action["content"]
        if not isinstance(content, str):
            raise ValueError("write_file content must be text")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": action["path"], "bytes_written": len(content.encode("utf-8"))}
    if name == "run_tests":
        return run_check(task, workspace)
    if name == "finish":
        return {"message": str(action.get("message", ""))}
    raise ValueError(f"Unknown action: {name}")


def run_task(
    task_file: Path,
    run_dir: Path,
    model_kind: str,
    model_name: str | None = None,
    region: str | None = None,
    request_timeout: float = 45,
    retries: int = 1,
    context_policy: str = "full",
    context_threshold: int = 1500,
) -> dict[str, Any]:
    task_file = task_file.resolve()
    task = json.loads(task_file.read_text(encoding="utf-8"))
    fixture = (task_file.parent / task["fixture_dir"]).resolve()
    if not fixture.is_dir():
        raise ValueError(f"Fixture directory does not exist: {fixture}")
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    context_window = ContextWindow(context_policy, context_threshold)

    if model_kind == "scripted":
        model = ScriptedModel()
    elif model_kind == "bedrock":
        model = BedrockModel(model_name or "", region or "")
    elif model_kind == "openai":
        model = OpenAIModel(model_name or "")
    elif model_kind == "gemini":
        model = GeminiModel(model_name or "", timeout=request_timeout, retries=retries)
    else:
        raise ValueError(f"Unknown model kind: {model_kind}")
    run_dir.mkdir(parents=True)
    workspace = run_dir / "workspace"
    shutil.copytree(fixture, workspace)
    trace = Trace(run_dir / "trace.jsonl")
    metadata = {
        "task_id": task["id"],
        "title": task["title"],
        "goal": task["goal"],
        "model": model.label,
        "started_at": timestamp(),
        "source_task": str(task_file),
        "workspace": str(workspace.resolve()),
        "context_policy": context_policy,
        "context_threshold": context_threshold if context_policy == "compact" else None,
    }
    (run_dir / "run.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    state = "READY"
    history: list[dict[str, Any]] = []
    baseline = run_check(task, workspace)
    trace.add("acceptance_check", 0, phase="baseline", **baseline)
    trace.add("state", 0, from_state=None, to_state=state)
    error: str | None = None
    steps_taken = 0
    previous_prompt_tokens: int | None = None
    total_prompt_tokens = 0
    compactions = 0

    for step in range(1, int(task.get("max_steps", 8)) + 1):
        steps_taken = step
        compaction = context_window.compact(history, previous_prompt_tokens)
        if compaction:
            compactions += 1
            trace.add("context_compaction", step, **compaction)
        context = {
            "task": {"id": task["id"], "goal": task["goal"]},
            "state": state,
            "acceptance_check": task["acceptance_check"],
            "files": sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
            "editable_files": task.get("editable_files", []),
            **context_window.context_history(history),
        }
        try:
            print(f"Step {step}: requesting next action from {model.label}", file=sys.stderr, flush=True)
            if isinstance(model, GeminiModel):
                model.on_event = lambda kind, **data: trace.add(kind, step, **data)
            reply = model.next_action(context)
            actual_prompt_tokens = prompt_tokens(reply.usage)
            previous_prompt_tokens = actual_prompt_tokens or estimate_tokens(context)
            if actual_prompt_tokens is not None:
                total_prompt_tokens += actual_prompt_tokens
            print(f"Step {step}: {reply.action.get('action', 'invalid action')}", file=sys.stderr, flush=True)
            trace.add(
                "model_call",
                step,
                model=model.label,
                input=context,
                action=reply.action,
                raw_text=reply.raw_text,
                usage=reply.usage,
            )
            action = reply.action
            observation = execute_action(action, task, workspace)
            trace.add("tool_result", step, action=action["action"], result=observation)
            history.append({"step": step, "action": action, "observation": observation})

            if action["action"] == "write_file" and state != "IMPLEMENTING":
                trace.add("state", step, from_state=state, to_state="IMPLEMENTING")
                state = "IMPLEMENTING"
            elif action["action"] == "run_tests":
                target = "VERIFYING" if observation["passed"] else "NEEDS_CHANGES"
                trace.add("state", step, from_state=state, to_state=target)
                state = target
            elif action["action"] == "finish":
                final_check = run_check(task, workspace)
                trace.add("acceptance_check", step, phase="final", **final_check)
                target = "DONE" if final_check["passed"] else "NEEDS_CHANGES"
                trace.add("state", step, from_state=state, to_state=target)
                state = target
                if state == "DONE":
                    break
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            trace.add("run_error", step, message=error)
            break

    result = {
        **metadata,
        "finished_at": timestamp(),
        "state": state,
        "passed": state == "DONE",
        "run_status": "ERROR" if error else ("COMPLETED" if state == "DONE" else "STEP_LIMIT"),
        "steps": steps_taken,
        "error": error,
        "prompt_tokens": total_prompt_tokens,
        "compactions": compactions,
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
