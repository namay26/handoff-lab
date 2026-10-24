"""A repeatable demo actor and optional live-model adapters."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


SYSTEM_INSTRUCTIONS = """You are fixing one small coding task in a sandboxed workspace.
If handoff_only is true, return only a JSON object with this shape:
  {"action":"handoff","learned":[{"file":"relative/path.py","interpretation":"what you infer"}],"remaining":["pending work"],"next_action":"specific next action"}
These interpretations are unverified notes. Do not present them as test-backed facts.
Otherwise, choose exactly one next action from the actions below.
Return only a JSON object. Available actions:
  {"action":"read_file","path":"relative/path.py"}
  {"action":"write_file","path":"relative/path.py","content":"entire new file"}
  {"action":"run_tests"}
  {"action":"finish","message":"brief result"}
Read relevant code before changing it. Run tests after changes. Only use paths inside the workspace.
The harness, not your finish message, decides whether the task passed.
"""


@dataclass
class ModelReply:
    action: dict[str, Any]
    raw_text: str
    usage: dict[str, Any]


def gemini_request(path: str, payload: dict[str, Any] | None = None, *, timeout: float = 45, retries: int = 1, on_event=None) -> dict[str, Any]:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise ValueError("GEMINI_API_KEY is required for Gemini")
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/" + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    for attempt in range(1, retries + 2):
        print(f"  Gemini request {attempt}/{retries + 1} (socket timeout {timeout:g}s)", file=sys.stderr, flush=True)
        if on_event:
            on_event("request_attempt", attempt=attempt, socket_timeout_seconds=timeout)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").replace(key, "[redacted]")
            try:
                detail = json.loads(body).get("error", {}).get("message", body)
            except json.JSONDecodeError:
                detail = body
            error = f"Gemini returned HTTP {exc.code}: {str(detail)[:2000]}"
            transient = exc.code in {408, 500, 502, 503, 504}
        except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
            error = f"Gemini connection failed after attempt {attempt}: {str(exc).replace(key, '[redacted]')}"
            transient = isinstance(exc, (TimeoutError, ConnectionError)) or isinstance(getattr(exc, "reason", None), (TimeoutError, ConnectionError))
        if not transient or attempt > retries:
            raise RuntimeError(error)
        delay = 2 ** (attempt - 1)
        print(f"  Request stalled or service unavailable; retrying in {delay}s", file=sys.stderr, flush=True)
        if on_event:
            on_event("request_retry", attempt=attempt, delay_seconds=delay, error=error)
        time.sleep(delay)
    raise RuntimeError("Gemini request attempts exhausted")


def list_gemini_models() -> list[str]:
    names = []
    page = ""
    while True:
        path = "models?pageSize=1000"
        if page:
            path += "&pageToken=" + urllib.parse.quote(page, safe="")
        result = gemini_request(path)
        names.extend(
            model["name"].removeprefix("models/")
            for model in result.get("models", [])
            if "generateContent" in model.get("supportedGenerationMethods", [])
        )
        page = result.get("nextPageToken", "")
        if not page:
            return sorted(names)


class GeminiModel:
    def __init__(self, model_name: str, timeout: float = 45, retries: int = 1) -> None:
        if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            raise ValueError("GEMINI_API_KEY is required for --model gemini")
        if not model_name:
            raise ValueError("--model-name is required for --model gemini")
        self.model_name = model_name.removeprefix("models/")
        self.label = f"gemini:{self.model_name}"
        self.timeout = timeout
        self.retries = retries
        self.on_event = None

    def next_action(self, context: dict[str, Any]) -> ModelReply:
        result = gemini_request(
            "models/" + urllib.parse.quote(self.model_name, safe="") + ":generateContent",
            {
                "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTIONS}]},
                "contents": [{"role": "user", "parts": [{"text": json.dumps(context, ensure_ascii=False)}]}],
                "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 4096},
            },
            timeout=self.timeout, retries=self.retries, on_event=self.on_event,
        )
        candidates = result.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini returned no candidate; check promptFeedback in the API response")
        raw_text = "\n".join(
            part["text"] for part in candidates[0].get("content", {}).get("parts", [])
            if "text" in part and not part.get("thought")
        ).strip()
        try:
            action = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Gemini returned an incomplete or non-JSON action: {raw_text[:400]}") from exc
        if not isinstance(action, dict):
            raise RuntimeError("Gemini action must be a JSON object")
        return ModelReply(action=action, raw_text=raw_text, usage=result.get("usageMetadata", {}))


class ScriptedModel:
    """A fixed four-step actor for verifying the harness without credentials."""

    label = "scripted-demo"

    def __init__(self) -> None:
        self.index = 0
        self.actions = [
            {"action": "read_file", "path": "auth.py"},
            {
                "action": "write_file",
                "path": "auth.py",
                "content": (
                    '"""Tiny example app with password checking."""\n\n'
                    'USERS = {"ada": "correct-horse"}\n\n\n'
                    'def authenticate(username: str, password: str) -> bool:\n'
                    '    return username in USERS and USERS[username] == password\n'
                ),
            },
            {"action": "run_tests"},
            {"action": "finish", "message": "Password validation fixed; tests ran."},
        ]

    def next_action(self, context: dict[str, Any]) -> ModelReply:
        if context.get("handoff_only"):
            action = {"action": "handoff", "learned": [], "remaining": ["Continue the task"], "next_action": "Read the relevant file"}
            return ModelReply(action=action, raw_text=json.dumps(action), usage={})
        if self.index >= len(self.actions):
            action = {"action": "finish", "message": "No more scripted actions."}
        else:
            action = self.actions[self.index]
        self.index += 1
        return ModelReply(action=action, raw_text=json.dumps(action), usage={})


class OpenAIModel:
    """One explicit next-action request per agent step using the Responses API."""

    def __init__(self, model_name: str) -> None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY is required for --model openai")
        if not model_name:
            raise ValueError("--model-name is required for --model openai")
        self.key = key
        self.model_name = model_name
        self.label = f"openai:{model_name}"

    def next_action(self, context: dict[str, Any]) -> ModelReply:
        payload = {
            "model": self.model_name,
            "instructions": SYSTEM_INSTRUCTIONS,
            "input": [
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)}
            ],
            "store": False,
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            raise RuntimeError(f"OpenAI API returned HTTP {exc.code}: {detail}") from exc

        raw_text = "\n".join(
            part.get("text", "")
            for item in result.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ).strip()
        try:
            action = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Model returned non-JSON action: {raw_text[:400]}") from exc
        if not isinstance(action, dict):
            raise RuntimeError("Model action must be a JSON object")
        return ModelReply(action=action, raw_text=raw_text, usage=result.get("usage", {}))


class BedrockModel:
    """One JSON action per step through Amazon Bedrock's Converse endpoint."""

    def __init__(self, model_name: str, region: str) -> None:
        key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        if not key:
            raise ValueError("AWS_BEARER_TOKEN_BEDROCK is required for --model bedrock")
        if not model_name:
            raise ValueError("--model-name is required for --model bedrock")
        if not re.fullmatch(r"[a-z0-9-]+", region):
            raise ValueError("--region must be an AWS region such as us-east-1")
        self.key = key
        self.model_name = model_name
        self.region = region
        self.label = f"bedrock:{region}:{model_name}"

    def next_action(self, context: dict[str, Any]) -> ModelReply:
        model_path = urllib.parse.quote(self.model_name, safe="")
        url = f"https://bedrock-runtime.{self.region}.amazonaws.com/model/{model_path}/converse"
        payload = {
            "system": [{"text": SYSTEM_INSTRUCTIONS}],
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": json.dumps(context, ensure_ascii=False)}],
                }
            ],
            "inferenceConfig": {"maxTokens": 2048},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            raise RuntimeError(f"Bedrock returned HTTP {exc.code}: {detail}") from exc

        raw_text = "\n".join(
            part.get("text", "")
            for part in result.get("output", {}).get("message", {}).get("content", [])
            if "text" in part
        ).strip()
        try:
            action = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Bedrock model returned non-JSON action: {raw_text[:400]}") from exc
        if not isinstance(action, dict):
            raise RuntimeError("Bedrock model action must be a JSON object")
        return ModelReply(action=action, raw_text=raw_text, usage=result.get("usage", {}))
