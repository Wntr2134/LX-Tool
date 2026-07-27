"""GDTF Share client.

GDTF Share (https://gdtf-share.com) is the largest fixture library going and
the one new fixtures land on first. Its API needs a free account.

**Your credentials are never stored.** Logging in exchanges them for a
session cookie, and only that cookie is written to disk, in the LX-Tool cache
directory with owner-only permissions. When it expires you log in again.
Nothing is transmitted anywhere except gdtf-share.com.

The API::

    POST /apis/public/login.php        user=...&password=...  -> sets session
    GET  /apis/public/getList.php                             -> fixture list
    GET  /apis/public/downloadFile.php?rid=N                  -> one .gdtf

All three answer JSON of the form ``{"result": bool, "error": str}`` on
failure, which is what the error handling below keys off.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .catalog import cache_dir

BASE = "https://gdtf-share.com/apis/public"
LOGIN_URL = f"{BASE}/login.php"
LIST_URL = f"{BASE}/getList.php"
DOWNLOAD_URL = f"{BASE}/downloadFile.php"

_USER_AGENT = "LX-Tool/1.0 (+https://github.com/Wntr2134/LX-Tool)"


_CERT_ADVICE = (
    "HTTPS certificate verification failed - Python has no CA certificates.\n"
    "Fix with:  pip install certifi"
)


class GdtfShareError(RuntimeError):
    """A GDTF Share request failed."""


@dataclass
class ShareEntry:
    """One fixture revision listed on GDTF Share."""

    rid: int
    manufacturer: str
    fixture: str
    revision: str = ""
    creator: str = ""
    rating: str = ""

    @property
    def filename(self) -> str:
        def clean(s: str) -> str:
            return "".join(c if c.isalnum() or c in "-_ ." else "_" for c in s).strip()

        stem = f"{clean(self.manufacturer)}@{clean(self.fixture)}"
        if self.revision:
            stem += f"@{clean(self.revision)}"
        return f"{stem}.gdtf"

    @property
    def label(self) -> str:
        return f"{self.manufacturer} {self.fixture}".strip()


def _session_path(cache: Path | str | None = None) -> Path:
    return Path(cache or cache_dir()) / "gdtf-share-session.txt"


def _opener(cache: Path | str | None = None) -> urllib.request.OpenerDirector:
    """An opener carrying the saved session cookie, if there is one."""
    jar = http.cookiejar.MozillaCookieJar(str(_session_path(cache)))
    path = _session_path(cache)
    if path.exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError):
            pass
    from .net import build_opener

    opener = build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.cookiejar = jar          # type: ignore[attr-defined]
    return opener


def _request(opener, url: str, data: bytes | None = None, timeout: int = 60) -> bytes:
    try:
        with opener.open(url, data, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode("utf-8", "replace")
        try:
            message = json.loads(body).get("error") or body
        except (json.JSONDecodeError, AttributeError):
            message = body
        if exc.code == 401:
            raise GdtfShareError(
                f"GDTF Share says: {message}\nRun 'lx gdtf login' first "
                "(the session may simply have expired)."
            ) from exc
        raise GdtfShareError(f"GDTF Share returned {exc.code}: {message}") from exc
    except urllib.error.URLError as exc:
        from .net import is_certificate_error

        if is_certificate_error(exc):
            raise GdtfShareError(_CERT_ADVICE) from exc
        raise GdtfShareError(f"could not reach GDTF Share: {exc.reason}") from exc


def login(user: str, password: str, *, cache: Path | str | None = None) -> None:
    """Exchange credentials for a session cookie.

    The credentials are used for this one request and never written down.
    """
    opener = _opener(cache)
    payload = urllib.parse.urlencode({"user": user, "password": password}).encode()
    raw = _request(opener, LOGIN_URL, payload)

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GdtfShareError("GDTF Share returned something that isn't JSON") from exc

    if not doc.get("result"):
        raise GdtfShareError(doc.get("error") or "login refused")

    path = _session_path(cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    jar = opener.cookiejar                       # type: ignore[attr-defined]
    jar.save(ignore_discard=True, ignore_expires=True)
    try:
        os.chmod(path, 0o600)                    # the cookie is a credential
    except OSError:
        pass


def logged_in(cache: Path | str | None = None) -> bool:
    return _session_path(cache).exists()


def logout(cache: Path | str | None = None) -> bool:
    path = _session_path(cache)
    if path.exists():
        path.unlink()
        return True
    return False


def fetch_list(*, cache: Path | str | None = None) -> list[ShareEntry]:
    """Every fixture revision available to this account."""
    raw = _request(_opener(cache), LIST_URL)
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GdtfShareError("fixture list was not JSON") from exc

    if isinstance(doc, dict):
        if doc.get("result") is False:
            raise GdtfShareError(doc.get("error") or "listing refused")
        rows = doc.get("list") or doc.get("data") or []
    else:
        rows = doc

    entries: list[ShareEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = row.get("rid") or row.get("id")
        if rid is None:
            continue
        entries.append(ShareEntry(
            rid=int(rid),
            manufacturer=str(row.get("manufacturer") or "").strip(),
            fixture=str(row.get("fixture") or row.get("name") or "").strip(),
            revision=str(row.get("revision") or "").strip(),
            creator=str(row.get("creator") or "").strip(),
            rating=str(row.get("rating") or "").strip(),
        ))
    return entries


def download(entry: ShareEntry, dest_dir: Path | str, *,
             cache: Path | str | None = None, overwrite: bool = False) -> Path:
    """Download one fixture. Returns the file written."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / entry.filename

    if out.exists() and not overwrite:
        return out

    url = f"{DOWNLOAD_URL}?{urllib.parse.urlencode({'rid': entry.rid})}"
    blob = _request(_opener(cache), url)
    if not blob.startswith(b"PK"):
        # An error comes back as JSON with a 200 in some cases.
        snippet = blob[:200].decode("utf-8", "replace")
        raise GdtfShareError(f"{entry.label}: not a GDTF archive - {snippet}")

    tmp = out.with_suffix(".part")
    tmp.write_bytes(blob)
    tmp.replace(out)
    return out


def search(entries: list[ShareEntry], query: str) -> list[ShareEntry]:
    """Filter a listing by a free-text query over manufacturer and model."""
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return entries
    out = []
    for e in entries:
        hay = f"{e.manufacturer} {e.fixture} {e.revision}".lower()
        if all(t in hay for t in terms):
            out.append(e)
    return out
