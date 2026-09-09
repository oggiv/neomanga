"""MangaPlus web-API client.

Uses the same endpoints the MangaPlus website (a JavaScript SPA) uses::

    https://jumpg-webapi.tokyo-cdn.com/api

Responses are protobuf; they are decoded with ``proto2dict`` from the
installed ``mangaplus`` package.  The mobile API client from that package
is deliberately NOT used: it sends Android parameters (os / os_ver /
app_ver / secret) which are not appropriate for a web client.

Verified against the live site's own JavaScript bundle, the web client:

* sends a client-generated UUID as the ``Session-Token`` header,
* calls ``title_detailV3`` with ``{title_id, clang}``,
* calls ``manga_viewer_v3`` with
  ``{chapter_id, split="yes", img_quality, viewer_mode, clang}``,
* receives a per-chapter ``vwToken`` (protobuf field 19 of MangaViewer,
  which the installed ``mangaplus`` .proto does not know about, so it is
  extracted manually from the raw bytes),
* sends that token back as the ``Plus-Vw-Token`` header when downloading
  page images (without it the image CDN answers ``400 Bad Request``),
* XOR-decrypts each page with the ``encryptionKey`` supplied alongside
  the image URL (this is exactly what the web reader does to display the
  page; no other transformation is applied).

Only chapters/pages the API serves to this unauthenticated web client are
downloaded.  Anything else (API error, missing token, undecryptable image)
is treated as "unavailable" and skipped -- never bypassed.
"""

import logging
import uuid

import requests

try:
    from mangaplus.shueisha import proto2dict
except ImportError:  # pragma: no cover - handled gracefully at runtime
    proto2dict = None

log = logging.getLogger("mangaplus_client")

API_BASE = "https://jumpg-webapi.tokyo-cdn.com/api"
TIMEOUT = 20
IMAGE_TIMEOUT = 30


def _read_varint(buf, pos):
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 64:
            raise ValueError("malformed varint")


def _walk_fields(buf, start, end):
    """Yield (field_number, wire_type, value_start, value_end) for a message."""
    pos = start
    while pos < end:
        tag, pos = _read_varint(buf, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 0:  # varint
            _, pos = _read_varint(buf, pos)
            yield field, wire, pos, pos
        elif wire == 1:  # 64-bit
            yield field, wire, pos, pos + 8
            pos += 8
        elif wire == 2:  # length-delimited
            length, pos = _read_varint(buf, pos)
            yield field, wire, pos, pos + length
            pos += length
        elif wire == 5:  # 32-bit
            yield field, wire, pos, pos + 4
            pos += 4
        else:
            raise ValueError("unsupported protobuf wire type %r" % wire)


def _find_submessage(buf, start, end, field_number):
    for field, wire, value_start, value_end in _walk_fields(buf, start, end):
        if field == field_number and wire == 2:
            return value_start, value_end
    return None


def extract_vw_token(raw):
    """Extract MangaViewer.vwToken (protobuf field 19) from raw bytes.

    Returns the token string, or None if it cannot be found.  The installed
    ``mangaplus`` protobuf schema predates this field, so ``proto2dict``
    silently drops it and it must be read from the wire format directly::

        Response.success (1) -> SuccessResult.mangaViewer (10)
        -> MangaViewer.vwToken (19)
    """
    try:
        sub = _find_submessage(raw, 0, len(raw), 1)
        if sub is None:
            return None
        sub = _find_submessage(raw, sub[0], sub[1], 10)
        if sub is None:
            return None
        token = _find_submessage(raw, sub[0], sub[1], 19)
        if token is None:
            return None
        return bytes(raw[token[0]:token[1]]).decode("utf-8") or None
    except (ValueError, IndexError, UnicodeDecodeError) as exc:
        log.debug("vwToken extraction failed: %s", exc)
        return None


def decrypt_image(data, encryption_key_hex):
    """Decrypt a page with the key the API supplied for that page.

    This mirrors the web reader: repeating-key XOR.  Returns the decrypted
    bytes, or None if there is no usable key.
    """
    if not encryption_key_hex:
        return None
    try:
        key = bytes.fromhex(encryption_key_hex)
    except (ValueError, TypeError):
        return None
    if not key:
        return None
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(bytes(data)))


def sniff_image_type(data):
    """Return 'jpeg'/'png'/'webp'/'gif' from magic bytes, else None."""
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def parse_chapter_number(name):
    """Extract the integer chapter number from a MangaPlus chapter ``name``.

    >>> parse_chapter_number("#019")
    19
    """
    if not name:
        return None
    import re

    match = re.search(r"#?\s*0*(\d+)", str(name))
    if not match:
        return None
    try:
        number = int(match.group(1))
    except ValueError:
        return None
    return number if number > 0 else None


def flatten_chapters(title_detail_view):
    """Flatten first/mid/lastChapterList into one list of chapter dicts."""
    chapters = []
    for group in title_detail_view.get("chapterListGroup") or []:
        for key in ("firstChapterList", "midChapterList", "lastChapterList"):
            chapters.extend(group.get(key) or [])
    return chapters


class MangaPlusWebClient:
    """Small web-API client; one instance reuses one session token."""

    def __init__(self, session_token=None, timeout=TIMEOUT):
        self.session_token = session_token or str(uuid.uuid4())
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip",
                "Origin": "https://mangaplus.shueisha.co.jp",
                "Referer": "https://mangaplus.shueisha.co.jp/",
                "User-Agent": "Mozilla/5.0",
                "Session-Token": self.session_token,
            }
        )

    # -- low-level ----------------------------------------------------
    def _get(self, endpoint, params):
        """GET an API endpoint; return decoded ``success`` dict or None."""
        raw = self._get_raw(endpoint, params)
        if raw is None:
            return None
        return self._decode_success(raw, endpoint)

    def _get_raw(self, endpoint, params):
        """GET an API endpoint; return raw protobuf bytes or None."""
        if proto2dict is None:
            log.error("mangaplus package not available; cannot decode API")
            return None
        try:
            response = self.session.get(
                "%s/%s" % (API_BASE, endpoint),
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            log.warning("MangaPlus request %s failed: %s", endpoint, exc)
            return None
        if not response.ok:
            log.warning(
                "MangaPlus request %s HTTP %s", endpoint, response.status_code
            )
            return None
        return response.content

    @staticmethod
    def _decode_success(raw, endpoint="<unknown>"):
        try:
            data = proto2dict(raw)
        except Exception as exc:  # protobuf decode failure
            log.warning("MangaPlus %s decode failed: %s", endpoint, exc)
            return None
        # NOTE: must test the *value* of data.get("error"); successful and
        # failed responses share structure, so `"error" in data` is wrong.
        if data.get("error"):
            popup = (data["error"].get("englishPopup") or {})
            log.info(
                "MangaPlus %s API error: %s | %s",
                endpoint,
                popup.get("subject"),
                (popup.get("body") or "")[:200],
            )
            return None
        return data.get("success")

    # -- title details ------------------------------------------------
    def get_title_detail(self, title_id, lang="eng"):
        """Return the ``titleDetailView`` dict, or None on any failure."""
        try:
            title_id = int(title_id)
        except (TypeError, ValueError):
            log.warning("invalid title_id %r", title_id)
            return None
        success = self._get(
            "title_detailV3", {"title_id": title_id, "clang": lang}
        )
        if not success:
            return None
        view = success.get("titleDetailView")
        if not view:
            log.warning("title_detailV3: missing titleDetailView")
            return None
        return view

    # -- chapter viewer -----------------------------------------------
    def get_chapter_viewer(self, chapter_id):
        """Return ``(mangaViewer dict, vwToken)`` or ``(None, None)``.

        ``mangaViewer`` is the decoded dict (page list with image URLs and
        encryption keys).  ``vwToken`` must be sent back as the
        ``Plus-Vw-Token`` header when downloading those image URLs.
        """
        try:
            chapter_id = int(chapter_id)
        except (TypeError, ValueError):
            log.warning("invalid chapter_id %r", chapter_id)
            return None, None
        raw = self._get_raw(
            "manga_viewer_v3",
            {
                "chapter_id": chapter_id,
                "split": "yes",
                "img_quality": "super_high",
                "viewer_mode": "vertical",
                "clang": "eng",
            },
        )
        if raw is None:
            return None, None
        success = self._decode_success(raw, "manga_viewer_v3")
        if not success:
            return None, None
        viewer = success.get("mangaViewer")
        if not viewer:
            log.info("manga_viewer_v3: chapter %s has no viewer", chapter_id)
            return None, None
        return viewer, extract_vw_token(raw)

    def get_chapter_pages(self, chapter_id):
        """Return ``(pages, vwToken)`` with only real manga pages.

        ``pages`` is a list of ``mangaPage`` dicts (imageUrl,
        encryptionKey, ...) in reading order.  Returns ``(None, None)``
        when the chapter is unavailable or the request failed.
        """
        viewer, vw_token = self.get_chapter_viewer(chapter_id)
        if viewer is None:
            return None, None
        pages = [
            page["mangaPage"]
            for page in viewer.get("pages") or []
            if page.get("mangaPage") and page["mangaPage"].get("imageUrl")
        ]
        if not pages:
            log.info("chapter %s: no downloadable pages", chapter_id)
            return None, None
        if not vw_token:
            # The web reader always sends this header; without the token
            # the image CDN refuses the request, so treat as unavailable.
            log.info("chapter %s: no vwToken; skipping", chapter_id)
            return None, None
        return pages, vw_token

    # -- page images ---------------------------------------------------
    def download_image(self, url, vw_token):
        """Download one page image; return bytes or None."""
        try:
            response = self.session.get(
                url,
                headers={
                    "Referer": "https://mangaplus.shueisha.co.jp/",
                    "Accept": "image/avif,image/webp,image/*,*/*",
                    "Plus-Vw-Token": vw_token,
                },
                timeout=IMAGE_TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("page download failed: %s", exc)
            return None
        if not response.ok or not response.content:
            log.warning(
                "page download HTTP %s (%d bytes)",
                response.status_code,
                len(response.content or b""),
            )
            return None
        return response.content
