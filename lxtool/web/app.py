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
                    {"name": m.name, "channels": m.__dict__.get("_declared_count")}
                    for m in f.modes
                ],
            }
            for f in sorted(fixtures, key=lambda f: f.key.lower())[:50]
        ],
    }


@app.post("/api/fetch-catalog")
def api_fetch_catalog() -> dict:
    """Download the Open Fixture Library for offline use."""
    try:
        path = catalog.Catalog.download()
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

<script>
const $ = id => document.getElementById(id);
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
