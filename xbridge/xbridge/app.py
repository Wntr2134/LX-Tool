"""XBridge's window: one panel, one job.

A small local FastAPI app showing bridge status, start/stop, and the
mapping editor. The desktop build wraps it in a native window; `xbridge
web` serves it to a browser. All state lives in the Runner and the stored
mapping - the page is a remote control for them.
"""

from __future__ import annotations

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

app = FastAPI(title="XBridge")

_runner = None
_thread = None


@app.get("/api/status")
def api_status() -> dict:
    from . import run as xrun

    r = _runner
    return {
        "available": xrun.midi_available(),
        "running": _thread is not None and _thread.is_alive(),
        "state": r.state if r else "stopped",
        "detail": r.detail if r else "",
        "port": r.midi_name if r else "",
        "counters": r.counters if r else {},
        "sent": list(r.last_sent) if r else [],
        "midi": list(r.last_midi) if r else [],
    }


@app.post("/api/test-send")
def api_test_send(strip: int = Form(0), level: float = Form(0.5)) -> dict:
    """Send one fader value as if the surface had moved, so the console
    can be proved without touching hardware."""
    if _runner is None:
        raise HTTPException(409, "start the bridge first")
    sent = _runner.test_send(strip=strip, level=level)
    if not sent:
        raise HTTPException(400, "this target has no fader for that strip")
    return {"sent": sent, "to": f"{_runner.ma3[0]}:{_runner.ma3[1]}"}


@app.get("/api/ports")
def api_ports() -> dict:
    """What MIDI this machine can see - the first thing to check when a
    surface will not connect."""
    from . import run as xrun

    if not xrun.midi_available():
        return {"available": False, "inputs": [], "outputs": []}
    import mido

    try:
        return {"available": True,
                "inputs": list(mido.get_input_names()),
                "outputs": list(mido.get_output_names())}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "inputs": [], "outputs": [],
                "error": str(exc)}


@app.post("/api/start")
def api_start(host: str = Form("127.0.0.1"), send_port: int = Form(0),
              recv_port: int = Form(9000), target: str = Form(""),
              surface: str = Form(""), midi_port: str = Form("")) -> dict:
    """Start the bridge. midi_port empty = find the surface by name.

    Naming a port matters for anything the hints cannot recognise: an
    X32 over DIN MIDI arrives under the MIDI interface's name, and over
    RTPMIDI under whatever the session was called.
    """
    import threading

    from . import run as xrun

    global _runner, _thread
    if not xrun.midi_available():
        raise HTTPException(409, "MIDI support is not installed - run: "
                            "pip install mido python-rtmidi and restart")
    if _thread is not None and _thread.is_alive():
        raise HTTPException(409, "the bridge is already running")

    store = xrun.config_store_path()
    _runner = xrun.Runner(ma3_host=host, send_port=send_port,
                          recv_port=recv_port,
                          config_path=str(store) if store.is_file() else "",
                          target=target, surface=surface,
                          midi_port=midi_port,
                          log=lambda *a: None)
    _thread = threading.Thread(target=_runner.run, daemon=True)
    _thread.start()
    return {"ok": True}


@app.post("/api/stop")
def api_stop() -> dict:
    if _runner is not None:
        _runner.stop()
    return {"ok": True}


@app.get("/api/config")
def api_config() -> dict:
    from dataclasses import fields as dc_fields

    from . import run as xrun

    cfg = xrun.load_stored_config()
    body = {}
    for f in dc_fields(cfg):
        v = getattr(cfg, f.name)
        body[f.name] = list(v) if isinstance(v, tuple) else v
    return {"config": body, "path": str(xrun.config_store_path())}


@app.post("/api/config")
async def api_config_save(request: Request) -> dict:
    from . import run as xrun

    try:
        data = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "expected a JSON object of config fields")
    try:
        xrun.store_config(data)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "path": str(xrun.config_store_path())}


@app.post("/api/probe")
def api_probe(host: str = Form("127.0.0.1"), port: int = Form(8000),
              page: int = Form(1), exec_no: int = Form(201),
              dwell: float = Form(2.0)) -> dict:
    """Walk every MA3 OSC dialect against one executor.

    MA3 discards anything whose prefix does not match, without saying so,
    so "the fader does nothing" has several indistinguishable causes.
    This puts each candidate on the wire in turn; whichever one moves the
    executor is the answer, and /api/probe/apply keeps it.
    """
    from .probe import Ma3Probe

    if dwell < 0 or dwell > 10:
        raise HTTPException(400, "dwell must be between 0 and 10 seconds")
    p = Ma3Probe(host=host, port=port, page=page, exec_=exec_no)
    try:
        p.run(dwell=dwell)
    except OSError as exc:
        raise HTTPException(400, f"could not send to {host}:{port}: {exc}") from exc
    return {"to": f"{host}:{port}", "exec": exec_no, "page": page,
            "steps": [s.as_dict() for s in p.steps]}


@app.post("/api/probe/apply")
def api_probe_apply(index: int = Form(...)) -> dict:
    """Keep the dialect that worked, so it is never guessed again."""
    from . import run as xrun
    from .probe import DIALECTS

    if not 0 <= index < len(DIALECTS):
        raise HTTPException(400, "no such probe step")
    prefix, value, addr_form = DIALECTS[index]
    # store_config writes exactly what it is given, so the rest of the
    # mapping has to be carried over or the probe would erase it.
    body = api_config()["config"]
    body.update({"prefix": prefix, "ma3_value": value, "ma3_addr": addr_form})
    xrun.store_config(body)
    return {"ok": True, "prefix": prefix, "ma3_value": value,
            "ma3_addr": addr_form}


@app.get("/api/ma3-setup")
def api_ma3_setup(host: str = "127.0.0.1", send_port: int = 8000,
                  recv_port: int = 9000, prefix: str = "",
                  bridge_ip: str = "127.0.0.1") -> dict:
    """The console-side OSC lines to create, with these ports filled in.

    An MA3 OSC line has a single Port cell used for both directions, so
    "which box is the send port" has no answer - it takes two lines. That
    trips up everyone, so the app spells out the rows rather than
    describing the rule.
    """
    from . import ma3setup

    kw = dict(host=host, send_port=send_port, recv_port=recv_port)
    return {
        "toggles": [{"name": n, "value": v, "note": note}
                    for n, v, note in ma3setup.GLOBAL_TOGGLES],
        "lines": [ln.as_dict()
                  for ln in ma3setup.lines(prefix=prefix,
                                           bridge_ip=bridge_ip, **kw)],
        "warnings": ma3setup.warnings(**kw),
        "feedback": ma3setup.feedback_note(),
    }


@app.get("/api/config/export")
def api_config_export() -> FileResponse:
    from . import run as xrun

    p = xrun.config_store_path()
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(xrun.default_config_json(), encoding="utf-8")
    return FileResponse(p, filename="xbridge-mapping.json",
                        media_type="application/json")


@app.post("/api/config/import")
async def api_config_import(file: UploadFile = File(...)) -> dict:
    import json as jsonlib

    from . import run as xrun

    try:
        data = jsonlib.loads((await file.read()).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, f"not a mapping file: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "not a mapping file: expected a JSON object")
    cfg = xrun.store_config(data)
    return {"ok": True, "target": cfg.target}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>XBridge</title>
<style>
 :root{--bg:#14161b;--card:#1d212a;--card2:#232834;--ink:#e9e6df;--dim:#8a919e;
   --line:#303645;--amber:#ffb454;--good:#58c08a;--bad:#e0705a;--blue:#6ea8d8}
 body{background:var(--bg);color:var(--ink);margin:0;
   font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:1.2rem}
 .wrap{max-width:46rem;margin:0 auto}
 html,body{max-width:100%;overflow-x:hidden}
 h1{font-size:1.15rem;margin:.2rem 0 .1rem}
 h1 small{color:var(--amber);font-weight:500;font-size:.75rem;
   font-family:ui-monospace,Menlo,monospace}
 p.tag{color:var(--dim);font-size:.82rem;margin:.1rem 0 1rem}
 fieldset{background:var(--card);border:1px solid var(--line);border-radius:10px;
   padding:.9rem 1rem;margin:0 0 1rem}
 legend{font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;
   color:var(--dim);padding:0 .4rem}
 label{color:var(--dim);font-size:.8rem;margin-right:.15rem}
 input,select{background:var(--card2);border:1px solid var(--line);
   color:var(--ink);border-radius:6px;padding:.35rem .5rem;font-size:.85rem}
 input[type=number]{width:5.5rem}
 button{background:var(--amber);color:#1a1408;border:0;border-radius:6px;
   padding:.42rem .85rem;font-weight:600;font-size:.85rem;cursor:pointer}
 button.ghost{background:transparent;color:var(--ink);border:1px solid var(--line)}
 .row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin:.35rem 0}
 /* Wrapping, not nowrap: a long status line used to widen the whole
    page, which gave the control row enough width not to wrap - so on a
    small screen the last controls sat off to the right, invisible. */
 #out{font-family:ui-monospace,Menlo,monospace;font-size:.76rem;
   background:var(--card2);border-radius:6px;padding:.5rem .6rem;margin-top:.6rem;
   min-height:1.2rem;white-space:pre-wrap;overflow-wrap:anywhere}
 #out .ok{color:var(--good)} #out .err{color:var(--bad)}
 .gridwrap{overflow-x:auto;margin-top:.5rem}
 table{border-collapse:collapse;font-size:.74rem;
   font-family:ui-monospace,Menlo,monospace}
 th,td{border:1px solid var(--line);padding:.25rem .4rem;text-align:center;
   font-variant-numeric:tabular-nums}
 th{color:var(--dim);font-weight:500}
 td input{width:3.6rem;text-align:center;padding:.2rem .2rem}
 .note{color:var(--dim);font-size:.78rem}
 table.setup{width:100%;font-family:inherit;font-size:.82rem}
 table.setup td{text-align:left}
 table.setup td:first-child{color:var(--dim);white-space:nowrap;width:11rem}
 table.setup th{text-align:left;color:var(--amber);padding-top:.5rem}
 table.setup .note{font-size:.72rem;margin-top:.1rem}
 code{color:var(--blue)}
 :focus-visible{outline:2px solid var(--amber);outline-offset:1px}
</style></head><body><div class="wrap">
<h1>XBridge <small>X-Touch &rarr; anything with faders</small></h1>
<p class="tag">grandMA3 &middot; grandMA2 &middot; MagicQ &middot; Eos &middot; X32/M32 &middot; Resolume &middot; Companion &middot; any OSC.
Motors follow the console both ways. Stream Decks reach the bridge through
Companion at <code>/xbridge/...</code> on the listen port.</p>

<fieldset><legend>Bridge</legend>
 <div class="row">
  <label>Target</label>
  <select id="target" onchange="renderMap()">
    <option value="ma3">grandMA3 onPC</option>
    <option value="ma2">grandMA2 onPC (web remote)</option>
    <option value="magicq">ChamSys MagicQ</option>
    <option value="eos">ETC Eos family</option>
    <option value="x32">Behringer X32 / M32</option>
    <option value="resolume">Resolume Arena/Avenue</option>
    <option value="companion">Bitfocus Companion</option>
    <option value="generic">Generic OSC templates</option>
  </select>
  <label>Surface</label>
  <select id="surface">
    <option value="xtouch">X-Touch (full size)</option>
    <option value="mpk">Akai MPK Mini (knobs + pads)</option>
    <option value="x32mc">X32 / M32 (Setup &rarr; Remote &rarr; Mackie Control)</option>
    <option value="xtouch,mpk">X-Touch + MPK Mini (both at once)</option>
    <option value="xtouch,x32mc">X-Touch + X32 (both at once)</option>
  </select>
  <label>host</label><input type="text" id="host" value="127.0.0.1" style="width:9rem">
  <label>send</label><input type="number" id="send" placeholder="auto">
  <label>listen</label><input type="number" id="recv" value="9000">
  <label>MIDI port</label>
  <select id="midiport" title="auto = find the surface by name">
   <option value="">auto-detect</option>
  </select>
 </div>
 <div class="row">
  <button onclick="start()">Start bridge</button>
  <button class="ghost" onclick="stop()">Stop</button>
  <button class="ghost" onclick="toggleMap()">Remap&hellip;</button>
  <button class="ghost" onclick="showPorts()">MIDI ports</button>
  <button class="ghost" onclick="testSend()">Test fader 1</button>
  <button class="ghost" onclick="probe()">Find MA3 format</button>
  <button class="ghost" onclick="setupGuide()">Console setup&hellip;</button>
 </div>
 <div id="out"></div>
</fieldset>

<fieldset id="setupbox" style="display:none"><legend>grandMA3 OSC setup</legend>
 <div id="setupout"></div>
</fieldset>

<fieldset id="probebox" style="display:none"><legend>Find MA3 format</legend>
 <p class="note"><b>On the console first</b> (Menu &rarr; In &amp; Out &rarr;
 OSC). Every one of these fails silently, and they all look identical
 from here:</p>
 <ol class="note">
  <li><b>Enable Input</b> is on.</li>
  <li><b>Receive</b> is <b>Yes</b> on the OSC line &mdash; it is not on by
   default, and it is separate from Enable Input.</li>
  <li><b>Receive Command</b> is <b>Yes</b> too, if you want the master
   fader and the transport keys (they use <code>/cmd</code>).</li>
  <li><b>Port</b> matches the <i>send</i> port above &mdash; 8000 by
   default. One OSC line uses the same port to send and receive, so if
   you want feedback as well, add a second line on the listen port.</li>
  <li>Something is actually <b>assigned to the executor</b> you are
   watching. A perfectly delivered message to an empty executor does
   nothing at all.</li>
 </ol>
 <p class="note">Then: MA3 processes only OSC whose prefix matches the OSC
 line's, and the "Page"/"Fader" address cells are editable too, so a wrong
 setting looks exactly like a dead bridge. Watch your executor and press
 Sweep &mdash; each step goes out 1.5s apart. Whichever one moves the
 fader is your format; click Keep on that row.</p>
 <div class="row">
  <label>page</label><input type="number" id="pbpage" value="1" style="width:4rem">
  <label>exec</label><input type="number" id="pbexec" value="201" style="width:5rem">
  <button onclick="probeRun()">Sweep</button>
 </div>
 <div id="probeout"></div>
</fieldset>

<fieldset id="mapbox" style="display:none"><legend>Mapping</legend>
 <div id="map"></div>
</fieldset>

<p class="note">Config lives at <span id="cfgpath" class="note"></span>.
CLI: <code>xbridge run --target ma3</code> &middot; <code>xbridge test</code> &middot; <code>xbridge sniff</code></p>
</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function post(url, fd) {
  const r = await fetch(url, {method:'POST', body:fd});
  if (!r.ok) throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail);
  return r;
}
let timer = null, cfg = null;
async function refresh() {
  try {
    const d = await (await fetch('/api/status')).json();
    let line;
    if (d.running) {
      line = `<span class="ok"><b>${esc(d.state)}</b></span> ${esc(d.detail)}`
        + `<br>MIDI in: ${d.counters.midi_in||0} · sent to console: ${d.counters.sent||0}`
        + ` · console in: ${d.counters.osc_in||0}`;
      if (d.sent && d.sent.length)
        line += '<br>last sent: ' + d.sent.slice(-4).map(esc).join(' , ');
      if (d.midi && d.midi.length)
        line += '<br><b>surface says:</b><br>' +
          d.midi.slice(-6).map(m => '<code>' + esc(m) + '</code>').join('<br>');
    }
    else if (d.state === 'error') line = `<span class="err">${esc(d.detail)}</span>`;
    else if (d.available) line = 'stopped';
    else line = '<span class="err">MIDI support not installed - pip install mido python-rtmidi</span>';
    $('out').innerHTML = line;
    if (!d.running && timer) { clearInterval(timer); timer = null; }
  } catch (e) { $('out').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
async function start() {
  const fd = new FormData();
  fd.append('host', $('host').value); fd.append('send_port', $('send').value || '0');
  fd.append('recv_port', $('recv').value); fd.append('target', $('target').value);
  fd.append('surface', $('surface').value);
  fd.append('midi_port', $('midiport').value);
  try {
    await post('/api/start', fd);
    if (!timer) timer = setInterval(refresh, 2000);
    refresh();
  } catch (e) { $('out').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
async function stop() {
  try { await post('/api/stop', new FormData()); } catch (e) {}
  setTimeout(refresh, 400);
}
async function testSend() {
  const fd = new FormData(); fd.append('strip', '0'); fd.append('level', '0.5');
  try {
    const d = await (await post('/api/test-send', fd)).json();
    $('out').innerHTML = `sent <b>${esc(d.sent)}</b> to ${esc(d.to)}` +
      '<br>if the console did not move, the address or port is wrong - not the surface.';
  } catch (e) { $('out').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function setupGuide() {
  const box = $('setupbox');
  if (box.style.display === '') { box.style.display = 'none'; return; }
  box.style.display = '';
  const q = new URLSearchParams({
    host: $('host').value || '127.0.0.1',
    send_port: $('send').value || '8000',
    recv_port: $('recv').value || '9000',
    prefix: (cfg && cfg.prefix) || ''});
  try {
    const d = await (await fetch('/api/ma3-setup?' + q)).json();
    let h = '<p class="note">Menu &rarr; In &amp; Out &rarr; OSC. ' +
      'An MA3 OSC line has <b>one Port cell used for both directions</b> ' +
      '&mdash; there is no separate send and receive port, which is why ' +
      'a round trip takes two lines.</p><table class="setup">';
    h += '<tr><th colspan="2">Top of the menu</th></tr>';
    for (const t of d.toggles)
      h += `<tr><td>${esc(t.name)}</td><td><b>${esc(t.value)}</b>` +
           `<div class="note">${esc(t.note)}</div></td></tr>`;
    for (const ln of d.lines) {
      h += `<tr><th colspan="2">${esc(ln.title)}</th></tr>` +
           `<tr><td colspan="2" class="note">${esc(ln.why)}</td></tr>`;
      for (const c of ln.cells)
        h += `<tr><td>${esc(c.name)}</td><td><b>${esc(c.value)}</b>` +
             (c.note ? `<div class="note">${esc(c.note)}</div>` : '') +
             '</td></tr>';
    }
    h += '</table>';
    for (const w of d.warnings)
      h += `<p class="note"><b>Watch out:</b> ${esc(w)}</p>`;
    h += `<p class="note">${esc(d.feedback)}</p>`;
    $('setupout').innerHTML = h;
  } catch (e) { $('setupout').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

function probe() {
  const b = $('probebox');
  b.style.display = b.style.display === 'none' ? '' : 'none';
}
async function probeRun() {
  const fd = new FormData();
  fd.append('host', $('host').value || '127.0.0.1');
  fd.append('port', $('send').value || '8000');
  fd.append('page', $('pbpage').value || '1');
  fd.append('exec_no', $('pbexec').value || '201');
  $('probeout').innerHTML = 'sweeping - watch the executor&hellip;';
  try {
    const d = await (await post('/api/probe', fd)).json();
    $('probeout').innerHTML = `sent to ${esc(d.to)}, exec ${esc(d.exec)}:<br>` +
      d.steps.map(s => `<div class="row"><code>${esc(s.label)}</code> ` +
        `<span class="note">${esc(s.sent)}</span> ` +
        `<button class="ghost" onclick="probeKeep(${s.index})">Keep</button></div>`
      ).join('');
  } catch (e) { $('probeout').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
async function probeKeep(i) {
  const fd = new FormData(); fd.append('index', i);
  try {
    const d = await (await post('/api/probe/apply', fd)).json();
    $('probeout').innerHTML = `saved: prefix <b>${esc(d.prefix || '(none)')}</b>, ` +
      `values <b>${esc(d.ma3_value)}</b>, address <b>${esc(d.ma3_addr)}</b>. ` +
      'Restart the bridge to use it.';
    loadCfg();
  } catch (e) { $('probeout').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function showPorts() {
  try {
    const d = await (await fetch('/api/ports')).json();
    if (!d.available) { $('out').innerHTML = '<span class="err">MIDI unavailable' +
      (d.error ? ': ' + esc(d.error) : '') + '</span>'; return; }
    $('out').innerHTML = 'IN: ' + (d.inputs.map(esc).join(' | ') || '(none)') +
      '<br>OUT: ' + (d.outputs.map(esc).join(' | ') || '(none)');
  } catch (e) { $('out').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function loadCfg() {
  const d = await (await fetch('/api/config')).json();
  cfg = d.config; $('cfgpath').textContent = d.path;
  if (cfg.target) $('target').value = cfg.target;
  loadPorts();
}
async function loadPorts() {
  // Auto-detect matches on port name, which cannot work for an X32 over
  // DIN MIDI (it arrives under the interface's name) or over RTPMIDI
  // (under the session's). So the list is offered to pick from.
  try {
    const d = await (await fetch('/api/ports')).json();
    const sel = $('midiport'), keep = sel.value;
    sel.innerHTML = '<option value="">auto-detect</option>';
    for (const n of (d.inputs || [])) {
      const o = document.createElement('option');
      o.value = n; o.textContent = n; sel.appendChild(o);
    }
    if (keep) sel.value = keep;
  } catch (e) { /* the ports button reports this properly */ }
}
async function toggleMap() {
  const box = $('mapbox');
  if (box.style.display !== 'none') { box.style.display = 'none'; return; }
  if (!cfg) { try { await loadCfg(); } catch (e) { $('out').innerHTML = esc(e.message); return; } }
  box.style.display = '';
  renderMap();
}
const num = (id, val, w) => `<input type="number" id="${id}" value="${val}" style="width:${w||'4.2rem'}">`;
function renderMap() {
  if (!cfg || $('mapbox').style.display === 'none') return;
  const t = $('target').value;
  let h = '';
  if (t === 'ma3') {
    h += '<div class="gridwrap"><table><tr><th>Strip</th>' +
      [1,2,3,4,5,6,7,8].map(i => `<th>${i}</th>`).join('') + '</tr>';
    const row = (label, key) => '<tr><th>' + label + '</th>' +
      [0,1,2,3,4,5,6,7].map(i =>
        `<td><input type="number" id="m_${key}${i}" value="${cfg[key][i] ?? ''}"></td>`).join('') + '</tr>';
    h += row('Fader&rarr;exec', 'fader_execs');
    h += row('SELECT&rarr;key', 'select_execs');
    h += row('MUTE&rarr;key', 'mute_execs');
    h += row('Enc&rarr;exec', 'encoder_execs');
    h += '</table></div>';
    h += `<div class="row"><label>master exec (0 = grand master)</label>${num('m_master', cfg.master_exec)}
      <label>page</label>${num('m_page', cfg.page)}
      <label>prefix</label><input type="text" id="m_prefix" value="${esc(cfg.prefix)}" style="width:6rem">
      <label>enc step</label>${num('m_step', cfg.encoder_step, '4.5rem')}
      <label>fader value</label><select id="m_3v">
        <option value="int" ${(cfg.ma3_value||'int')==='int'?'selected':''}>int 0-100 (manual)</option>
        <option value="float" ${(cfg.ma3_value||'int')==='float'?'selected':''}>float</option>
      </select></div>`;
    h += `<div class="row"><label>PLAY</label><input type="text" id="m_play" value="${esc(cfg.cmd_play)}" style="width:8rem">
      <label>STOP</label><input type="text" id="m_stop" value="${esc(cfg.cmd_stop)}" style="width:8rem">
      <label>REW</label><input type="text" id="m_rew" value="${esc(cfg.cmd_rewind)}" style="width:8rem"></div>`;
  } else if (t === 'generic') {
    const g = (k, d) => cfg[k] !== undefined ? cfg[k] : d;
    h += `<p class="note"><code>{n}</code> = strip number 1-8 (+8 per page). Empty = unmapped.
      Buttons send 1 press / 0 release. Feedback on the fader address moves the motors.</p>
      <div class="row"><label>fader</label><input type="text" id="m_gf" value="${esc(g('gen_fader','/fader/{n}'))}" style="width:11rem">
      <label>master</label><input type="text" id="m_gm" value="${esc(g('gen_master','/master'))}" style="width:9rem"></div>
      <div class="row"><label>SELECT</label><input type="text" id="m_gs" value="${esc(g('gen_select','/button/{n}'))}" style="width:11rem">
      <label>MUTE</label><input type="text" id="m_gmu" value="${esc(g('gen_mute',''))}" style="width:11rem"></div>
      <div class="row"><label>encoder</label><input type="text" id="m_ge" value="${esc(g('gen_encoder',''))}" style="width:11rem">
      <label>value</label><select id="m_gsc">
        <option value="float01" ${g('gen_scale','float01')==='float01'?'selected':''}>float 0-1</option>
        <option value="int100" ${g('gen_scale','float01')==='int100'?'selected':''}>int 0-100</option>
      </select></div>`;
  } else if (t === 'ma2') {
    h += `<p class="note">grandMA2 via its <b>Web Remote websocket</b> (MA2 has
      no OSC - this is how ShowCockpit does it too). Faders ride
      <code>Executor page.1-8</code>, SELECT = Go, MUTE = Off, encoders ride
      executors 9-16, master = SpecialMaster 2.1; executor titles and levels
      come back from the web remote to drive the strips and motors
      (experimental - the feedback format is reverse-engineered). MA2 setup:
      enable Remotes (Setup &rarr; Console &rarr; Global Settings) and check
      the web remote works in a browser first. Port: 80.</p>
      <div class="row"><label>user</label><input type="text" id="m_2u" value="${esc(cfg.ma2_user)}" style="width:7rem">
      <label>password</label><input type="text" id="m_2p" value="${esc(cfg.ma2_password)}" style="width:7rem">
      <label>exec page</label>${num('m_page', cfg.page)}</div>
      <div class="row"><label>master command</label>
      <input type="text" id="m_2m" value="${esc(cfg.ma2_master_cmd)}" style="width:16rem"></div>`;
  } else if (t === 'magicq') {
    h += `<p class="note">Playbacks 1-8 with motor feedback; SELECT = Go, MUTE = true Flash,
      encoders = execute grid 1, STOP/PLAY = blackout on/off. MagicQ: Setup &rarr; View
      Settings &rarr; Network, receive 8000 / transmit 9000.</p>
      <div class="row"><label>master &rarr; playback (0 = off)</label>${num('m_mqpb', cfg.magicq_master_pb)}</div>`;
  } else if (t === 'eos') {
    h += `<p class="note">OSC fader bank 1 (created on connect), floats with motor feedback
      (Eos echoes ~3s late by design). SELECT = Fire, MUTE = Stop, PLAY/STOP = Go / Stop-Back.
      Eos: Setup &rarr; System &rarr; Show Control &rarr; OSC, UDP RX 8000, TX here :9000.</p>`;
  } else if (t === 'x32') {
    h += `<p class="note">Input channels banked 8 at a time, real mutes, channel select,
      pan on encoders, main stereo on the master, names on the strips. No console setup.</p>
      <div class="row"><label>start bank (1-4)</label>${num('m_page', cfg.page)}</div>`;
  } else if (t === 'resolume') {
    h += `<p class="note">Layer opacity on faders (banked), MUTE bypasses, SELECT connects the
      column, encoders ride layer masters. Enable OSC output in Resolume for motor feedback.
      (Resolume also does MIDI natively if you prefer.)</p>
      <div class="row"><label>start bank (1-4)</label>${num('m_page', cfg.page)}</div>`;
  } else {
    h += `<p class="note">Companion page buttons: SELECT = row 0, MUTE = row 1, transport =
      row 2 cols 0-4, all true down/up; faders write custom variables fader1-8 + master
      (0-100); encoders rotate row 3; FADER BANK changes the Companion page.</p>
      <div class="row"><label>start page</label>${num('m_page', cfg.page)}</div>`;
  }
  h += `<div class="row"><button onclick="saveMap()">Save mapping</button>
    <a href="/api/config/export"><button type="button" class="ghost">Export preset</button></a>
    <label>import:</label><input type="file" id="imp" accept=".json" onchange="importMap()">
    <span id="mapout" class="note"></span></div>`;
  $('map').innerHTML = h;
}
function collect(key, n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const el = $('m_' + key + i);
    if (el && el.value !== '') out.push(parseInt(el.value, 10));
  }
  return out;
}
async function saveMap() {
  const t = $('target').value;
  const body = {target: t};
  const pg = $('m_page'); if (pg) body.page = parseInt(pg.value, 10) || 1;
  const mq = $('m_mqpb'); if (mq) body.magicq_master_pb = parseInt(mq.value, 10) || 0;
  const u2 = $('m_2u');
  if (u2) { body.ma2_user = u2.value; body.ma2_password = $('m_2p').value;
            body.ma2_master_cmd = $('m_2m').value; }
  if (t === 'ma3') {
    body.fader_execs = collect('fader_execs', 8);
    body.select_execs = collect('select_execs', 8);
    body.mute_execs = collect('mute_execs', 8);
    body.encoder_execs = collect('encoder_execs', 8);
    body.master_exec = parseInt($('m_master').value, 10) || 0;
    body.prefix = $('m_prefix').value;
    body.encoder_step = parseFloat($('m_step').value) || 0.02;
    body.cmd_play = $('m_play').value; body.cmd_stop = $('m_stop').value;
    body.cmd_rewind = $('m_rew').value;
    if ($('m_3v')) body.ma3_value = $('m_3v').value;
  }
  if (t === 'generic') {
    body.gen_fader = $('m_gf').value; body.gen_master = $('m_gm').value;
    body.gen_select = $('m_gs').value; body.gen_mute = $('m_gmu').value;
    body.gen_encoder = $('m_ge').value; body.gen_scale = $('m_gsc').value;
  }
  try {
    const r = await fetch('/api/config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if (!r.ok) throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail);
    cfg = Object.assign(cfg || {}, body);
    $('mapout').textContent = 'saved - applies on next start';
  } catch (e) { $('mapout').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
async function importMap() {
  const f = $('imp').files[0];
  if (!f) return;
  const fd = new FormData(); fd.append('file', f);
  try {
    const d = await (await post('/api/config/import', fd)).json();
    await loadCfg();
    $('target').value = d.target;
    renderMap();
    $('mapout').textContent = 'preset imported (' + d.target + ')';
  } catch (e) { $('mapout').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
window.addEventListener('DOMContentLoaded', async () => {
  try { await loadCfg(); } catch (e) { /* defaults are fine */ }
  refresh();
});
</script></body></html>
"""
