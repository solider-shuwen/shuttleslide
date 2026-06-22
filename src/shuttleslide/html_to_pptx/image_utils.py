"""
Image utilities — fetch images from URLs or local paths for PPTX embedding.

Handles four URL shapes:
  - ``http(s)://...``         → HTTP download via requests
  - ``data:image/...;base64`` → inline base64 decode
  - ``file:///...``           → local file via file:// URI
  - bare relative/absolute path (e.g. ``images/foo.png``, ``/abs/x.png``)
                              → resolved against base_dir (if given), read from disk

After fetching, :func:`ensure_pptx_compatible` converts any unsupported
format (WEBP, etc.) to PNG via Pillow, since python-pptx only accepts
BMP, GIF, JPEG, PNG, TIFF, WMF.
"""

import base64
import io
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

import requests

logger = logging.getLogger(__name__)

# Formats that python-pptx supports natively
_PPTX_SUPPORTED = {'BMP', 'GIF', 'JPEG', 'PNG', 'TIFF', 'WMF'}

# Session with reasonable defaults
_session = requests.Session()
_session.headers.update({"User-Agent": "Shuttleslide/0.1"})
_session.timeout = 30


def _decode_data_uri(url: str) -> Optional[bytes]:
    """Decode a ``data:...;base64,<payload>`` URI to raw bytes."""
    # Format: data:[<mediatype>][;base64],<data>
    if "," not in url:
        return None
    header, payload = url.split(",", 1)
    if "base64" not in header:
        # Non-base64 data: URIs are URL-encoded text. Rare for images;
        # decode percent-encoding and return as bytes.
        return unquote(payload).encode("utf-8")
    try:
        return base64.b64decode(payload)
    except (ValueError, base64.binascii.Error) as e:
        logger.warning("Failed to decode data: URI base64 payload: %s", e)
        return None


def _read_local_path(raw_path: str, base_dir: Optional[Path]) -> Optional[bytes]:
    """Resolve ``raw_path`` against ``base_dir`` (if relative) and read bytes.

    Logs a warning and returns None if the file does not exist. Path
    traversal outside ``base_dir`` is allowed only when the input is an
    absolute path (caller's explicit choice); relative paths that
    escape ``base_dir`` via ``..`` are rejected.
    """
    p = Path(raw_path)
    if not p.is_absolute():
        if base_dir is None:
            logger.warning(
                "Image path %r is relative but no base_dir was provided",
                raw_path,
            )
            return None
        p = (base_dir / raw_path).resolve()
        try:
            p.relative_to(base_dir.resolve())
        except ValueError:
            logger.warning(
                "Image path %r resolves outside base_dir %s "
                "(path traversal guard)", raw_path, base_dir,
            )
            return None
    if not p.is_file():
        logger.warning("Image file not found: %s", p)
        return None
    try:
        return p.read_bytes()
    except OSError as e:
        logger.warning("Failed to read image %s: %s", p, e)
        return None


def download_image(
    url: str,
    timeout: int = 30,
    base_dir: Optional[Path] = None,
) -> Optional[bytes]:
    """
    Fetch an image from a URL or local path and return its raw bytes.

    Schemes handled:
      - ``data:``       — base64 (or url-encoded) inline payload.
      - ``http(s)://``  — HTTP download via the shared requests session.
      - ``file://``     — local file URI; authority/path decoded.
      - bare path       — relative (resolved against ``base_dir``) or absolute.

    Returns None if the fetch fails (the caller should treat the image as
    unavailable and skip or fall back).
    """
    if not url:
        return None

    # data: URIs don't have a parseable scheme via urlparse in older
    # Pythons; check the prefix directly.
    if url.startswith("data:"):
        return _decode_data_uri(url)

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in ("http", "https"):
        try:
            resp = _session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            logger.warning("Failed to download image %s: %s", url, e)
            return None

    if scheme == "file":
        # urlparse leaves the leading slash on the path for
        # file:///abs/path -> "/abs/path"; for file://host/path the
        # host segment is in parsed.netloc. Reassemble via unquote so
        # spaces / non-ASCII in paths work.
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            # file://host/path — only localhost makes sense locally
            logger.warning(
                "Unsupported file:// URI with remote authority %r: %s",
                parsed.netloc, url,
            )
            return None
        local_path = unquote(parsed.path)
        return _read_local_path(local_path, base_dir=None)

    # Empty scheme → relative or absolute bare path.
    if scheme == "":
        return _read_local_path(url, base_dir=base_dir)

    # Unknown scheme (ftp://, blob://, etc.) — bail loudly.
    logger.warning("Unsupported image URL scheme %r: %s", scheme, url)
    return None


def ensure_pptx_compatible(image_bytes: bytes) -> bytes:
    """
    Convert image bytes to a PPTX-compatible format if necessary.

    python-pptx supports: BMP, GIF, JPEG, PNG, TIFF, WMF.
    WEBP and other formats are converted to PNG via Pillow.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed; cannot convert image formats")
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes))
        fmt = img.format  # e.g. 'WEBP', 'JPEG', 'PNG'

        if fmt and fmt.upper() in _PPTX_SUPPORTED:
            return image_bytes

        # Convert to PNG
        buf = io.BytesIO()
        # Convert RGBA to RGB if needed (PNG supports alpha, but safer)
        if img.mode in ('RGBA', 'P'):
            img.save(buf, format='PNG')
        else:
            img.save(buf, format='PNG')

        logger.debug("Converted image from %s to PNG", fmt)
        return buf.getvalue()

    except Exception as e:
        logger.warning("Failed to convert image: %s", e)
        return image_bytes


def guess_image_type(url: str) -> str:
    """Guess the image MIME type from a URL path."""
    url_lower = url.lower().split("?")[0]
    if url_lower.endswith(".png"):
        return "image/png"
    elif url_lower.endswith(".gif"):
        return "image/gif"
    elif url_lower.endswith(".webp"):
        return "image/webp"
    else:
        return "image/jpeg"


class ImageCache:
    """Simple in-memory cache for fetched images to avoid re-fetching.

    Args:
        base_dir: Directory that relative image URLs (``images/foo.png``)
            resolve against. Typically the HTML file's parent directory.
            When None, only absolute paths and http(s)/data/file URLs work.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self._cache: dict[str, Optional[bytes]] = {}
        self._base_dir = base_dir

    def get(self, url: str) -> Optional[bytes]:
        if url not in self._cache:
            raw = download_image(url, base_dir=self._base_dir)
            if raw:
                self._cache[url] = ensure_pptx_compatible(raw)
            else:
                self._cache[url] = None
        return self._cache[url]

    def clear(self):
        self._cache.clear()
