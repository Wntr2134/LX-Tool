"""The CI build stamp must never lose label() again.

The stamp step used to overwrite _build.py with two bare assignments,
deleting the label() function - so every shipped app said "unknown
build". The step now seds the values in place; these tests pin both the
sed behaviour and the workflow's use of it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_sed_style_stamp_preserves_label(tmp_path):
    src = (ROOT / "lxtool" / "_build.py").read_text(encoding="utf-8")
    stamped = re.sub(r"^COMMIT = .*$", 'COMMIT = "abc1234"', src, flags=re.M)
    stamped = re.sub(r"^DATE = .*$", 'DATE = "2026-08-01"', stamped, flags=re.M)
    mod = tmp_path / "_build.py"
    mod.write_text(stamped, encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-c",
         "import _build; print(_build.label())"],
        cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "abc1234" in out.stdout
    assert "2026-08-01" in out.stdout


def test_workflow_stamps_in_place_and_proves_it():
    wf = (ROOT / ".github" / "workflows" / "build-desktop.yml").read_text()
    assert "> lxtool/_build.py" not in wf, "stamp must not overwrite the file"
    assert 's/^COMMIT = .*/' in wf
    assert "_build.label()" in wf or "print(_build.label())" in wf


def test_version_file_paths_resolve_relative_to_their_spec():
    """PyInstaller resolves EXE(version=...) against the spec's directory -
    a repo-root-relative path broke the Windows build."""
    import re as _re

    for spec in (ROOT / "packaging" / "lxtool.spec",
                 ROOT / "xbridge" / "packaging" / "xbridge.spec"):
        text = spec.read_text(encoding="utf-8")
        m = _re.search(r'version="([^"]+)"', text)
        assert m, f"{spec} has no version resource"
        assert (spec.parent / m.group(1)).is_file(), (
            f"{m.group(1)} not found next to {spec.name}")
