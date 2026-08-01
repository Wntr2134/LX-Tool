"""Pull the text out of a PDF, so a printed patch can be read back in.

MagicQ (and every other desk) will happily give you the patch as a PDF
when the CSV has gone missing. `pypdf` is pure Python and ships with the
packaged app; without it the caller gets a clear message rather than a
crash, and the CSV route still works.

Table text out of a PDF arrives ragged - cells become runs of spaces -
which is exactly what :func:`lxtool.patchlist.parse` is built to cope
with.
"""

from __future__ import annotations


def available() -> tuple[bool, str]:
    """(usable, reason). Importing pypdf is itself the risky step.

    pypdf pulls in `cryptography` at import time, whose Rust bindings
    raise a PanicException - a BaseException, not an Exception - when
    that install is broken. Catching only ImportError here would let a
    broken dependency take the whole app down instead of disabling one
    feature.
    """
    try:
        import pypdf  # noqa: F401
        return True, "pypdf"
    except ImportError:
        return False, ("PDF reading needs pypdf: pip install pypdf "
                       "(or export the patch as CSV instead)")
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:      # noqa: BLE001
        return False, (f"PDF reading is unavailable on this machine ({exc}) "
                       "- export the patch as CSV instead")


def read_text(data: bytes) -> str:
    """Every page's text, in order. Raises RuntimeError with the reason."""
    ok, detail = available()
    if not ok:
        raise RuntimeError(detail)
    import io

    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:      # noqa: BLE001
        # BaseException, not Exception: pypdf reaches for `cryptography`,
        # whose Rust bindings raise a PanicException (a BaseException) when
        # that install is broken. A bad PDF - or a bad dependency - must
        # surface as a message, never as a crashed app.
        raise RuntimeError(f"could not open that PDF: {exc}") from exc
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            raise RuntimeError("that PDF is password-protected") from None

    out = []
    for page in reader.pages:
        try:
            out.append(page.extract_text() or "")
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 - one bad page loses only itself
            continue
    text = "\n".join(out)
    if not text.strip():
        raise RuntimeError(
            "no text in that PDF - it is probably a scan or an image "
            "export; use the CSV, or screenshot it and use the image box")
    return text
