"""MangaPlus synchronization: config, local-chapter detection, downloading.

Configuration (``manga.json``, next to ``server.py``) maps a *relative*
folder path to a MangaPlus title id and is easy to edit by hand::

    {
        "Haunted Peak": {"title_id": 100728},
        "Some/Nested Folder": {"title_id": 123456},
        "discord_webhook_url": "https://discord.com/api/webhooks/... (optional)"
    }

Folders without an entry behave exactly as before (plain local reader).
``discord_webhook_url`` may also come from the ``DISCORD_WEBHOOK_URL``
environment variable, which takes precedence.

Runtime metadata lives in ``mangaplus_cache.json`` (same directory)::

    {
        "Haunted Peak": {
            "title_id": 100728,
            "title_name": "Haunted Peak",
            "next_timestamp": 1788966000,
            "last_check": 1788900000,
            "last_status": "ok",
            "unavailable": {"1028983": 1788900000}
        }
    }

Only ``manga.json`` is meant to be edited by hand.
"""

import json
import logging
import os
import re
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from mangaplus_client import (
    MangaPlusWebClient,
    decrypt_image,
    flatten_chapters,
    parse_chapter_number,
)
from discord import DiscordNotifier

log = logging.getLogger("manga_sync")

ROOT = Path(__file__).resolve().parent
MANGA_ROOT = (ROOT / "manga").resolve()
CONFIG_FILE = ROOT / "manga.json"
CACHE_FILE = ROOT / "mangaplus_cache.json"

IMAGE_EXT_BY_TYPE = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "gif": ".gif",
}
URL_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Skip re-probing a chapter that was unavailable recently (unless forced).
UNAVAILABLE_RETRY_SECONDS = 7 * 24 * 3600
# Full re-check interval when no future release timestamp is known.
FALLBACK_RECHECK_SECONDS = 24 * 3600
# Safety margin added on top of a release timestamp before re-checking.
RELEASE_DELAY_SECONDS = 15 * 60

_state_lock = threading.Lock()
STATUS = {}  # rel posix -> cache entry (in-memory copy of CACHE_FILE)
sync_lock = threading.Lock()


# --------------------------------------------------------------------------
# config / cache
# --------------------------------------------------------------------------

def load_config():
    """Return ``(folders, discord_url)`` from manga.json (never raises)."""
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}, ""
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("cannot read %s: %s", CONFIG_FILE.name, exc)
        return {}, ""
    if not isinstance(data, dict):
        log.warning("%s is not a JSON object", CONFIG_FILE.name)
        return {}, ""
    discord_url = data.get("discord_webhook_url") or ""
    folders = {}
    for rel, entry in data.items():
        if rel == "discord_webhook_url" or not isinstance(entry, dict):
            continue
        try:
            title_id = int(entry.get("title_id"))
        except (TypeError, ValueError):
            log.warning("bad title_id for %r in %s", rel, CONFIG_FILE.name)
            continue
        folder = resolve_folder(rel)
        if folder is None:
            log.warning("ignoring %r: outside manga directory", rel)
            continue
        folders[folder.relative_to(MANGA_ROOT).as_posix()] = title_id
    return folders, discord_url if isinstance(discord_url, str) else ""


def resolve_folder(rel):
    """Resolve a configured relative path; None if it escapes MANGA_ROOT."""
    candidate = (MANGA_ROOT / str(rel).replace("\\", "/")).resolve()
    try:
        candidate.relative_to(MANGA_ROOT)
    except ValueError:
        return None
    return candidate


def load_cache():
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_cache(data):
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False,
                      sort_keys=True)
        os.replace(tmp, CACHE_FILE)
    except OSError as exc:
        log.warning("cannot write cache: %s", exc)


def refresh_status():
    """Reload the on-disk cache into memory (call at startup)."""
    with _state_lock:
        STATUS.clear()
        STATUS.update(load_cache())


def get_status(rel_posix):
    """Cached metadata for a folder (used by the directory view; no I/O)."""
    with _state_lock:
        entry = STATUS.get(rel_posix)
        return dict(entry) if entry else {}


def _update_status(rel_posix, entry):
    with _state_lock:
        STATUS[rel_posix] = dict(entry)
        save_cache(dict(STATUS))


def _prune_missing(disk_cache):
    for rel in [r for r in disk_cache if not resolve_folder(r)]:
        disk_cache.pop(rel, None)
    for rel in [r for r in disk_cache
                if not (MANGA_ROOT / r).is_dir()]:
        disk_cache.pop(rel, None)
        with _state_lock:
            STATUS.pop(rel, None)


# --------------------------------------------------------------------------
# local chapter-number detection
# --------------------------------------------------------------------------

# Ordered, most-explicit first.  A bare number only counts when it stands
# alone at the start (before a separator) or end of the filename, so years,
# volume numbers buried mid-name, etc. are not mistaken for chapters.
_CHAPTER_PATTERNS = (
    re.compile(r"(?i)\bchapters?\s*#?\s*0*(\d{1,4})\b"),
    re.compile(r"(?i)\bch\.?\s*#?\s*0*(\d{1,4})\b"),
    re.compile(r"#\s*0*(\d{1,4})\b"),
    re.compile(r"[\s._(\[]0*(\d{1,4})\s*$"),
    re.compile(r"^\s*0*(\d{1,4})\s*[-–—.)\] _]"),
)


def extract_chapter_number(path):
    """Infer a MangaPlus chapter number from a CBZ filename.

    Returns ``int`` or ``None``.  Ambiguous names return None on purpose:
    it is better to (possibly) re-download than to wrongly believe a
    chapter is already local.  Handles e.g.::

        "Haunted Peak - Chapter 19.cbz"  -> 19
        "Haunted Peak 019.cbz"           -> 19
        "Chapter 19.cbz"                 -> 19
        "19 - Impurity [MangaPlus].cbz"  -> 19
    """
    stem = Path(str(path)).stem
    for pattern in _CHAPTER_PATTERNS:
        match = pattern.search(stem)
        if match:
            try:
                number = int(match.group(1))
            except ValueError:
                continue
            if 0 < number < 10000:
                return number
    return None


def scan_local_chapters(folder):
    """Return the set of chapter numbers already present as CBZ files."""
    found = set()
    try:
        entries = list(folder.iterdir())
    except OSError as exc:
        log.warning("cannot list %s: %s", folder, exc)
        return found
    for entry in entries:
        if not (entry.is_file() and entry.suffix.lower() == ".cbz"):
            continue
        number = extract_chapter_number(entry.name)
        if number is not None:
            found.add(number)
    return found


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

def _page_extension(image_url, image_type):
    suffix = Path(image_url.split("?", 1)[0]).suffix.lower()
    if suffix in URL_IMAGE_EXTS:
        return ".jpg" if suffix == ".jpeg" else suffix
    if image_type and image_type in IMAGE_EXT_BY_TYPE:
        return IMAGE_EXT_BY_TYPE[image_type]
    return None


def download_chapter(client, folder, chapter_number, chapter_id):
    """Download one chapter into ``folder`` as a CBZ.

    Returns the final Path on success, None otherwise.  Pages are written
    to a temp dir first and the finished CBZ is moved into place atomically,
    so a failed/interrupted download never leaves a half-written CBZ behind.
    Never overwrites an existing CBZ.
    """
    from mangaplus_client import sniff_image_type

    target = folder / ("%s - Chapter %d.cbz"
                       % (folder.name, chapter_number))
    if target.exists():
        return target

    pages, vw_token = client.get_chapter_pages(chapter_id)
    if not pages:
        return None  # unavailable or failed; already logged

    log.info("downloading chapter %d (%d pages) -> %s",
             chapter_number, len(pages), target.name)
    with tempfile.TemporaryDirectory(prefix="mpdl-") as tmpdir:
        tmpdir = Path(tmpdir)
        for index, page in enumerate(pages, start=1):
            url = page.get("imageUrl")
            key = page.get("encryptionKey")
            data = None
            for attempt in (1, 2):
                data = client.download_image(url, vw_token)
                if data:
                    break
                log.warning("page %d of chapter %d: retrying (%d/2)",
                            index, chapter_number, attempt)
                time.sleep(1)
            if not data:
                log.warning("chapter %d: giving up on page %d",
                            chapter_number, index)
                return None
            image = decrypt_image(data, key) if key else bytes(data)
            if image is None:
                log.warning("chapter %d page %d: no usable key",
                            chapter_number, index)
                return None
            image_type = sniff_image_type(image)
            if image_type is None:
                log.warning("chapter %d page %d: unrecognized image data",
                            chapter_number, index)
                return None
            ext = _page_extension(url, image_type)
            if ext is None:
                log.warning("chapter %d page %d: unknown extension",
                            chapter_number, index)
                return None
            try:
                (tmpdir / ("%03d%s" % (index, ext))).write_bytes(image)
            except OSError as exc:
                log.warning("chapter %d: temp write failed: %s",
                            chapter_number, exc)
                return None

        tmp_cbz = tmpdir / "chapter.cbz"
        try:
            with zipfile.ZipFile(tmp_cbz, "w", zipfile.ZIP_STORED) as archive:
                for index in range(1, len(pages) + 1):
                    matches = sorted(tmpdir.glob("%03d.*" % index))
                    if not matches:
                        log.warning("chapter %d: missing temp page %d",
                                    chapter_number, index)
                        return None
                    archive.write(matches[0], matches[0].name)
        except OSError as exc:
            log.warning("chapter %d: CBZ creation failed: %s",
                        chapter_number, exc)
            return None

        # Verify before publishing: valid zip, every page, in order.
        try:
            with zipfile.ZipFile(tmp_cbz) as check:
                names = [i.filename for i in check.infolist()]
                if len(names) != len(pages) or check.testzip() is not None:
                    raise zipfile.BadZipFile("page count/content mismatch")
        except zipfile.BadZipFile as exc:
            log.warning("chapter %d: CBZ verification failed: %s",
                        chapter_number, exc)
            return None

        try:
            if target.exists():  # raced with another process; keep theirs
                return target
            os.replace(tmp_cbz, target)
        except OSError as exc:
            log.warning("chapter %d: cannot publish CBZ: %s",
                        chapter_number, exc)
            return None
    log.info("chapter %d saved: %s", chapter_number, target.name)
    return target


# --------------------------------------------------------------------------
# synchronization
# --------------------------------------------------------------------------

def parse_next_timestamp(view):
    """Unix int from titleDetailView["nextTimeStamp"], else None."""
    try:
        value = int(view.get("nextTimeStamp"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def format_next_release(timestamp):
    """Human-readable release time in the local/system timezone."""
    try:
        moment = datetime.fromtimestamp(int(timestamp)).astimezone()
    except (TypeError, ValueError, OverflowError, OSError):
        return "Unknown"
    return moment.strftime("%B %d, %Y at %H:%M")


def synchronize_folder(client, rel_posix, title_id, notifier, force=False):
    """Sync one folder; returns a result dict and updates the cache."""
    folder = resolve_folder(rel_posix)
    if folder is None or not folder.is_dir():
        log.warning("skipping %r: not a manga folder", rel_posix)
        return {"rel": rel_posix, "skipped": True}

    now = int(time.time())
    disk_cache = load_cache()
    entry = disk_cache.get(rel_posix, {})
    unavailable = entry.get("unavailable") or {}

    view = client.get_title_detail(title_id)
    if view is None:
        entry.update({"title_id": title_id, "last_check": now,
                      "last_status": "title fetch failed"})
        _update_status(rel_posix, entry)
        return {"rel": rel_posix, "error": "title fetch failed"}

    title = view.get("title") or {}
    title_name = title.get("name") or rel_posix
    next_timestamp = parse_next_timestamp(view)
    log.info("synchronizing %s (title %s, id %s)", rel_posix,
             title_name, title_id)

    local = scan_local_chapters(folder)
    chapters = flatten_chapters(view)
    log.info("%s: %d known chapters, %d local", title_name,
             len(chapters), len(local))

    downloaded = []
    skipped_unavailable = 0
    for chapter in chapters:
        number = parse_chapter_number(chapter.get("name"))
        chapter_id = chapter.get("chapterId")
        if number is None or not chapter_id:
            continue
        if number in local:
            continue
        try:
            chapter_id = int(chapter_id)
        except (TypeError, ValueError):
            continue
        last_fail = unavailable.get(str(chapter_id), 0)
        if not force and now - last_fail < UNAVAILABLE_RETRY_SECONDS:
            skipped_unavailable += 1
            continue
        result = download_chapter(client, folder, number, chapter_id)
        if result is not None and result.exists():
            local.add(number)
            downloaded.append(number)
            entry.pop("unavailable", None)  # refresh below
            unavailable.pop(str(chapter_id), None)
            notifier.notify_chapter_downloaded(title_name, number)
        else:
            unavailable[str(chapter_id)] = now
            skipped_unavailable += 1

    entry.update({
        "title_id": title_id,
        "title_name": title_name,
        "next_timestamp": next_timestamp,
        "last_check": now,
        "last_status": "ok",
        "unavailable": unavailable,
    })
    _update_status(rel_posix, entry)
    if downloaded:
        log.info("%s: downloaded chapters %s", title_name,
                 sorted(downloaded))
    return {"rel": rel_posix, "title": title_name,
            "downloaded": sorted(downloaded),
            "skipped_unavailable": skipped_unavailable,
            "next_timestamp": next_timestamp}


def make_notifier(config_discord_url=""):
    import os as _os

    url = (_os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not url and config_discord_url:
        url = config_discord_url.strip()
    return DiscordNotifier(url or None)


def synchronize_all(client=None, force=False):
    """Sync every configured folder.  Never runs twice concurrently.

    Returns a result dict; ``{"ran": False}`` if another sync is active.
    """
    if not sync_lock.acquire(blocking=False):
        log.info("sync already running; skipping")
        return {"ran": False}
    try:
        folders, config_discord = load_config()
        if not folders:
            log.info("no MangaPlus folders configured; nothing to sync")
            return {"ran": True, "results": []}
        client = client or MangaPlusWebClient()
        notifier = make_notifier(config_discord)
        results = []
        for rel, title_id in sorted(folders.items()):
            try:
                results.append(synchronize_folder(
                    client, rel, title_id, notifier, force=force))
            except Exception:  # one bad folder must not stop the rest
                log.exception("sync of %s failed", rel)
                results.append({"rel": rel, "error": "exception"})
        with _state_lock:
            disk_cache = load_cache()
        _prune_missing(disk_cache)
        save_cache(disk_cache)
        return {"ran": True, "results": results}
    finally:
        sync_lock.release()


def earliest_next_timestamp():
    """Soonest known future release across cached titles, else None."""
    now = int(time.time())
    soonest = None
    with _state_lock:
        entries = list(STATUS.values())
    for entry in entries:
        timestamp = entry.get("next_timestamp")
        try:
            timestamp = int(timestamp)
        except (TypeError, ValueError):
            continue
        if timestamp > now and (soonest is None or timestamp < soonest):
            soonest = timestamp
    return soonest


# --------------------------------------------------------------------------
# scheduler: exactly one pending future check, in-process
# --------------------------------------------------------------------------

class SyncScheduler(threading.Thread):
    """Background thread: sync now, then re-sync at each next release.

    Only one future check ever exists; scheduling a new release replaces
    the old one.  Never blocks the HTTP server (daemon thread).
    """

    daemon = True

    def __init__(self):
        super().__init__(name="mangaplus-sync", daemon=True)
        self._wake = threading.Event()
        self._forced = False
        self._stopped = False
        self._next_run = None
        self._lock = threading.Lock()

    # -- control ------------------------------------------------------
    def trigger_now(self, force=True):
        """Request an out-of-band full sync (manual trigger)."""
        with self._lock:
            self._forced = force or self._forced
        self._wake.set()

    def stop(self):
        self._stopped = True
        self._wake.set()

    @property
    def next_run(self):
        with self._lock:
            return self._next_run

    def _set_next_run(self, timestamp):
        with self._lock:
            self._next_run = timestamp
            if timestamp:
                log.info(
                    "next MangaPlus check scheduled for %s",
                    format_next_release(timestamp),
                )
            else:
                log.info("no future MangaPlus check scheduled")

    # -- main loop ----------------------------------------------------
    def run(self):
        log.info("MangaPlus scheduler started; running initial sync")
        self._sync_and_reschedule(force=False)
        while not self._stopped:
            with self._lock:
                target = self._next_run
            if target is None:
                timeout = FALLBACK_RECHECK_SECONDS
            else:
                timeout = max(0, target - time.time())
            fired = self._wake.wait(timeout)
            self._wake.clear()
            if self._stopped:
                break
            with self._lock:
                forced, self._forced = self._forced, False
            if fired:
                log.info("manual MangaPlus sync triggered")
                self._sync_and_reschedule(force=forced)
            else:
                log.info("scheduled MangaPlus check reached")
                self._sync_and_reschedule(force=False)

    def _sync_and_reschedule(self, force):
        try:
            synchronize_all(force=force)
        except Exception:
            log.exception("background sync failed")
        soonest = earliest_next_timestamp()
        if soonest is None:
            # No known release: fall back to a periodic re-check so newly
            # configured or timestamp-less titles are still revisited.
            soonest = int(time.time()) + FALLBACK_RECHECK_SECONDS
        else:
            soonest += RELEASE_DELAY_SECONDS
        self._set_next_run(soonest)


refresh_status()
