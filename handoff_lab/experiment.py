"""Compare fresh-session resumes from one identical paused task state."""

from __future__ import annotations

import html
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .checkpoint import trace_events, workspace_digest, write_json_atomic
from .context import prompt_tokens
from .runner import HANDOFF_MODES, resume_task, safe_file
from .viewer import render_viewer


def continuation_metrics(run_dir: Path, source_step: int) -> dict[str, Any]:
    events = trace_events(run_dir / "trace.jsonl")
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    prior_reads = {
        (event["data"]["result"]["path"], event["data"]["result"]["sha256"])
        for event in events
        if event["kind"] == "tool_result" and event["data"].get("action") == "read_file"
        and event["step"] <= source_step
    }
    continuation = [event for event in events if event["step"] > source_step]
    calls = [event for event in continuation if event["kind"] == "model_call"]
    tokens = [prompt_tokens(event["data"].get("usage", {})) for event in calls]
    reads = [event["data"]["result"] for event in continuation
             if event["kind"] == "tool_result" and event["data"].get("action") == "read_file"]
    tests = [event["data"]["result"] for event in continuation
             if event["kind"] == "tool_result" and event["data"].get("action") == "run_tests"]
    return {
        "model": result["model"],
        "run_status": result["run_status"],
        "passed": result["passed"],
        "actions": len(calls),
        "prompt_tokens": sum(tokens) if all(value is not None for value in tokens) else None,
        "file_reads": len(reads),
        "same_version_rereads": sum((read["path"], read["sha256"]) in prior_reads for read in reads),
        "test_runs": len(tests),
        "tests_passed": [test["passed"] for test in tests],
    }


def render_experiment(report: dict[str, Any], output_dir: Path) -> Path:
    rows = []
    for trial in report["trials"]:
        metrics = trial["metrics"]
        values = [
            trial["trial"], trial["condition"], metrics["run_status"],
            "yes" if metrics["passed"] else "no", metrics["actions"],
            metrics["prompt_tokens"] if metrics["prompt_tokens"] is not None else "unmeasured",
            metrics["same_version_rereads"], metrics["test_runs"],
        ]
        cells = "".join(f"<td>{html.escape(str(value))}</td>" for value in values)
        viewer = html.escape(trial["run"] + "/viewer.html", quote=True)
        rows.append(f"<tr>{cells}<td><a href=\"{viewer}\">trace</a></td></tr>")
    headers = ["Trial", "Condition", "Status", "Passed", "Actions", "Prompt tokens", "Same-version rereads", "Test runs", "Viewer"]
    heading = "".join(f"<th>{html.escape(label)}</th>" for label in headers)
    descriptions = {
        "checkpoint": "full checkpoint",
        "lean": "checkpoint with goal, state, file evidence, and latest test; without agent notes and file manifest",
        "minimal": "no checkpoint",
        "without-notes": "checkpoint without agent notes",
        "without-manifest": "checkpoint without full file manifest",
        "without-tests": "checkpoint without latest test evidence",
        "without-files": "checkpoint without read/write records and file manifest",
    }
    conditions = report.get("conditions", ["checkpoint", "minimal"])
    condition_text = "; ".join(descriptions[condition] for condition in conditions)
    drift = report.get("drift")
    drift_text = (f"<p>Workspace drift: <code>{html.escape(drift['target'])}</code> changed after the pause; "
                  f"original SHA-256 <code>{html.escape(drift['before_sha256'])}</code>; "
                  f"replacement SHA-256 <code>{html.escape(drift['after_sha256'])}</code>.</p>") if drift else ""
    summaries = []
    for condition in conditions:
        metrics = [trial["metrics"] for trial in report["trials"]
                   if trial["condition"] == condition and trial["metrics"]["run_status"] != "ERROR"]
        if not metrics:
            continue
        measured = [item["prompt_tokens"] for item in metrics if item["prompt_tokens"] is not None]
        mean_tokens = round(sum(measured) / len(measured)) if len(measured) == len(metrics) else "unmeasured"
        summaries.append(f"<li>{condition}: {sum(item['passed'] for item in metrics)}/{len(metrics)} passed; "
                         f"mean {sum(item['actions'] for item in metrics) / len(metrics):.1f} actions; "
                         f"mean {mean_tokens} prompt tokens</li>")
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Handoff Lab · Paired experiment</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:0;background:#0b1220;color:#e8eef7}}
main{{max-width:1200px;margin:auto;padding:32px}}h1{{margin-bottom:8px}}p{{color:#adbed3}}
table{{width:100%;border-collapse:collapse;margin-top:24px;background:#121d30}}
th,td{{padding:10px;text-align:left;border-bottom:1px solid #2a3a50}}th{{color:#8fe0ca}}
a{{color:#8fe0ca}}code{{overflow-wrap:anywhere}}.wrap{{overflow-x:auto}}</style></head>
<body><main><h1>Paired handoff experiment</h1>
<p>All trials clone the same paused workspace and start with empty new-session history. When configured, the same file drift is applied to every clone before resume. Each condition changes only the checkpoint fields passed to the model. Metrics below count actions after resume.</p>
<p>Conditions: {html.escape(condition_text)}.</p>
{drift_text}
<p>Source step: {report['source_step']} · Workspace digest: <code>{html.escape(report['workspace_digest'])}</code> · Completed trials: {sum(item['metrics']['run_status'] != 'ERROR' for item in report['trials'])}/{report['planned_trials']} · Attempts: {len(report['trials'])}</p>
<ul>{''.join(summaries)}</ul>
<div class="wrap"><table><thead><tr>{heading}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>Prompt tokens exclude the shared prefix. Creating the handoff note cost {report['handoff_prompt_tokens'] if report['handoff_prompt_tokens'] is not None else 'unmeasured'} prompt tokens. Same-version rereads count reads of a file version already read before the pause. A few stochastic trials cannot establish a general performance advantage.</p>
</main></body></html>"""
    path = output_dir / "experiment.html"
    path.write_text(document, encoding="utf-8")
    return path


def run_experiment(
    source_dir: Path, output_dir: Path, repeats: int = 2,
    model_kind: str | None = None, model_name: str | None = None,
    region: str | None = None, request_timeout: float = 45, retries: int = 1,
    conditions: tuple[str, ...] = ("checkpoint", "minimal"),
    drift_target: str | None = None, drift_from: Path | None = None,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    if not 1 <= repeats <= 5:
        raise ValueError("repeats must be between 1 and 5")
    if not conditions or len(set(conditions)) != len(conditions) or any(condition not in HANDOFF_MODES for condition in conditions):
        raise ValueError(f"conditions must be distinct choices from: {', '.join(HANDOFF_MODES)}")
    if output_dir.is_relative_to(source_dir):
        raise ValueError("Experiment output must be outside the source run")
    source_result = json.loads((source_dir / "result.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((source_dir / "checkpoint.json").read_text(encoding="utf-8"))
    events = trace_events(source_dir / "trace.jsonl")
    if source_result["run_status"] != "PAUSED":
        raise ValueError("Experiment source must be a paused run")
    if checkpoint["last_event_id"] != events[-1]["id"] or checkpoint["last_step"] != source_result["steps"]:
        raise ValueError("Source checkpoint and trace are out of sync")
    digest = workspace_digest(source_dir / "workspace")
    if checkpoint["workspace_digest"] != digest:
        raise ValueError("Source workspace changed after the pause")
    if (drift_target is None) != (drift_from is None):
        raise ValueError("Specify both drift_target and drift_from")
    replacement = None
    drift = None
    if drift_target is not None and drift_from is not None:
        target = safe_file(source_dir / "workspace", drift_target)
        if not target.is_file():
            raise ValueError(f"Drift target is not a workspace file: {drift_target}")
        replacement = drift_from.resolve().read_bytes()
        original = target.read_bytes()
        if replacement == original:
            raise ValueError("Drift replacement must change the target file")
        drift = {"target": drift_target, "replacement_file": str(drift_from.resolve()),
                 "before_sha256": hashlib.sha256(original).hexdigest(),
                 "after_sha256": hashlib.sha256(replacement).hexdigest()}
    handoff_calls = [event for event in events if event["kind"] == "handoff_call"]
    handoff_tokens = prompt_tokens(handoff_calls[-1]["data"].get("usage", {})) if handoff_calls else None
    report: dict[str, Any] = {
        "schema_version": 1,
        "source": str(source_dir),
        "source_step": checkpoint["last_step"],
        "source_event": checkpoint["last_event_id"],
        "workspace_digest": digest,
        "planned_trials": repeats * len(conditions),
        "conditions": list(conditions),
        "drift": drift,
        "handoff_prompt_tokens": handoff_tokens,
        "trials": [],
    }
    report_path = output_dir / "experiment.json"
    if output_dir.exists():
        if not report_path.exists():
            raise ValueError("Existing output has no experiment report to continue")
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        previous.setdefault("conditions", ["checkpoint", "minimal"])
        if any(previous.get(key) != report[key] for key in ("source", "source_step", "source_event", "workspace_digest", "planned_trials", "drift")):
            raise ValueError("Existing experiment does not match this paused source")
        if previous["conditions"] != report["conditions"]:
            raise ValueError("Existing experiment uses different conditions")
        report = previous
    else:
        output_dir.mkdir(parents=True)
    for trial in range(1, repeats + 1):
        order = conditions if trial % 2 else tuple(reversed(conditions))
        for condition in order:
            attempts = [item for item in report["trials"] if item["trial"] == trial and item["condition"] == condition]
            if any(item["metrics"]["run_status"] != "ERROR" for item in attempts):
                continue
            name = f"trial-{trial}-{condition}" + (f"-retry-{len(attempts) + 1}" if attempts else "")
            run_dir = output_dir / name
            shutil.copytree(source_dir, run_dir)
            for filename in ("run.json", "result.json"):
                path = run_dir / filename
                data = json.loads(path.read_text(encoding="utf-8"))
                data["workspace"] = str((run_dir / "workspace").resolve())
                write_json_atomic(path, data)
            if workspace_digest(run_dir / "workspace") != digest:
                raise ValueError(f"Cloned workspace differs from source: {name}")
            if drift and replacement is not None:
                safe_file(run_dir / "workspace", drift["target"]).write_bytes(replacement)
            resumed_digest = workspace_digest(run_dir / "workspace")
            if drift and resumed_digest == digest:
                raise ValueError("Drift did not change the workspace digest")
            print(f"Trial {trial}/{repeats}: resuming {condition}", file=sys.stderr, flush=True)
            resume_task(run_dir, model_kind, model_name, region, request_timeout, retries,
                        handoff_mode=condition)
            render_viewer(run_dir)
            report["trials"].append({"trial": trial, "condition": condition,
                                     "run": name, "workspace_digest_at_resume": resumed_digest,
                                     "metrics": continuation_metrics(run_dir, checkpoint["last_step"])})
            write_json_atomic(report_path, report)
            render_experiment(report, output_dir)
            if report["trials"][-1]["metrics"]["run_status"] == "ERROR":
                raise RuntimeError(f"Trial {name} failed; partial report saved in {output_dir}")
    return report
