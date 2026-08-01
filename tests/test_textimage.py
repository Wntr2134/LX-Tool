"""Built-in OCR: honest degradation everywhere it cannot run."""

from __future__ import annotations

import pytest


def test_ocr_is_honest_about_availability():
    import sys

    from lxtool import textimage

    ok, detail = textimage.available()
    if sys.platform not in ("darwin", "win32"):
        assert not ok
        assert "phone" in detail
        with pytest.raises(RuntimeError):
            textimage.read_text(b"not an image")


def test_ocr_endpoint_maps_unavailable_to_409(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from lxtool import textimage
    from lxtool.web import app as web

    monkeypatch.setattr(textimage, "available",
                        lambda: (False, "no OCR engine here"))

    class FakeUpload:
        filename = "chart.png"

        async def read(self):
            return b"\x89PNG fake"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(web.api_ocr(FakeUpload()))
    assert exc.value.status_code == 409
    assert "no OCR engine" in exc.value.detail


# ---- the stored mapping (what the editor UI reads and writes) ---------
