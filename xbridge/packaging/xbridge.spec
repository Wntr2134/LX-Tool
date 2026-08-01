# PyInstaller spec for the XBridge desktop app.
#
# Build with:  pyinstaller --noconfirm xbridge/packaging/xbridge.spec
# (run from the repository root; must be built on the platform it targets)

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = [
    "xbridge",
    "xbridge.app",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "webview",
    "webview.platforms.cocoa",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    # mido picks its backend at runtime by name.
    "mido",
    "mido.backends.rtmidi",
    "rtmidi",
    # The MA2 target's websocket client, imported lazily.
    "websockets",
    "websockets.sync.client",
]
hidden += collect_submodules("xbridge")

a = Analysis(
    ["../xbridge/desktop.py"],
    pathex=[str(Path(".").resolve())],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
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
    name="XBridge",
    version="xbridge_version.txt" if sys.platform == "win32" else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
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
        name="XBridge.app",
        icon=None,
        bundle_identifier="uk.co.lxtool.xbridge",
        info_plist={
            "CFBundleName": "XBridge",
            "CFBundleDisplayName": "XBridge",
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
