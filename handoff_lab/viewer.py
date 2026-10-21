"""Generate a standalone, interactive HTML view of one run."""

from __future__ import annotations

import json
from pathlib import Path


def render_viewer(run_dir: Path) -> Path:
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # Escape '<' so task text cannot close the JSON script tag.
    payload = json.dumps({"result": result, "events": events}, ensure_ascii=False).replace(
        "<", "\\u003c"
    )
    html = HTML.replace("__PAYLOAD__", payload)
    output = run_dir / "viewer.html"
    output.write_text(html, encoding="utf-8")
    return output


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Handoff Lab · Trace Viewer</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1220; --panel:#121d30; --line:#2a3a50;
      --text:#e8eef7; --muted:#9fb1c8; --accent:#64d2b3; --bad:#ff8f89; }
    * { box-sizing:border-box; }
    body { margin:0; font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
      color:var(--text); background:var(--bg); }
    header { padding:22px 28px; border-bottom:1px solid var(--line); background:#101b2d; }
    .eyebrow { color:var(--accent); font-size:11px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
    h1 { margin:3px 0 8px; font-size:24px; }
    .meta { color:var(--muted); display:flex; gap:18px; flex-wrap:wrap; }
    .pill { display:inline-block; padding:2px 9px; border-radius:999px; border:1px solid var(--line); }
    .passed { color:var(--accent); border-color:#2d715e; }
    .failed { color:var(--bad); border-color:#854946; }
    main { display:grid; grid-template-columns:330px minmax(0,1fr); min-height:calc(100vh - 126px); }
    aside { border-right:1px solid var(--line); padding:16px; overflow:auto; }
    section { min-width:0; padding:22px 28px; }
    h2 { margin:0 0 15px; font-size:17px; }
    .event { width:100%; text-align:left; border:1px solid var(--line); background:var(--panel);
      color:var(--text); padding:11px 12px; border-radius:9px; margin-bottom:8px; cursor:pointer; }
    .event:hover,.event.active { border-color:var(--accent); }
    .event strong { display:block; font-size:13px; }
    .event small { color:var(--muted); }
    .event .tag { float:right; color:var(--accent); font-size:11px; }
    .card { border:1px solid var(--line); background:var(--panel); border-radius:10px;
      margin:0 0 16px; overflow:hidden; }
    .card h3 { margin:0; padding:10px 14px; border-bottom:1px solid var(--line); font-size:13px; }
    pre { margin:0; padding:14px; white-space:pre-wrap; overflow-wrap:anywhere;
      font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace; color:#dbe8f7; max-height:58vh; overflow:auto; }
    .hint { color:var(--muted); }
    @media (max-width:750px) { main { grid-template-columns:1fr; } aside { border-right:0;
      border-bottom:1px solid var(--line); max-height:320px; } section { padding:18px; } }
  </style>
</head>
<body>
<header>
  <div class="eyebrow">Handoff Lab / Session 1</div>
  <h1 id="title"></h1>
  <div class="meta"><span id="status"></span><span id="model"></span><span id="steps"></span></div>
  <pre id="run-error" hidden style="color:var(--bad);padding:12px 0 0;max-height:none"></pre>
</header>
<main>
  <aside><h2>Trace timeline</h2><div id="timeline"></div></aside>
  <section><h2 id="detail-title">Select an event</h2><div id="details" class="hint">Every model input, tool result, and acceptance check is inspectable here.</div></section>
</main>
<script id="trace-data" type="application/json">__PAYLOAD__</script>
<script>
const data = JSON.parse(document.getElementById('trace-data').textContent);
const {result, events} = data;
document.getElementById('title').textContent = result.title;
document.getElementById('status').innerHTML = '<span class="pill ' + (result.passed ? 'passed' : 'failed') + '">' + result.state + '</span>';
document.getElementById('model').textContent = 'Model: ' + result.model;
document.getElementById('steps').textContent = 'Steps: ' + result.steps;
if (result.error) {
  document.getElementById('status').textContent = 'Run error · task state: ' + result.state;
  const error = document.getElementById('run-error');
  error.hidden = false;
  error.textContent = result.error;
}
const timeline = document.getElementById('timeline');
function label(e) {
  const d = e.data;
  if (e.kind === 'model_call') return 'Model chose ' + (d.action?.action || 'action');
  if (e.kind === 'tool_result') return 'Tool result: ' + d.action;
  if (e.kind === 'acceptance_check') return (d.phase || 'check') + ' check: ' + (d.passed ? 'passed' : 'failed');
  if (e.kind === 'state') return 'State: ' + d.to_state;
  return e.kind;
}
function card(title, value) {
  const wrap = document.createElement('div'); wrap.className = 'card';
  const heading = document.createElement('h3'); heading.textContent = title;
  const pre = document.createElement('pre');
  pre.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  wrap.append(heading, pre); return wrap;
}
function select(e, button) {
  document.querySelectorAll('.event').forEach(x => x.classList.remove('active'));
  button.classList.add('active');
  document.getElementById('detail-title').textContent = 'Step ' + e.step + ' · ' + label(e);
  const details = document.getElementById('details'); details.replaceChildren();
  if (e.kind === 'model_call') {
    details.append(card('Exact model input', e.data.input), card('Chosen action', e.data.action), card('Usage', e.data.usage));
  } else if (e.kind === 'tool_result') {
    details.append(card('Tool observation', e.data.result));
  } else if (e.kind === 'acceptance_check') {
    details.append(card('Command and result', e.data));
  } else {
    details.append(card('Event', e.data));
  }
}
events.forEach(e => {
  const button = document.createElement('button'); button.className = 'event';
  const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = e.id;
  const strong = document.createElement('strong'); strong.textContent = label(e);
  const small = document.createElement('small'); small.textContent = 'Step ' + e.step + ' · ' + e.time;
  button.append(tag, strong, small); button.onclick = () => select(e, button);
  timeline.append(button);
});
const first = timeline.querySelector('.event'); if (first) first.click();
</script>
</body>
</html>
"""
