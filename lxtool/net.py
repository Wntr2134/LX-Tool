"""HTTPS with certificates that actually verify.

macOS ships a Python with no CA bundle of its own, so a plain urlopen fails
with ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``
against every site. The usual advice - run ``Install Certificates.command``,
or worse, disable verification - is either unavailable on the Command Line
Tools build or actively harmful.

Instead: use ``certifi``'s bundle when it is installed, fall back to the
system store otherwise, and if verification still fails, say what to do
about it rather than surfacing an OpenSSL error code.

Verification is never disabled.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request

USER_AGENT = "LX-Tool/1.0 (+https://github.com/Wntr2134/LX-Tool)"

_CERT_HELP = (
    "HTTPS certificate verification failed.\n"
    "This normally means Python has no CA certificates - common with the "
    "macOS system Python.\n"
    "Fix it with:\n"
    "    pip install certifi\n"
    "and run the command again."
)


class CertificateError(RuntimeError):
    """TLS verification failed, with advice attached."""


def ssl_context() -> ssl.SSLContext:
    """A verifying SSL context, preferring certifi's bundle."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def build_opener(*handlers) -> urllib.request.OpenerDirector:
    """An opener that verifies certificates properly."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_context()), *handlers
    )
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


def is_certificate_error(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


def urlopen(url: str, data: bytes | None = None, timeout: int = 60) -> bytes:
    """Fetch a URL, raising :class:`CertificateError` with advice on TLS failure."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            return resp.read()
    except urllib.error.URLError as exc:
        if is_certificate_error(exc):
            raise CertificateError(_CERT_HELP) from exc
        raise
