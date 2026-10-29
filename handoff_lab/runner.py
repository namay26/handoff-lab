"""Single-agent loop, fixed task checks, and append-only trace recording."""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context import ContextWindow, estimate_tokens, prompt_tokens
from .checkpoint import build_checkpoint, checkpoint_for_context, file_manifest, trace_events, workspace_digest, write_json_atomic
from .models import BedrockModel, GeminiModel, OpenAIModel, ScriptedModel


HANDOFF_MODES = ("checkpoint", "lean", "minimal", "without-notes", "without-manifest", "without-tests", "without-files")


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Trace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.count = len(trace_events(path)) if path.exists() else 0

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
    digest = workspace_digest(workspace)
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
            "workspace_digest": digest,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "exit_code": None,
            "passed": False,
            "stdout": str(exc.stdout or ""),
            "stderr": "Acceptance check timed out after 30 seconds",
            "workspace_digest": digest,
        }


def execute_action(
    action: dict[str, Any], task: dict[str, Any], workspace: Path
) -> dict[str, Any]:
    name = action.get("action")
    if name == "read_file":
        path = safe_file(workspace, str(action["path"]))
        content = path.read_text(encoding="utf-8")
        return {"path": action["path"], "content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    if name == "write_file":
        if action.get("path") not in task.get("editable_files", []):
            raise ValueError("File is not in the task's editable_files list")
        path = safe_file(workspace, str(action["path"]))
        content = action["content"]
        if not isinstance(content, str):
            raise ValueError("write_file content must be text")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": action["path"], "bytes_written": len(content.encode("utf-8")), "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    if name == "run_tests":
        return run_check(task, workspace)
    if name == "finish":
        return {"message": str(action.get("message", ""))}
    raise ValueError(f"Unknown action: {name}")


def make_model(kind: str, name: str | None, region: str | None, timeout: float, retries: int) -> Any:
    if kind == "scripted":
        return ScriptedModel()
    if kind == "gemini":
        return GeminiModel(name or "", timeout=timeout, retries=retries)
    if kind == "bedrock":
        return BedrockModel(name or "", region or "")
    if kind == "openai":
        return OpenAIModel(name or "")
    raise ValueError(f"Unknown model kind: {kind}")


def save_checkpoint(run_dir: Path, task: dict[str, Any], step: int, state: str, notes: dict[str, Any] | None) -> dict[str, Any]:
    checkpoint = build_checkpoint(task, run_dir / "workspace", run_dir / "trace.jsonl", step, state, notes)
    write_json_atomic(run_dir / "checkpoint.json", checkpoint)
    return checkpoint


def run_task(
    task_file: Path, run_dir: Path, model_kind: str, model_name: str | None = None,
    region: str | None = None, request_timeout: float = 45, retries: int = 1,
    context_policy: str = "full", context_threshold: int = 1500,
    pause_after: int | None = None, pause_on_stale_test: bool = False,
) -> dict[str, Any]:
    task_file = task_file.resolve()
    task = json.loads(task_file.read_text(encoding="utf-8"))
    fixture = (task_file.parent / task["fixture_dir"]).resolve()
    if not fixture.is_dir():
        raise ValueError(f"Fixture directory does not exist: {fixture}")
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    if pause_after is not None and not 1 <= pause_after <= int(task.get("max_steps", 8)):
        raise ValueError("pause_after must be within this session's step budget")
    if pause_after is not None and pause_on_stale_test:
        raise ValueError("Choose either pause_after or pause_on_stale_test")
    model = make_model(model_kind, model_name, region, request_timeout, retries)
    ContextWindow(context_policy, context_threshold)
    run_dir.mkdir(parents=True)
    workspace = run_dir / "workspace"
    shutil.copytree(fixture, workspace)
    trace = Trace(run_dir / "trace.jsonl")
    metadata = {
        "task_id": task["id"], "title": task["title"], "goal": task["goal"],
        "model": model.label, "model_kind": model_kind, "model_name": model_name,
        "region": region, "started_at": timestamp(), "source_task": str(task_file),
        "source_task_sha256": hashlib.sha256(task_file.read_bytes()).hexdigest(),
        "workspace": str(workspace.resolve()), "context_policy": context_policy,
        "context_threshold": context_threshold if context_policy == "compact" else None,
    }
    write_json_atomic(run_dir / "run.json", metadata)
    baseline = run_check(task, workspace)
    trace.add("acceptance_check", 0, phase="baseline", **baseline)
    trace.add("state", 0, from_state=None, to_state="READY")
    trace.add("session_start", 0, session=1, resumed=False)
    save_checkpoint(run_dir, task, 0, "READY", None)
    return run_session(task, run_dir, model, metadata, "READY", 0, 1, None,
                       context_policy, context_threshold, pause_after, None,
                       pause_on_stale_test=pause_on_stale_test)


def resume_task(
    run_dir: Path, model_kind: str | None = None, model_name: str | None = None,
    region: str | None = None, request_timeout: float = 45, retries: int = 1,
    context_policy: str | None = None, context_threshold: int | None = None,
    pause_after: int | None = None, handoff_mode: str = "checkpoint",
    pause_on_stale_test: bool = False,
) -> dict[str, Any]:
    if handoff_mode not in HANDOFF_MODES:
        raise ValueError(f"handoff_mode must be one of: {', '.join(HANDOFF_MODES)}")
    run_dir = run_dir.resolve()
    metadata = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    prior = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if prior["run_status"] != "PAUSED":
        raise ValueError("Only a paused run can be resumed")
    task_file = Path(metadata["source_task"])
    if hashlib.sha256(task_file.read_bytes()).hexdigest() != metadata["source_task_sha256"]:
        raise ValueError("Source task changed since this run started")
    task = json.loads(task_file.read_text(encoding="utf-8"))
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text(encoding="utf-8"))
    events = trace_events(run_dir / "trace.jsonl")
    if checkpoint["schema_version"] != 1 or checkpoint["last_event_id"] != events[-1]["id"]:
        raise ValueError("Checkpoint and trace are out of sync; inspect the run before resuming")
    if checkpoint["last_step"] != prior["steps"] or checkpoint["task"]["id"] != task["id"]:
        raise ValueError("Checkpoint does not match the paused run")
    kind = model_kind or metadata["model_kind"]
    name = model_name or metadata["model_name"]
    model = make_model(kind, name, region or metadata["region"], request_timeout, retries)
    policy = context_policy or metadata["context_policy"]
    threshold = context_threshold if context_threshold is not None else (metadata.get("context_threshold") or 1500)
    ContextWindow(policy, threshold)
    if pause_after is not None and not 1 <= pause_after <= int(task.get("max_steps", 8)):
        raise ValueError("pause_after must be within this session's step budget")
    if pause_after is not None and pause_on_stale_test:
        raise ValueError("Choose either pause_after or pause_on_stale_test")
    current_files = file_manifest(run_dir / "workspace")
    changed = sorted(path for path in set(current_files) | set(checkpoint["workspace_files"])
                     if current_files.get(path) != checkpoint["workspace_files"].get(path))
    handoff = {**checkpoint, "workspace_changed": bool(changed), "changed_files": changed}
    if changed:
        if handoff["latest_test"]:
            handoff["latest_test"] = {**handoff["latest_test"], "current": False}
        handoff["agent_notes"] = {**handoff["agent_notes"], "current": False}
    trace = Trace(run_dir / "trace.jsonl")
    session = int(prior.get("sessions", 1)) + 1
    trace.add("session_resume", checkpoint["last_step"], session=session,
              checkpoint_event=checkpoint["last_event_id"], changed_files=changed,
              workspace_digest=workspace_digest(run_dir / "workspace"), handoff_mode=handoff_mode)
    save_checkpoint(run_dir, task, checkpoint["last_step"], checkpoint["state"], checkpoint["agent_notes"])
    return run_session(task, run_dir, model, metadata, checkpoint["state"], checkpoint["last_step"],
                       session, prior, policy, threshold, pause_after,
                       handoff if handoff_mode != "minimal" else None, handoff_mode,
                       pause_on_stale_test)


def run_session(
    task: dict[str, Any], run_dir: Path, model: Any, metadata: dict[str, Any],
    state: str, start_step: int, session: int, prior: dict[str, Any] | None,
    context_policy: str, context_threshold: int, pause_after: int | None,
    handoff: dict[str, Any] | None, handoff_mode: str = "checkpoint",
    pause_on_stale_test: bool = False,
) -> dict[str, Any]:
    workspace = run_dir / "workspace"
    trace = Trace(run_dir / "trace.jsonl")
    window = ContextWindow(context_policy, context_threshold)
    history: list[dict[str, Any]] = []
    previous_prompt_tokens: int | None = None
    prompt_total = int(prior.get("prompt_tokens", 0)) if prior else 0
    compactions = int(prior.get("compactions", 0)) if prior else 0
    handoff_calls = int(prior.get("handoff_calls", 0)) if prior else 0
    notes = handoff["agent_notes"] if handoff else None
    prior_tests = [event for event in trace_events(run_dir / "trace.jsonl")
                   if event["kind"] == "tool_result" and event["data"].get("action") == "run_tests"]
    agent_test_digest = prior_tests[-1]["data"]["result"]["workspace_digest"] if prior_tests else None
    error: str | None = None
    steps_taken = start_step
    paused = False
    for local_step in range(1, int(task.get("max_steps", 8)) + 1):
        step = start_step + local_step
        steps_taken = step
        compaction = window.compact(history, previous_prompt_tokens)
        if compaction:
            compactions += 1
            trace.add("context_compaction", step, session=session, **compaction)
        context = {
            "task": {"id": task["id"], "goal": task["goal"]},
            "state": state, "acceptance_check": task["acceptance_check"],
            "files": sorted(file_manifest(workspace)),
            "editable_files": task.get("editable_files", []),
            **window.context_history(history),
        }
        if handoff:
            context["checkpoint"] = checkpoint_for_context(handoff, workspace, handoff_mode)
        try:
            print(f"Step {step}: requesting next action from {model.label}", file=sys.stderr, flush=True)
            if isinstance(model, GeminiModel):
                model.on_event = lambda kind, **data: trace.add(kind, step, session=session, **data)
            reply = model.next_action(context)
            actual_tokens = prompt_tokens(reply.usage)
            previous_prompt_tokens = actual_tokens if actual_tokens is not None else estimate_tokens(context)
            prompt_total += actual_tokens or 0
            print(f"Step {step}: {reply.action.get('action', 'invalid action')}", file=sys.stderr, flush=True)
            trace.add("model_call", step, session=session, model=model.label, input=context,
                      action=reply.action, raw_text=reply.raw_text, usage=reply.usage)
            action = reply.action
            observation = execute_action(action, task, workspace)
            trace.add("tool_result", step, session=session, action=action["action"], result=observation)
            history.append({"step": step, "action": action, "observation": observation})
            if action["action"] == "write_file" and state != "IMPLEMENTING":
                trace.add("state", step, from_state=state, to_state="IMPLEMENTING")
                state = "IMPLEMENTING"
            elif action["action"] == "run_tests":
                agent_test_digest = observation["workspace_digest"]
                target = "VERIFYING" if observation["passed"] else "NEEDS_CHANGES"
                trace.add("state", step, from_state=state, to_state=target)
                state = target
            elif action["action"] == "finish":
                final_check = run_check(task, workspace)
                trace.add("acceptance_check", step, phase="final", **final_check)
                target = "DONE" if final_check["passed"] else "NEEDS_CHANGES"
                trace.add("state", step, from_state=state, to_state=target)
                state = target
            save_checkpoint(run_dir, task, step, state, notes)
            if state == "DONE":
                break
            stale_test = (action["action"] == "write_file" and agent_test_digest is not None
                          and agent_test_digest != workspace_digest(workspace))
            if pause_after == local_step or (pause_on_stale_test and stale_test):
                handoff_context = {
                    **context,
                    "state": state,
                    "files": sorted(file_manifest(workspace)),
                    "history": [*context["history"], history[-1]],
                }
                note, used = request_handoff(model, handoff_context, workspace, step, session, trace)
                prompt_total += used
                handoff_calls += 1
                notes = note
                trace.add("session_pause", step, session=session,
                          reason="stale_test" if pause_on_stale_test and stale_test else "step")
                save_checkpoint(run_dir, task, step, state, notes)
                paused = True
                break
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            trace.add("run_error", step, message=error)
            break
    result = {
        **metadata, "model": model.label, "finished_at": timestamp(),
        "state": state, "passed": state == "DONE",
        "run_status": "ERROR" if error else ("COMPLETED" if state == "DONE" else "PAUSED" if paused else "STEP_LIMIT"),
        "steps": steps_taken, "session_steps": steps_taken - start_step, "sessions": session,
        "error": error, "prompt_tokens": prompt_total, "compactions": compactions,
        "handoff_calls": handoff_calls,
    }
    write_json_atomic(run_dir / "result.json", result)
    return result


def request_handoff(model: Any, context: dict[str, Any], workspace: Path, step: int, session: int, trace: Trace) -> tuple[dict[str, Any], int]:
    request = {**context, "handoff_only": True, "workspace_digest": workspace_digest(workspace),
               "handoff_instruction": "Describe what you learned from files, what remains, and the next action. Do not claim unrun tests passed."}
    try:
        reply = model.next_action(request)
        trace.add("handoff_call", step, session=session, model=model.label, input=request,
                  action=reply.action, raw_text=reply.raw_text, usage=reply.usage)
        if reply.action.get("action") != "handoff":
            raise ValueError("Model did not return a handoff note")
        learned = reply.action.get("learned", [])
        remaining = reply.action.get("remaining", [])
        if not isinstance(learned, list) or not isinstance(remaining, list):
            raise ValueError("Handoff learned and remaining must be lists")
        note = {"learned": learned[:20], "remaining": remaining[:20],
                "next_action": str(reply.action.get("next_action", ""))[:500],
                "status": "unverified", "workspace_digest": request["workspace_digest"]}
        trace.add("handoff_note", step, session=session, note=note)
        return note, prompt_tokens(reply.usage) or 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        trace.add("handoff_error", step, session=session, message=f"{type(exc).__name__}: {exc}")
        return {"learned": [], "remaining": [], "next_action": "", "status": "unverified"}, 0
