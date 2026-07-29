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
    from .. import _build
    return PAGE.replace("__BUILD__", _build.label())


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
                "unparsed": note,
                "warnings": plan.warnings(fixture)}

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
            "channels": len((m or fixture.modes[0]).channels), "unparsed": "",
            "warnings": []}


@app.post("/api/head-check")
def api_head_check(plan_text: str = Form(...)) -> dict:
    """Parse a plan and lint it, without building anything."""
    from .. import plan

    try:
        fixture = plan.parse(plan_text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    m = fixture.modes[0]
    return {
        "label": f"{fixture.key} [{m.name}]",
        "channels": [
            {"n": c.offset, "name": c.name, "attribute": c.attribute,
             "fine": c.fine, "ranges": len(c.ranges)}
            for c in m.channels
        ],
        "warnings": plan.warnings(fixture),
    }


@app.post("/api/head-match")
def api_head_match(plan_text: str = Form(...)) -> dict:
    """Rank known fixtures against a plan: "what is this clone really?"."""
    from .. import library as libmod, matching, plan

    try:
        fixture = plan.parse(plan_text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        lib = libmod.load(None, include_ofl=True)
    except Exception:      # noqa: BLE001 - no libraries is a fine answer
        return {"hits": []}
    if not lib.fixtures:
        return {"hits": []}

    hits = matching.find_candidates(fixture, fixture.modes[0], lib.fixtures,
                                    limit=5)
    return {"hits": [
        {
            "label": m.label,
            "score": round(m.score, 3),
            "exact": m.exact,
            "source": libmod.label_for(m.fixture.source),
            # An OFL hit can be loaded straight back into the editor.
            "key": m.fixture.source_id
            if m.fixture.source == "ofl" else "",
        }
        for m in hits
    ]}


@app.post("/api/head-save")
def api_head_save(plan_text: str = Form(...)) -> dict:
    """Build a plan and keep it in the personal head library."""
    from .. import mylib, plan

    try:
        fixture = plan.parse(plan_text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    saved = mylib.save(fixture, plan_text=plan_text)
    return {"stem": saved.stem, "label": f"{saved.manufacturer} {saved.model} [{saved.mode}]",
            "path": str(saved.hed)}


@app.get("/api/my-heads")
def api_my_heads() -> dict:
    """List the saved custom heads."""
    from .. import mylib

    return {"dir": str(mylib.store_dir()), "heads": [
        {"stem": h.stem, "manufacturer": h.manufacturer, "model": h.model,
         "mode": h.mode, "channels": h.channels}
        for h in mylib.entries()
    ]}


@app.get("/api/my-heads/plan")
def api_my_head_plan(stem: str) -> dict:
    """Reopen a saved head's plan in the editor."""
    from .. import mylib

    text = mylib.get_plan(stem)
    if text is None:
        raise HTTPException(404, f"no saved head called {stem!r}")
    return {"plan": text}


@app.get("/api/my-heads/download")
def api_my_head_download(stem: str) -> FileResponse:
    """Download a saved head's .hed."""
    from .. import mylib

    hed = mylib.store_dir() / f"{stem}.hed"
    if not hed.is_file():
        raise HTTPException(404, f"no saved head called {stem!r}")
    return FileResponse(hed, filename=hed.name,
                        media_type="application/octet-stream")


@app.post("/api/my-heads/remove")
def api_my_head_remove(stem: str = Form(...)) -> dict:
    """Delete a saved head."""
    from .. import mylib

    return {"removed": mylib.remove(stem)}


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
 #chanlist{list-style:none;margin:.5rem 0;padding:0}
 #chanlist li{display:flex;align-items:center;gap:.4rem;padding:.35rem .5rem;
   margin:.2rem 0;border:1px solid #8884;border-radius:6px;background:#8881;cursor:grab}
 #chanlist li.drag{opacity:.4} #chanlist li.over{border-color:#2f6feb;border-style:dashed}
 #chanlist .grip{opacity:.5;cursor:grab} #chanlist .num{opacity:.5;min-width:1.6rem;text-align:right}
 #chanlist input.cn{flex:2;min-width:6rem} #chanlist input.ca{flex:1;min-width:5rem}
 #chanlist .rm{background:#8883;color:inherit;padding:.2rem .5rem}
 #chanlist .rng{opacity:.6;font-size:12px;min-width:3rem}
</style></head><body>
<h1>LX-Tool</h1>
<p class="sub">Match a fixture against your ChamSys library, and convert between GDTF, OFL and grandMA2.</p>
<p class="sub" style="font-size:12px">LX-Tool &middot; __BUILD__ &middot; <a href="https://github.com/Wntr2134/LX-Tool/releases/latest" style="color:inherit">check for a newer build</a></p>

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
 <p style="margin-top:1rem"><input type="text" id="pm" placeholder="Manufacturer" style="width:32%">
    <input type="text" id="pmod" placeholder="Model" style="width:32%">
    <input type="text" id="pmode" placeholder="Mode" style="width:22%"></p>
 <label>Channels - drag the grip to reorder, edit names, X to remove</label>
 <ul id="chanlist"></ul>
 <p><button onclick="addChan()">+ Add channel</button>
    <button onclick="headWhat()">What is this? (match)</button></p>
 <p><button onclick="headCheck(false)">Check plan</button>
    <button onclick="headBuild()">Build .hed &amp; download</button>
    <button onclick="headSave()">Save to my heads</button>
    <button type="button" onclick="toggleRaw()" style="background:#8883;color:inherit">Raw text</button></p>
 <textarea id="planbox" rows="12" style="width:100%;font-family:monospace;display:none"
    oninput="rawToRows()"></textarea>
 <div id="headout"></div>
</fieldset>

<fieldset><legend>6. My saved heads</legend>
 <p class="note">Custom heads you have saved - they also turn up in match and
    "What is this?" from now on. Reopen one to tweak, or download it again.</p>
 <p><button onclick="loadMyHeads()">Refresh list</button> <span id="mydir" class="note"></span></p>
 <div id="myheads"></div>
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
  loadMyHeads();
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

let rows = [];          // [{name, attr, ranges:[[lo,hi,name],...]}]
let rawMode = false;

function planText() {
  if (rawMode) return $('planbox').value;
  let out = 'manufacturer: ' + $('pm').value + '\\nmodel: ' + ($('pmod').value || 'MyFixture') +
            '\\nmode: ' + ($('pmode').value || 'Custom') + '\\n\\n';
  for (const r of rows) {
    let line = 'channel: ' + (r.name || 'Channel');
    if (r.attr) line += ' | attr=' + r.attr;
    out += line + '\\n';
    for (const rg of (r.ranges||[])) out += `  ${rg[0]}-${rg[1]}  ${rg[2]}\\n`;
  }
  return out;
}

function renderRows() {
  const ul = $('chanlist'); ul.innerHTML = '';
  rows.forEach((r, i) => {
    const li = document.createElement('li');
    li.draggable = true; li.dataset.i = i;
    li.innerHTML = `<span class="grip">⣿</span><span class="num">${i+1}</span>` +
      `<input class="cn" value="${escAttr(r.name)}" placeholder="name">` +
      `<input class="ca" value="${escAttr(r.attr||'')}" placeholder="attribute">` +
      (r.ranges&&r.ranges.length ? `<span class="rng">${r.ranges.length} rng</span>` : '') +
      `<button class="rm" title="remove">✕</button>`;
    li.querySelector('.cn').oninput = e => rows[i].name = e.target.value;
    li.querySelector('.ca').oninput = e => rows[i].attr = e.target.value;
    li.querySelector('.rm').onclick = () => { rows.splice(i,1); renderRows(); };
    li.addEventListener('dragstart', e => { li.classList.add('drag'); e.dataTransfer.setData('text', i); });
    li.addEventListener('dragend', () => li.classList.remove('drag'));
    li.addEventListener('dragover', e => { e.preventDefault(); li.classList.add('over'); });
    li.addEventListener('dragleave', () => li.classList.remove('over'));
    li.addEventListener('drop', e => {
      e.preventDefault(); li.classList.remove('over');
      const from = +e.dataTransfer.getData('text'), to = i;
      if (from === to) return;
      const [m] = rows.splice(from, 1); rows.splice(to, 0, m); renderRows();
    });
    ul.appendChild(li);
  });
  if (rawMode) $('planbox').value = planText();
}
const escAttr = s => String(s).replace(/"/g,'&quot;');

function planToRows(text) {
  const lines = text.split('\\n'); rows = []; let cur = null;
  for (const raw of lines) {
    const t = raw.trim();
    if (!t || t.startsWith('#')) continue;
    if (/^\\s/.test(raw) && cur) {
      const m = t.match(/^(\\d{1,3})\\s*-\\s*(\\d{1,3})\\s+(.+)$/);
      if (m) { (cur.ranges = cur.ranges||[]).push([+m[1],+m[2],m[3]]); continue; }
    }
    const [k, ...rest] = t.split(':'); const v = rest.join(':').trim();
    if (k.toLowerCase()==='manufacturer') $('pm').value = v;
    else if (k.toLowerCase()==='model') $('pmod').value = v;
    else if (k.toLowerCase()==='mode') $('pmode').value = v;
    else if (k.toLowerCase()==='channel') {
      const parts = v.split('|').map(x=>x.trim());
      cur = {name: parts[0], attr: '', ranges: []};
      for (const p of parts.slice(1)) if (p.toLowerCase().startsWith('attr=')) cur.attr = p.slice(5).trim();
      rows.push(cur);
    }
  }
  renderRows();
}
function rawToRows() { if (rawMode) planToRows($('planbox').value); }
function toggleRaw() {
  rawMode = !rawMode;
  if (rawMode) $('planbox').value = planText();
  else planToRows($('planbox').value);
  $('planbox').style.display = rawMode ? 'block' : 'none';
  $('chanlist').style.display = rawMode ? 'none' : 'block';
}
function addChan() { rows.push({name:'New channel', attr:'', ranges:[]}); renderRows(); }

async function headWhat() {
  const fd = new FormData(); fd.append('plan_text', planText());
  try {
    const d = await (await post('/api/head-match', fd)).json();
    if (!d.hits.length) { $('headout').innerHTML = '<p class="note">No close matches (or no library loaded).</p>'; return; }
    $('headout').innerHTML = '<p><b>This layout looks most like:</b></p><table><tr><th>Match</th><th>Fixture</th><th></th></tr>' +
      d.hits.map(h => `<tr><td>${h.exact?'EXACT':Math.round(h.score*100)+'%'}</td>
        <td>${esc(h.label)} <span class="note">${esc(h.source)}</span></td>
        <td>${h.key?`<button onclick="loadKey('${esc(h.key)}')">Start from this</button>`:''}</td></tr>`).join('') +
      '</table>';
  } catch (e) { $('headout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}
async function loadKey(key) { $('refkey').value = key; await headPlan(); }

async function headPlan() {
  const fd = new FormData();
  fd.append('key', $('refkey').value); fd.append('mode', $('refmode').value);
  try {
    const d = await (await post('/api/head-plan', fd)).json();
    planToRows(d.plan);
    $('headout').innerHTML = `<p>${d.channels} channel(s) loaded - drag to reorder, edit names.</p>`;
  } catch (e) { $('headout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function headChart() {
  const fd = new FormData(); fd.append('chart_text', $('chartbox').value);
  try {
    const d = await (await post('/api/head-plan', fd)).json();
    planToRows(d.plan);
    let msg = `<p>${d.channels} channel(s) recognised - <b>check against the manual</b>, drag to reorder.</p>`;
    if (d.unparsed) msg += `<p class="sev5">Could not read: ${esc(d.unparsed)}</p>`;
    $('headout').innerHTML = msg;
  } catch (e) { $('headout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function headSave() {
  const fd = new FormData(); fd.append('plan_text', planText());
  try {
    const d = await (await post('/api/head-save', fd)).json();
    $('headout').innerHTML = `<p class="exact">Saved ${esc(d.label)} to your heads.</p>`;
    loadMyHeads();
  } catch (e) { $('headout').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function loadMyHeads() {
  try {
    const d = await (await fetch('/api/my-heads')).json();
    $('mydir').textContent = d.dir;
    if (!d.heads.length) { $('myheads').innerHTML = '<p class="note">Nothing saved yet.</p>'; return; }
    $('myheads').innerHTML = '<table><tr><th>Fixture</th><th>Mode</th><th>Ch</th><th></th></tr>' +
      d.heads.map(h => `<tr>
        <td>${esc(h.manufacturer)} ${esc(h.model)}</td><td>${esc(h.mode)}</td><td>${h.channels}</td>
        <td><button onclick="openMyHead('${esc(h.stem)}')">Edit</button>
            <a href="/api/my-heads/download?stem=${encodeURIComponent(h.stem)}"><button type="button">Download</button></a>
            <button onclick="removeMyHead('${esc(h.stem)}')" style="background:#8883;color:inherit">Remove</button></td>
      </tr>`).join('') + '</table>';
  } catch (e) { $('myheads').innerHTML = `<p class="sev5">${esc(e.message)}</p>`; }
}

async function openMyHead(stem) {
  const d = await (await fetch('/api/my-heads/plan?stem=' + encodeURIComponent(stem))).json();
  planToRows(d.plan);
  $('headout').innerHTML = `<p>Loaded ${esc(stem)} into the editor above.</p>`;
  document.getElementById('planbox').scrollIntoView({behavior:'smooth'});
}

async function removeMyHead(stem) {
  if (!confirm('Remove ' + stem + '?')) return;
  const fd = new FormData(); fd.append('stem', stem);
  await post('/api/my-heads/remove', fd);
  loadMyHeads();
}

async function headSave2Placeholder() {}

async function headCheck(quiet) {
  const fd = new FormData(); fd.append('plan_text', planText());
  const d = await (await post('/api/head-check', fd)).json();
  let h = `<p><b>${esc(d.label)}</b> - ${d.channels.length} channel(s)</p>`;
  h += '<table><tr><th>#</th><th>Name</th><th>Attribute</th><th>Ranges</th></tr>' +
    d.channels.map(c => `<tr><td>${c.n}</td><td>${esc(c.name)}</td>
      <td>${esc(c.attribute)}${c.fine ? ' (fine)' : ''}</td><td>${c.ranges || ''}</td></tr>`).join('') +
    '</table>';
  h += d.warnings.map(w => `<p class="sev5">warning: ${esc(w)}</p>`).join('');
  if (!quiet || d.warnings.length) $('headout').innerHTML = h;
  return d.warnings.length;
}

async function headBuild() {
  const fd = new FormData(); fd.append('plan_text', planText());
  try {
    try { await headCheck(true); } catch (e) { /* build reports its own error */ }
    const r = await post('/api/head-build', fd);
    const blob = await r.blob();
    const name = (r.headers.get('content-disposition')||'').match(/filename="?([^"]+)"?/)?.[1] || 'head.hed';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    $('headout').innerHTML += `<p class="exact">Downloaded ${esc(name)} - copy it into the MagicQ heads folder and restart MagicQ.</p>`;
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
