# Handoff Lab

A small coding-agent harness for studying context and handoffs. The current build includes a single-agent loop, an append-only JSONL trace, an acceptance check, and a standalone HTML trace viewer.

## Run the included task

From this project directory, use Python 3.11 or newer. No packages are needed for the demo.

```bash
python3 -m handoff_lab run
```

The default **scripted demo actor** reads `auth.py`, fixes an intentional password-checking bug, runs the tests, and asks to finish. The harness independently reruns the test command before marking the run `DONE`. The command prints a path to `viewer.html`; open it in a browser to inspect each step. The source fixture is copied into a new run directory, so the sample remains unchanged.

## Run with your Gemini key

In a terminal, enter the project directory and load your key. The hidden prompt keeps the key out of terminal history. This needs no extra Python packages or region setting.

```bash
read -rsp 'Gemini API key: ' GEMINI_API_KEY; echo
export GEMINI_API_KEY
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
