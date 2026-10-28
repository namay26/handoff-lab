"""Command-line entry points for running and viewing a task."""

from __future__ import annotations

import argparse
import json
import os
import math
from datetime import datetime
from pathlib import Path

from .comparison import summarize_run
from .experiment import run_experiment
from .runner import HANDOFF_MODES, resume_task, run_task
from .models import list_gemini_models
from .viewer import render_viewer


PROJECT = Path(__file__).resolve().parent.parent


def load_gemini_key() -> None:
    env_file = PROJECT / ".env"
    if not env_file.is_file() or os.environ.get("GEMINI_API_KEY"):
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            key = line.partition("=")[2].strip().strip('"\'')
            if key:
                os.environ["GEMINI_API_KEY"] = key
            return


def main() -> None:
    load_gemini_key()
    parser = argparse.ArgumentParser(prog="handoff", description="Run and inspect coding-agent traces")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a coding task")
    run.add_argument("task", type=Path, nargs="?", default=PROJECT / "tasks" / "login.json")
    run.add_argument("--model", choices=["scripted", "gemini", "bedrock", "openai"], default="scripted")
    run.add_argument("--model-name", help="required with a live model")
    run.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"), help="AWS region for Bedrock")
    run.add_argument("--out", type=Path, help="new run directory")
    run.add_argument("--request-timeout", type=float, default=45, help="Gemini socket timeout in seconds (default: 45)")
    run.add_argument("--retries", type=int, default=1, help="Gemini transient-error retries, 0 to 3 (default: 1)")
    run.add_argument("--context-policy", choices=["full", "compact"], default="full", help="model history policy (default: full)")
    run.add_argument("--context-threshold", type=int, default=1500, help="compact after a prompt reaches this token count (default: 1500)")
    run.add_argument("--pause-after", type=int, help="pause after this many steps and write a handoff checkpoint")
    run.add_argument("--pause-on-stale-test", action="store_true", help="pause after an agent test is made stale by a later edit")

    resume = sub.add_parser("resume", help="resume a paused run in a fresh agent session")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--model", choices=["scripted", "gemini", "bedrock", "openai"])
    resume.add_argument("--model-name")
    resume.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    resume.add_argument("--request-timeout", type=float, default=45)
    resume.add_argument("--retries", type=int, default=1)
    resume.add_argument("--context-policy", choices=["full", "compact"])
    resume.add_argument("--context-threshold", type=int)
    resume.add_argument("--pause-after", type=int)
    resume.add_argument("--pause-on-stale-test", action="store_true")
    resume.add_argument("--handoff-mode", choices=HANDOFF_MODES, default="checkpoint")

    experiment = sub.add_parser("experiment", help="compare checkpoint and minimal resumes from one paused run")
    experiment.add_argument("source", type=Path, help="paused source run")
    experiment.add_argument("--out", type=Path, required=True, help="new experiment directory")
    experiment.add_argument("--repeats", type=int, default=2, help="trials per condition (default: 2)")
    experiment.add_argument("--conditions", default="checkpoint,minimal", help="comma-separated prompt conditions")
    experiment.add_argument("--model", choices=["scripted", "gemini", "bedrock", "openai"])
    experiment.add_argument("--model-name")
    experiment.add_argument("--region", default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"))
    experiment.add_argument("--request-timeout", type=float, default=45)
    experiment.add_argument("--retries", type=int, default=1)

    view = sub.add_parser("view", help="regenerate the HTML trace viewer")
    view.add_argument("run_dir", type=Path)
    compare = sub.add_parser("compare", help="compare saved run outcomes and context costs")
    compare.add_argument("run_dirs", type=Path, nargs="+", help="run directories to compare")
    sub.add_parser("models", help="list Gemini models available to your API key")

    args = parser.parse_args()
    if args.command == "models":
        try:
            print("\n".join(list_gemini_models()))
        except (ValueError, RuntimeError, OSError) as exc:
            parser.error(str(exc))
        return
    if args.command == "view":
        print(render_viewer(args.run_dir.resolve()))
        return
    if args.command == "compare":
        try:
            print(json.dumps([summarize_run(path) for path in args.run_dirs], indent=2))
        except (OSError, ValueError, KeyError) as exc:
            parser.error(str(exc))
        return

    if args.command == "run" and args.model in {"openai", "bedrock", "gemini"} and not args.model_name:
        parser.error("--model-name is required with a live model")
    if args.model == "bedrock" and not args.region:
        parser.error("--region or AWS_REGION is required with --model bedrock")
    if not math.isfinite(args.request_timeout) or args.request_timeout <= 0:
        parser.error("--request-timeout must be a positive number")
    if not 0 <= args.retries <= 3:
        parser.error("--retries must be between 0 and 3")
    if getattr(args, "context_threshold", None) is not None and args.context_threshold <= 0:
        parser.error("--context-threshold must be positive")
    if args.command == "experiment":
        conditions = tuple(part.strip() for part in args.conditions.split(","))
        try:
            report = run_experiment(args.source, args.out, args.repeats, args.model,
                                    args.model_name, args.region, args.request_timeout, args.retries,
                                    conditions)
        except (ValueError, FileExistsError, OSError, KeyError, RuntimeError) as exc:
            parser.error(str(exc))
        print(json.dumps({"report": str(args.out.resolve() / "experiment.json"),
                          "viewer": str(args.out.resolve() / "experiment.html"),
                          "completed_trials": sum(item["metrics"]["run_status"] != "ERROR" for item in report["trials"]),
                          "attempts": len(report["trials"])}, indent=2))
        return
    try:
        if args.command == "run":
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            run_dir = (args.out or PROJECT / "runs" / f"{stamp}-{args.model}").resolve()
            result = run_task(
                args.task, run_dir, args.model, args.model_name, args.region,
                args.request_timeout, args.retries, args.context_policy, args.context_threshold,
                args.pause_after, args.pause_on_stale_test,
            )
        else:
            run_dir = args.run_dir.resolve()
            result = resume_task(
                run_dir, args.model, args.model_name, args.region,
                args.request_timeout, args.retries, args.context_policy, args.context_threshold,
                args.pause_after, args.handoff_mode, args.pause_on_stale_test,
            )
    except (ValueError, FileExistsError, OSError, KeyError) as exc:
        parser.error(str(exc))
    viewer = render_viewer(run_dir)
    print(json.dumps({"run_status": result["run_status"], "state": result["state"], "passed": result["passed"], "steps": result["steps"], "sessions": result["sessions"], "prompt_tokens": result["prompt_tokens"], "compactions": result["compactions"], "error": result["error"], "run": str(run_dir), "checkpoint": str(run_dir / "checkpoint.json"), "viewer": str(viewer)}, indent=2))
    if result["run_status"] not in {"COMPLETED", "PAUSED"}:
        raise SystemExit(1)
