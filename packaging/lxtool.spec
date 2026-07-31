# PyInstaller spec for the LX-Tool desktop app.
#
# Build with:  pyinstaller --noconfirm packaging/lxtool.spec
#
# Produces a single-window app that runs the local server. It has to be built
# on the platform it targets - PyInstaller does not cross-compile - so CI does
# macOS and Windows separately (.github/workflows/build-desktop.yml).

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# uvicorn and fastapi load parts of themselves dynamically, so PyInstaller's
# static analysis misses them without help.
hidden = [
    # The app package itself: desktop.py imports it absolutely at runtime, so
    # static analysis of the entry script alone does not pull it in.
    "lxtool",
    "lxtool.web.app",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # pywebview picks its backend at runtime, so the platform module has to be
    # named explicitly or the frozen app silently falls back to a browser.
    "webview",
    "webview.platforms.cocoa",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    # The X-Touch bridge. mido picks its backend at runtime by name, so the
    # rtmidi backend must be listed or the frozen app has silent no-MIDI.
    "mido",
    "mido.backends.rtmidi",
    "rtmidi",
]

# Many lxtool submodules (_build, mylib, plan, chart, net, gdtfshare, ...) are
# imported lazily inside functions, so PyInstaller's static analysis of the
# entry script never sees them and leaves them out of the bundle - which makes
# the frozen app raise ImportError (a 500 on the very first page) the moment
# one of those code paths runs. Collect the whole package so every submodule
# ships, regardless of how it is imported.
hidden += collect_submodules("lxtool")

a = Analysis(
    ["../lxtool/desktop.py"],
    pathex=[str(Path(".").resolve())],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here needs a GUI toolkit or scientific stack; excluding them
    # keeps the download small.
    excludes=["matplotlib", "numpy", "PIL", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LX-Tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed on macOS (no terminal behind the app); console on Windows so
    # errors are visible, since unsigned Windows builds fail quietly otherwise.
    console=sys.platform != "darwin",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="LX-Tool.app",
        icon=None,
        bundle_identifier="uk.co.lxtool.app",
        info_plist={
            "CFBundleName": "LX-Tool",
            "CFBundleDisplayName": "LX-Tool",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            # No server sockets are exposed beyond localhost.
            "LSMinimumSystemVersion": "11.0",
        },
    )
