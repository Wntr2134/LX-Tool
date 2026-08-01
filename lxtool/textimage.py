"""Read text out of an image, using the OS's own OCR engine.

The point is dropping a screenshot of a DMX chart or a patch sheet
straight into the app, instead of doing select-text-in-image on a phone
first. Both desktop platforms ship a capable OCR engine, so no ML
dependency is bundled:

- macOS: the Vision framework (VNRecognizeTextRequest), via pyobjc.
- Windows: the WinRT OCR engine (Windows.Media.Ocr), via winsdk.
- elsewhere (incl. this project's Linux CI): unavailable, with a clear
  message - the phone route still works.

Lines come back top-to-bottom so the chart parser sees the same shape a
human copy-paste would produce.
"""

from __future__ import annotations

import sys


def available() -> tuple[bool, str]:
    """(usable, detail): detail is the backend name, or the reason not."""
    if sys.platform == "darwin":
        try:
            import Vision  # noqa: F401
            return True, "macOS Vision"
        except ImportError:
            return False, ("macOS Vision framework not installed "
                           "(pip install pyobjc-framework-Vision)")
    if sys.platform == "win32":
        try:
            import winsdk.windows.media.ocr  # noqa: F401
            return True, "Windows OCR"
        except ImportError:
            return False, ("Windows OCR support not installed "
                           "(pip install winsdk)")
    return False, (f"no OCR engine on {sys.platform} - use your phone's "
                   "select-text-in-image and paste instead")


def read_text(image_bytes: bytes, suffix: str = ".png") -> str:
    """OCR one image (PNG/JPEG bytes) into plain text lines.

    Raises RuntimeError with a human-readable reason when it can't.
    """
    ok, detail = available()
    if not ok:
        raise RuntimeError(detail)
    if sys.platform == "darwin":
        return _read_macos(image_bytes)
    return _read_windows(image_bytes, suffix)


def _read_macos(image_bytes: bytes) -> str:
    import Vision
    from Foundation import NSData

    data = NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
        data, {})
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)   # DMX charts aren't prose

    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {error}")

    # Sort by top edge, descending y (Vision's origin is bottom-left).
    lines = []
    for obs in request.results() or []:
        candidate = obs.topCandidates_(1)
        if candidate and len(candidate):
            box = obs.boundingBox()
            lines.append((-box.origin.y, box.origin.x,
                          str(candidate[0].string())))
    lines.sort()
    return "\n".join(text for _, _, text in lines)


def _read_windows(image_bytes: bytes, suffix: str) -> str:
    import asyncio
    import tempfile
    from pathlib import Path

    async def _run() -> str:
        from winsdk.windows.graphics.imaging import BitmapDecoder
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.storage import FileAccessMode, StorageFile

        tmp = Path(tempfile.mkdtemp(prefix="lxtool-ocr-")) / f"img{suffix}"
        tmp.write_bytes(image_bytes)
        try:
            f = await StorageFile.get_file_from_path_async(str(tmp))
            stream = await f.open_async(FileAccessMode.READ)
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                raise RuntimeError(
                    "Windows OCR has no language pack installed - add one "
                    "under Settings > Time & Language")
            result = await engine.recognize_async(bitmap)
            return "\n".join(line.text for line in result.lines)
        finally:
            tmp.unlink(missing_ok=True)

    return asyncio.run(_run())
