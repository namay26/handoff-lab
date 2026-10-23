# Handoff Lab

A small coding-agent harness for studying context and handoffs. The current build includes a single-agent loop, an append-only JSONL trace, an acceptance check, and a standalone HTML trace viewer.

## Run the included task

From this project directory, use Python 3.11 or newer. No packages are needed for the demo.

```bash
python3 -m handoff_lab run
```

The default **scripted demo actor** reads `auth.py`, fixes an intentional password-checking bug, runs the tests, and asks to finish. The harness independently reruns the test command before marking the run `DONE`. The command prints a path to `viewer.html`; open it in a browser to inspect each step. The source fixture is copied into a new run directory, so the sample remains unchanged.

## Run the longer login-flow task

`tasks/login_flow.json` is a three-file task covering credential checks, active sessions, profile access, and logout. It allows up to 16 model steps. The fixed tests start with three failures, and the agent may edit only `auth.py`, `sessions.py`, and `app.py` in its isolated workspace. Run it with a live model:

```bash
python3 -m handoff_lab run tasks/login_flow.json --model gemini --model-name gemini-2.5-flash-lite
```

The scripted demo actor is specific to the smaller `tasks/login.json` fixture.

## Compare full and compact context

The default `full` policy sends every prior action and tool result at each step. The `compact` policy waits until a model call reaches the token threshold, then replaces older history with a small structured summary while preserving the latest three action–observation steps. The summary records files read, files edited, and the latest test outcome; it omits old file contents and test logs. The agent can read a file or rerun tests when it needs those details. Compaction uses no extra model call, and the full trace remains available for inspection.

```bash
python3 -m handoff_lab run tasks/login_flow.json --model gemini --model-name gemini-flash-lite-latest --context-policy compact --context-threshold 1500
```

The threshold uses the previous call's reported input tokens, so a compaction takes effect on the following step. The trace viewer shows each compaction, the summary, the retained steps, and every exact model input. The run result reports total prompt tokens and compaction count. Compare saved runs with `python3 -m handoff_lab compare RUN_DIR_1 RUN_DIR_2`; completion, repeated file reads, and token use all matter. A saved trace can be replayed to inspect context construction, but it cannot predict what a model would do with different context.

## Run with your Gemini key

Paste your key after `GEMINI_API_KEY=` in the project-root `.env` file. The CLI loads it automatically, and Git ignores the file. An already exported `GEMINI_API_KEY` takes precedence. This needs no extra Python packages or region setting.

```bash
python3 -m handoff_lab models
```

The last command lists models supporting `generateContent` for that API endpoint. It does not guarantee quota for every model. Choose a text-capable Gemini model and copy its exact name into the command below:

```bash
python3 -m handoff_lab run --model gemini --model-name MODEL_NAME_FROM_LIST
```

Open the printed `viewer.html` path after the run. The fixture permits edits only to `auth.py`; the acceptance tests stay fixed. Gemini uses the documented [Generate Content API](https://ai.google.dev/api/generate-content) with the key in a header. The key is not recorded in the trace. A live `gemini-2.5-flash-lite` run has completed the included task in four steps.

The terminal prints each step and request attempt. Gemini uses a 45-second socket timeout and one retry for timeouts, dropped connections, and selected transient server errors. Quota errors stop immediately. Adjust these with `--request-timeout 60 --retries 1`, or use `--retries 0` when comparing policies without automatic retries. A socket timeout measures network inactivity, not total wall-clock duration. Retries are recorded in the trace; a timed-out generation may still have been processed by the provider.

For a run with Amazon Bedrock, set your Bedrock API key in the terminal, then pass a model ID enabled for that key and its AWS region:

```bash
read -rsp 'Bedrock API key: ' AWS_BEARER_TOKEN_BEDROCK; echo
export AWS_BEARER_TOKEN_BEDROCK
python3 -m handoff_lab run --model bedrock --model-name YOUR_MODEL_ID --region YOUR_AWS_REGION
```

The Bedrock adapter uses AWS's [Converse endpoint and bearer-token authentication](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-use.html). The key is read from the environment and is not written to the trace. Do not paste the key into a task file or a chat message. If you use a different AWS credential type rather than a Bedrock API key, this adapter does not yet support it.

The optional OpenAI adapter remains available with `--model openai --model-name YOUR_MODEL_ID` and `OPENAI_API_KEY`. Live adapters require API credentials and network access.

## Inspect a run

Each run directory contains:

- `workspace/`: isolated copy of the task fixture, including the resulting code.
- `trace.jsonl`: every model input, chosen action, tool result, state change, and acceptance-check result.
- `run.json` and `result.json`: run metadata and final outcome.
- `viewer.html`: interactive, self-contained timeline. Select an event to inspect its exact recorded data.

Regenerate a viewer after processing a trace with `python3 -m handoff_lab view RUN_DIRECTORY`.

Check the runner's completion gate with `python3 -m unittest discover -s tests -v`.

## First build-session boundary

The runner currently sends the complete recorded history to the model at every step. Context policies, forced resets, durable task checkpoints, and paired comparisons belong to the next build sessions. The trace format exists so those changes can be inspected and measured against this baseline.
