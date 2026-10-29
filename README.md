# Handoff Lab

A small coding-agent harness for studying context and handoffs. It records an append-only trace, runs fixed acceptance checks, and can pause a task for a fresh agent session.

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

## Run the file-backed job queue task

`tasks/job_queue.json` is a harder, deterministic task across `store.py`, `job_queue.py`, and `worker.py`. Twelve fixed tests cover duplicate IDs, lease expiry, retry limits, worker failures, and persistence after reopening the store. The seeded fixture starts with four passing tests and eight failures or errors. The task permits up to 28 model actions.

```bash
python3 -m handoff_lab run tasks/job_queue.json --model gemini --model-name gemini-flash-lite-latest --pause-on-stale-test
```

`--pause-on-stale-test` pauses after the agent runs tests and then edits a file, so the checkpoint contains a real agent test result marked stale against the new workspace digest. The trigger does not fire from the harness's initial baseline check. If the agent finishes without that sequence, the run simply continues to its normal outcome.

## Pause and resume with a checkpoint

Use the longer task to compare a fresh session against an uninterrupted run:

```bash
python3 -m handoff_lab run tasks/login_flow.json --model gemini --model-name gemini-flash-lite-latest --pause-after 8
python3 -m handoff_lab resume RUN_DIRECTORY
python3 -m handoff_lab compare UNINTERRUPTED_RUN_DIRECTORY RUN_DIRECTORY
```

The first command saves `checkpoint.json` and exits with `PAUSED`. Resume creates a new model session with empty action history and passes the checkpoint. The model chooses which files to read. The checkpoint lists the goal, file hashes, prior reads and writes, latest test evidence, and an agent-written handoff note. The note is explicitly unverified. Tests are tied to a workspace digest; an edit makes an earlier result stale. Resume detects files changed outside the harness and records them in the trace. The trace viewer shows the handoff request, pause and resume events, the current checkpoint, and the exact first prompt after resume. Each session gets the task's step budget; global step numbers continue across sessions. Pausing uses one additional model call to write the handoff note.

## Compare checkpoint and minimal resumes

Pause one run, then use it as the shared starting state for a paired experiment:

```bash
python3 -m handoff_lab run tasks/login_flow.json --model gemini --model-name gemini-flash-lite-latest --pause-after 8 --out runs/paused-source
python3 -m handoff_lab experiment runs/paused-source --out runs/paired-experiment --repeats 2
```

The experiment clones the paused workspace and trace for each trial. Both conditions start with empty new-session history. The checkpoint condition receives the saved handoff; the minimal condition receives the goal, state, available files, and acceptance command without that handoff. Trial order alternates. `experiment.html` links to each trace and compares post-resume completion, actions, prompt tokens, test runs, and reads of a file version already read before the pause. It reports the shared handoff-generation cost separately. The source run remains paused and unchanged. If a provider error stops a trial, rerun the same experiment command to retry that condition from the paused source; the failed attempt remains visible. A few stochastic trials are an exploratory comparison, not a general performance claim.

To test individual checkpoint components from the same paused source, choose the conditions explicitly:

```bash
python3 -m handoff_lab experiment runs/paused-source --out runs/ablation --repeats 1 --conditions checkpoint,without-notes,without-tests,without-files

# Compare the full checkpoint, a lean evidence checkpoint, and no checkpoint.
python3 -m handoff_lab experiment runs/paused-source --out runs/comparison --repeats 2 --conditions checkpoint,lean,minimal
```

Each `without-*` condition removes just the named checkpoint fields from the fresh session's prompt. `lean` retains the goal, state, file read/write records, and latest test evidence while omitting agent notes and the full file manifest. The saved checkpoint and original paused run remain intact. The file condition removes read/write records and the file manifest; the current file-name list in the ordinary task prompt remains available.

To isolate agent notes and the full file manifest, run all four combinations from the same source:

```bash
python3 -m handoff_lab experiment runs/paused-source --out runs/factorial --repeats 2 --conditions checkpoint,without-notes,without-manifest,lean
```

To test a file changed between pause and resume, provide a replacement file. Each trial receives the same changed bytes, while the paused source stays unchanged:

```bash
python3 -m handoff_lab experiment runs/paused-source --out runs/drift --repeats 2 --conditions checkpoint,without-manifest --drift-target worker.py --drift-from fixtures/job_queue/worker.py
```

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
- `checkpoint.json`: current versioned handoff state and evidence references.
- `viewer.html`: interactive, self-contained timeline. Select an event to inspect its exact recorded data.

Regenerate a viewer after processing a trace with `python3 -m handoff_lab view RUN_DIRECTORY`.

Check the runner's completion gate with `python3 -m unittest discover -s tests -v`.
