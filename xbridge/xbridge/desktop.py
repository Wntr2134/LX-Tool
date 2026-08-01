"""XBridge desktop entry point: the panel in its own native window."""

from __future__ import annotations

import socket
import sys
import threading
import time
from contextlib import closing


def free_port(preferred: int = 8765) -> int:
    for port in (preferred, 8766, 8767, 8790):
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

    # Absolute import: PyInstaller runs this file as __main__.
    from xbridge.app import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> int:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    if not wait_until_up(port):
        print("XBridge: the local server did not start", file=sys.stderr)
        return 1

    try:
        import webview      # type: ignore

        webview.create_window("XBridge", url, width=860, height=640,
                              min_size=(700, 480), text_select=True)
        webview.start()
        return 0
    except Exception as exc:  # noqa: BLE001 - fall back to a browser
        import webbrowser

        print(f"XBridge: native window unavailable ({exc}); using browser",
              file=sys.stderr)
        print(f"XBridge is running at {url} - close this window to quit")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
