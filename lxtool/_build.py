"""Build stamp, so a running app can say exactly which version it is.

CI overwrites this file just before packaging, filling in the real commit
and date. In a source checkout it stays "dev", which is the honest answer
there. Nothing else should depend on these values.
"""

from __future__ import annotations

COMMIT = "dev"
DATE = ""


def label() -> str:
    """A short human string like 'build 2026-07-29 (fc2c56f)' or 'dev'."""
    if COMMIT == "dev" or not COMMIT:
        return "dev build"
    date = f"{DATE} " if DATE else ""
    return f"build {date}({COMMIT})"
