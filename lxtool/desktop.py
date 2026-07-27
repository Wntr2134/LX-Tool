"""Desktop app entry point.

Packaged with PyInstaller into a Mac ``.app`` or a Windows ``.exe``, this is
what someone double-clicks.  It runs the same local server the web UI uses -
the tool needs to read a MagicQ heads folder on *this* machine, so a hosted
version could never do the job - and shows it in a window.

Prefers a native window via pywebview when that is available, and falls back
to the default browser, which keeps the bundle small and has no hard
dependency on a GUI toolkit.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from contextlib import closing


def free_port(preferred: int = 8000) -> int:
    """An available localhost port, preferring the usual one."""
    for port in (preferred, 8001, 8010, 8080):
        with closing(socket.socket()) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_until_up(port: int, timeout: float = 20.0) -> bool:
    """Block until the server accepts connections, so the window isn't blank."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with closing(socket.socket()) as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


def _serve(port: int) -> None:
    import uvicorn

    # Absolute, not relative: PyInstaller runs this file as __main__ with no
    # parent package, so `from .web.app import ...` fails in the frozen app.
    from lxtool.web.app import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> int:
    port = free_port()
    url = f"http://127.0.0.1:{port}"

    # The server thread is a daemon so closing the window exits the process
    # rather than leaving something running in the background.
    threading.Thread(target=_serve, args=(port,), daemon=True).start()

    if not wait_until_up(port):
        print("LX-Tool: the local server did not start", file=sys.stderr)
        return 1

    if _run_native_window(url):
        return 0

    # No native webview available: fall back to the browser rather than
    # failing, so the tool still works on a machine missing the GUI bits.
    import webbrowser

    print(f"LX-Tool is running at {url}")
    print("Opened in your browser. Close this window to quit.")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def _run_native_window(url: str) -> bool:
    """Show the UI in a real application window.

    Returns False when no native webview is available, so the caller can fall
    back. pywebview drives the platform's own engine - WKWebView on macOS,
    WebView2 on Windows - which is what makes this an app with its own dock
    icon rather than a browser tab.
    """
    try:
        import webview      # type: ignore
    except ImportError:
        return False

    try:
        webview.create_window(
            "LX-Tool",
            url,
            width=1150,
            height=820,
            min_size=(900, 600),
            text_select=True,      # so channel lists can be copied out
        )
        webview.start()
        return True
    except Exception as exc:      # noqa: BLE001 - any backend failure falls back
        print(f"LX-Tool: native window unavailable ({exc}); using browser",
              file=sys.stderr)
        return False


if __name__ == "__main__":
    raise SystemExit(main())
