"""Local web UI for LX-Tool.

Run with::

    pip install -r requirements.txt
    uvicorn lxtool.web.app:app --reload

Then open http://127.0.0.1:8000.  It is intended to run on the tech's own
laptop against a local MagicQ heads folder, so there is no auth and it binds
to localhost by default.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from .. import catalog, matching
from ..cli import load_fixture
from ..formats import chamsys, gdtf, ma2

app = FastAPI(title="LX-Tool", description="Fixture library matching and conversion")

_SUPPORTED = {".gdtf", ".json", ".xml"}


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Entry point for the ``lx-web`` command.

    Binds to localhost by default: the UI takes a filesystem path and has no
    auth, so it is meant for the tech's own machine, not a shared network.
    """
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="lx-web", description="LX-Tool web UI")
    parser.add_argument("--host", default=host)
    parser.add_argument("--port", type=int, default=port)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open a browser window automatically")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print("=" * 58)
    print(f"  LX-Tool is running at  {url}")
    print("  Leave this window open. Press Ctrl+C to stop.")
    print("=" * 58)

    if not args.no_browser and not args.reload:
        # Open the browser once the server is actually accepting connections,
        # so the first request doesn't land on a closed port.
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        "lxtool.web.app:app" if args.reload else app,
        host=args.host, port=args.port, reload=args.reload,
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.post("/api/scan")
def api_scan(folder: str = Form(...)) -> dict:
    """Index a ChamSys heads folder."""
    try:
        lib = chamsys.ChamSysLibrary.scan(folder)
    except (NotADirectoryError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc

    fixtures = lib.as_fixtures()
    return {
        "heads": len(lib.heads),
        "fixtures": len(fixtures),
        "modes": sum(len(f.modes) for f in fixtures),
        "aliases": len(lib.aliases),
        "sample": [
            {
                "manufacturer": f.manufacturer,
                "model": f.model,
                "modes": [
                    {"name": m.name, "channels": m.channel_count}
                    for m in f.modes
                ],
            }
            for f in sorted(fixtures, key=lambda f: f.key.lower())[:50]
        ],
    }


@app.get("/api/heads-folders")
def api_heads_folders() -> dict:
    """MagicQ heads folders found on this machine, so nobody has to hunt."""
    found = chamsys.find_heads_folders()
    return {
        "folders": [
            {"path": str(p), "has_library": (p / "heads.all").is_file()}
            for p in found
        ]
    }


@app.post("/api/fetch-catalog")
def api_fetch_catalog() -> dict:
    """Download the Open Fixture Library for offline use."""
    from ..net import CertificateError

    try:
        path = catalog.Catalog.download()
    except CertificateError as exc:
        # Worth its own branch: the fix is a pip install, not a retry.
        raise HTTPException(502, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(502, f"could not download catalogue: {exc}") from exc
    cat = catalog.Catalog.load()
    return {"fixtures": len(cat), "manufacturers": len(cat.manufacturers()),
            "path": str(path)}


@app.get("/api/search")
def api_search(q: str, limit: int = 20) -> dict:
    """Search the cached catalogue."""
    try:
        cat = catalog.Catalog.load()
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc

    return {
        "count": len(cat),
        "age_days": round(cat.age_days, 1) if cat.age_days is not None else None,
        "hits": [
            {
                "key": e.key,
                "label": e.label,
                "categories": list(e.categories),
                "modes": [{"name": n, "channels": c} for n, c in e.modes],
            }
            for e in cat.search(q, limit=limit)
        ],
    }


@app.post("/api/match-catalog")
def api_match_catalog(folder: str = Form(...), key: str = Form(...)) -> dict:
    """Match a catalogue fixture against a ChamSys library, no upload needed."""
    try:
        cat = catalog.Catalog.load()
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc

    entry = cat.get(key)
    if entry is None:
        raise HTTPException(404, f"{key} is not in the catalogue")

    try:
        library = chamsys.ChamSysLibrary.scan(folder).as_fixtures()
    except (NotADirectoryError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc

    return _match_payload(entry.to_fixture(), library)


@app.post("/api/match")
async def api_match(folder: str = Form(...), file: UploadFile = File(...)) -> dict:
    """Match an uploaded fixture against a ChamSys library."""
    fixture, _ = await _load_upload(file)
    if not fixture.modes:
        raise HTTPException(400, "no DMX modes found in that file")

    try:
        library = chamsys.ChamSysLibrary.scan(folder).as_fixtures()
    except (NotADirectoryError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc

    return _match_payload(fixture, library)


def _match_payload(fixture, library) -> dict:
    """Shared response shape for both the upload and catalogue match routes."""
    results = []
    for mode in fixture.modes:
        matches = matching.find_candidates(fixture, mode, library, limit=8)
        results.append({
            "mode": mode.name,
            "channels": mode.channel_count,
            "matches": [
                {
                    "label": m.label,
                    "score": m.score,
                    "exact": m.exact,
                    "reasons": m.reasons,
                    "edits": [
                        {
                            "action": e.action,
                            "offset": e.offset,
                            "attribute": e.attribute,
                            "detail": e.detail,
                            "severity": e.severity,
                        }
                        for e in m.edits
                    ],
                }
                for m in matches
            ],
        })

    return {
        "fixture": {"manufacturer": fixture.manufacturer, "model": fixture.model,
                    "source": fixture.source},
        "results": results,
    }


@app.post("/api/convert")
async def api_convert(target: str = Form(...), file: UploadFile = File(...)) -> FileResponse:
    """Convert an uploaded fixture and return the converted file."""
    if target not in {"gdtf", "ma2"}:
        raise HTTPException(400, "target must be 'gdtf' or 'ma2'")

    fixture, _ = await _load_upload(file)
    out_dir = Path(tempfile.mkdtemp(prefix="lxtool-"))
    stem = (fixture.model or "fixture").replace("/", "-")

    if target == "gdtf":
        out = gdtf.write(fixture, out_dir / f"{stem}.gdtf")
        media = "application/zip"
    else:
        out = ma2.write(fixture, out_dir / f"{stem}.xml")
        media = "application/xml"

    return FileResponse(out, filename=out.name, media_type=media)


@app.post("/api/head-plan")
def api_head_plan(key: str = Form(""), mode: str = Form(""),
                  chart_text: str = Form("")) -> dict:
    """An editable head plan, from a catalogue fixture or a pasted DMX chart."""
    from .. import chart as chart_mod, plan

    if chart_text.strip():
        try:
            fixture = chart_mod.parse_chart(chart_text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        note = fixture.source_id
        return {"plan": plan.dump(fixture),
                "channels": len(fixture.modes[0].channels),
                "unparsed": note}

    if not key.strip():
        raise HTTPException(400, "give a catalogue key or paste a DMX chart")
    try:
        cat = catalog.Catalog.load()
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    entry = cat.get(key.strip())
    if entry is None:
        hits = cat.search_scored(key.strip(), limit=1)
        if not hits:
            raise HTTPException(404, f"{key!r} is not in the catalogue")
        entry = hits[0][1]
    fixture = entry.to_fixture()
    m = fixture.mode(mode) if mode.strip() else None
    if mode.strip() and m is None:
        names = ", ".join(x.name for x in fixture.modes)
        raise HTTPException(404, f"no mode {mode!r}. Available: {names}")
    return {"plan": plan.dump(fixture, m),
            "channels": len((m or fixture.modes[0]).channels), "unparsed": ""}


@app.post("/api/head-build")
def api_head_build(plan_text: str = Form(...)) -> FileResponse:
    """Compile an edited plan into a MagicQ .hed."""
    import re as _re

    from .. import plan

    try:
        fixture = plan.parse(plan_text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    m = fixture.modes[0]
    stem = f"{fixture.manufacturer}_{fixture.model}_{m.name}".strip("_")
    stem = _re.sub(r'[^A-Za-z0-9 ._+-]', "", stem) or "custom_head"
    out_dir = Path(tempfile.mkdtemp(prefix="lxtool-"))
    out = chamsys.write(fixture, out_dir / f"{stem}.hed", m)
    return FileResponse(out, filename=out.name,
                        media_type="application/octet-stream")


async def _load_upload(file: UploadFile):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _SUPPORTED:
        raise HTTPException(
            400,
            f"unsupported file type {suffix or '(none)'}. "
            f"Supported: {', '.join(sorted(_SUPPORTED))}. "
            "ChamSys .hed bodies are obfuscated and cannot be read.",
        )
    tmp_dir = Path(tempfile.mkdtemp(prefix="lxtool-in-"))
    path = tmp_dir / (file.filename or f"upload{suffix}")
    path.write_bytes(await file.read())
    try:
        return load_fixture(path), path
    except SystemExit as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, f"could not parse {path.name}: {exc}") from exc


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>LX-Tool</title>
<style>
 :root{color-scheme:light dark}
 body{font:15px/1.5 system-ui,sans-serif;max-width:940px;margin:2rem auto;padding:0 1rem}
 h1{margin-bottom:.2rem} .sub{opacity:.7;margin-top:0}
 fieldset{border:1px solid #8884;border-radius:8px;margin:1.2rem 0;padding:1rem}
 legend{font-weight:600;padding:0 .4rem}
 label{display:block;margin:.5rem 0 .2rem;font-weight:500}
 input,select,button{font:inherit;padding:.45rem .6rem;border-radius:6px;border:1px solid #8886;background:transparent;color:inherit}
 input[type=text]{width:100%;box-sizing:border-box}
 button{cursor:pointer;font-weight:600;background:#2f6feb;color:#fff;border:0}
 button:hover{background:#2559c4}
 pre{background:#8881;padding:.8rem;border-radius:6px;overflow-x:auto;font-size:13px}
 .sev5,.sev4{color:#d33} .sev3{color:#c80} .sev2,.sev1{opacity:.75}
 .exact{color:#1a7f37;font-weight:600}
 table{border-collapse:collapse;width:100%;margin-top:.5rem}
 td,th{text-align:left;padding:.3rem .5rem;border-bottom:1px solid #8883;vertical-align:top}
 .note{font-size:13px;opacity:.75}
</style></head><body>
<h1>LX-Tool</h1>
<p class="sub">Match a fixture against your ChamSys library, and convert between GDTF, OFL and grandMA2.</p>

<fieldset><legend>1. Your ChamSys heads folder</legend>
 <label for="folder">Path on this machine</label>
 <input type="text" id="folder" placeholder="/Users/you/Documents/MagicQ/heads">
 <p class="note">Windows: <code>C:\\ProgramData\\MagicQ\\heads</code> &middot;
    macOS: <code>~/Documents/MagicQ/heads</code></p>
 <button onclick="scan()">Scan library</button>
 <div id="scanout"></div>
</fieldset>

<fieldset><legend>2. Find a fixture online</legend>
 <p class="note">Searches a local copy of the Open Fixture Library, so it works
    with no signal. Download it once, then it's instant.</p>
 <p><button onclick="fetchCat()">Download / refresh catalogue</button>
    <span id="catstatus"></span></p>
 <label for="q">Search by name</label>
 <input type="text" id="q" placeholder="mac 700" onkeydown="if(event.key==='Enter')search()">
 <p><button onclick="search()">Search</button></p>
 <div id="searchout"></div>
</fieldset>

<fieldset><legend>3. Match a fixture file</legend>
 <label for="mfile">GDTF, OFL JSON or grandMA2 XML</label>
 <input type="file" id="mfile" accept=".gdtf,.json,.xml">
 <p><button onclick="match()">Find closest head</button></p>
 <div id="matchout"></div>
</fieldset>

<fieldset><legend>4. Convert</legend>
 <label for="cfile">Source file</label>
 <input type="file" id="cfile" accept=".gdtf,.json,.xml">
 <label for="target">Convert to</label>
 <select id="target">
   <option value="gdtf">GDTF (imports into MagicQ and MA3)</option>
   <option value="ma2">grandMA2 XML</option>
 </select>
 <p><button onclick="convert()">Convert &amp; download</button></p>
 <div id="convout"></div>
</fieldset>

<fieldset><legend>5. Make your own head (clones &amp; manuals)</legend>
 <p class="note">For the venue clone: start from the genuine profile, reorder
    the channels to match what the fixture actually does, rename it, build.
    Or paste the DMX chart out of a manual - photograph it and use your
    phone/Mac's select-text-in-image to copy the table.</p>
 <label>Start from a catalogue fixture</label>
 <p><input type="text" id="refkey" placeholder="martin/mac-aura" style="width:40%">
    <input type="text" id="refmode" placeholder="mode (optional)" style="width:20%">
    <button onclick="headPlan()">Load into editor</button></p>
 <label for="chartbox">&hellip;or paste a DMX chart from a manual</label>
 <textarea id="chartbox" rows="4" style="width:100%" placeholder="1  Pan&#10;2  Pan fine&#10;3  Dimmer&#10;0-9  Open"></textarea>
 <p><button onclick="headChart()">Read chart into editor</button></p>
 <label for="planbox">The plan - edit names, reorder channel lines, set manufacturer</label>
 <textarea id="planbox" rows="14" style="width:100%;font-family:monospace"></textarea>
 <p><button onclick="headBuild()">Build .hed &amp; download</button></p>
 <div id="headout"></div>
</fieldset>

<script>
const $ = id => document.getElementById(id);

// Offer whatever MagicQ folders exist on this machine.
window.addEventListener('DOMContentLoaded', async () => {
  try {
    const d = await (await fetch('/api/heads-folders')).json();
    if (!d.folders.length) return;
    $('folder').value = d.folders[0].path;
    const lib = d.folders[0].has_library ? ' (full library found)' : ' (custom heads only)';
    $('detected').innerHTML = 'Found: <code>' + d.folders[0].path + '</code>' + lib;
  } catch (e) { /* detection is a convenience, not a requirement */ }
});
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function post(url, fd) {
  const r = await fetch(url, {method:'POST', body:fd});
  if (!r.ok) throw new Error((await r.json().catch(()=>({detail:r.statusText}))).detail);
  return r;
}

async function scan() {
  const fd = new FormData(); fd.append('folder', $('folder').value);
  try {
    const d = await (await post('/api/scan', fd)).json();
    $('scanout').innerHTML = `<p><b>${d.heads}</b> head files &rarr;
      <b>${d.fixtures}</b> fixtures, <b>${d.modes}</b> modes,
      ${d.aliases} manufacturer aliases.</p>` +
      '<table><tr><th>Manufacturer</th><th>Model</th><th>Modes</th></tr>' +
      d.sample.map(f => `<tr><td>${esc(f.manufacturer)}</td><td>${esc(f.model)}</td>
        <td>${f.modes.map(m => esc(m.name) + (m.channels ? ` (${m.channels}ch)` : '')).join(', ')}</td></tr>`).join('') +
      '</table>';
  } catch (e) { $('scanout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function fetchCat() {
  $('catstatus').textContent = ' downloading...';
  try {
    const d = await (await post('/api/fetch-catalog', new FormData())).json();
    $('catstatus').innerHTML = ` <span class="exact">${d.fixtures} fixtures from ${d.manufacturers} manufacturers</span>`;
  } catch (e) { $('catstatus').innerHTML = ` <span class="sev5">${esc(e.message)}</span>`; }
}

async function search() {
  const q = $('q').value.trim();
  if (!q) return;
  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q));
    if (!r.ok) throw new Error((await r.json()).detail);
    const d = await r.json();
    if (!d.hits.length) { $('searchout').innerHTML = `<p>Nothing matching in ${d.count} fixtures.</p>`; return; }
    $('searchout').innerHTML =
      '<table><tr><th>Fixture</th><th>Modes</th><th></th></tr>' +
      d.hits.map(h => `<tr><td>${esc(h.label)}<br><span class="note">${esc(h.key)}</span></td>
        <td class="note">${h.modes.map(m => esc(m.name)+' ('+m.channels+'ch)').join('<br>')}</td>
        <td><button onclick="matchKey('${esc(h.key)}')">Match</button></td></tr>`).join('') +
      '</table>';
  } catch (e) { $('searchout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function matchKey(key) {
  const fd = new FormData(); fd.append('folder', $('folder').value); fd.append('key', key);
  try {
    render(await (await post('/api/match-catalog', fd)).json(), 'searchout');
  } catch (e) { $('searchout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

function render(d, into) {
  let h = `<p>Matching <b>${esc(d.fixture.manufacturer)} ${esc(d.fixture.model)}</b>
           (${esc(d.fixture.source)})</p>`;
  for (const r of d.results) {
    h += `<h3>Mode: ${esc(r.mode)} &mdash; ${r.channels} ch</h3>`;
    if (!r.matches.length) { h += '<p>No candidates.</p>'; continue; }
    h += '<table><tr><th>Head</th><th>Score</th><th>Why</th></tr>' +
      r.matches.map(m => `<tr><td>${esc(m.label)}</td>
        <td>${m.exact ? '<span class="exact">EXACT</span>' : Math.round(m.score*100)+'%'}</td>
        <td class="note">${m.reasons.map(esc).join('<br>')}</td></tr>`).join('') + '</table>';
    const best = r.matches[0];
    if (best.edits.length) {
      h += `<p><b>To use &ldquo;${esc(best.label)}&rdquo;, change (most critical first):</b></p><pre>` +
        best.edits.map(e =>
          `<span class="sev${e.severity}">ch ${String(e.offset).padStart(3)}  ` +
          `${e.action.padEnd(10)} ${esc(e.attribute).padEnd(14)} ${esc(e.detail)}</span>`
        ).join('\\n') + '</pre>';
    }
  }
  $(into).innerHTML = h;
}

async function match() {
  const f = $('mfile').files[0];
  if (!f) { $('matchout').innerHTML = '<p class="sev5">Choose a file first.</p>'; return; }
  const fd = new FormData(); fd.append('folder', $('folder').value); fd.append('file', f);
  try {
    render(await (await post('/api/match', fd)).json(), 'matchout');
  } catch (e) { $('matchout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function headPlan() {
  const fd = new FormData();
  fd.append('key', $('refkey').value); fd.append('mode', $('refmode').value);
  try {
    const d = await (await post('/api/head-plan', fd)).json();
    $('planbox').value = d.plan;
    $('headout').innerHTML = `<p>${d.channels} channel(s) loaded - edit away.</p>`;
  } catch (e) { $('headout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function headChart() {
  const fd = new FormData(); fd.append('chart_text', $('chartbox').value);
  try {
    const d = await (await post('/api/head-plan', fd)).json();
    $('planbox').value = d.plan;
    let msg = `<p>${d.channels} channel(s) recognised - <b>check against the manual</b>.</p>`;
    if (d.unparsed) msg += `<p class="sev5">Could not read: ${esc(d.unparsed)}</p>`;
    $('headout').innerHTML = msg;
  } catch (e) { $('headout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function headBuild() {
  const fd = new FormData(); fd.append('plan_text', $('planbox').value);
  try {
    const r = await post('/api/head-build', fd);
    const blob = await r.blob();
    const name = (r.headers.get('content-disposition')||'').match(/filename="?([^"]+)"?/)?.[1] || 'head.hed';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    $('headout').innerHTML = `<p class="exact">Downloaded ${esc(name)} - copy it into the MagicQ heads folder and restart MagicQ.</p>`;
  } catch (e) { $('headout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function convert() {
  const f = $('cfile').files[0];
  if (!f) { $('convout').innerHTML = '<p class="sev5">Choose a file first.</p>'; return; }
  const fd = new FormData(); fd.append('target', $('target').value); fd.append('file', f);
  try {
    const r = await post('/api/convert', fd);
    const blob = await r.blob();
    const name = (r.headers.get('content-disposition')||'').match(/filename="?([^"]+)"?/)?.[1] || 'converted';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    $('convout').innerHTML = `<p class="exact">Downloaded ${esc(name)}</p>`;
  } catch (e) { $('convout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}
</script>
</body></html>
"""
