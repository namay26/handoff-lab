"""Command-line entry points for running and viewing a task."""

from __future__ import annotations

import argparse
import json
import os
import math
from datetime import datetime
from pathlib import Path

from .runner import run_task
from .models import list_gemini_models
from .viewer import render_viewer


PROJECT = Path(__file__).resolve().parent.parent


def main() -> None:
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

    view = sub.add_parser("view", help="regenerate the HTML trace viewer")
    view.add_argument("run_dir", type=Path)
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

    if args.model in {"openai", "bedrock", "gemini"} and not args.model_name:
        parser.error("--model-name is required with a live model")
    if args.model == "bedrock" and not args.region:
        parser.error("--region or AWS_REGION is required with --model bedrock")
    if not math.isfinite(args.request_timeout) or args.request_timeout <= 0:
        parser.error("--request-timeout must be a positive number")
    if not 0 <= args.retries <= 3:
        parser.error("--retries must be between 0 and 3")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = (args.out or PROJECT / "runs" / f"{stamp}-{args.model}").resolve()
    try:
        result = run_task(args.task, run_dir, args.model, args.model_name, args.region, args.request_timeout, args.retries)
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    viewer = render_viewer(run_dir)
    print(json.dumps({"run_status": result["run_status"], "state": result["state"], "passed": result["passed"], "error": result["error"], "run": str(run_dir), "viewer": str(viewer)}, indent=2))
    if not result["passed"]:
        raise SystemExit(1)
