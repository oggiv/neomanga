# NeoManga — Local CBZ Reader with MangaPlus Auto-Download

A small Python HTTP server for reading a local manga collection (CBZ files)
in the browser, with optional automatic downloading of new chapters from
MangaPlus for configured titles.

## Features

- Directory browsing of a `manga/` library with natural sorting
- In-browser CBZ reader with keyboard navigation and per-manga reading
  progress and continuing where you left off
- MangaPlus integration: associate a folder with a MangaPlus title ID and
  the server will download newly released chapters as CBZ files
- Background scheduler that re-checks at each title's next release time,
  plus a "Sync now" button on configured folder pages
- Optional Discord webhook notification when a chapter is downloaded

## Requirements

- Python 3
- `requests` and `mangaplus` packages (`pip install requests mangaplus`)

No browser automation or external schedulers are used.

## Running

Place manga folders containing `.cbz` files under `manga/`:

```
manga/
    Gantz/
        Chapter 19.cbz
```

Then start the server (default port 8000):

```
python server.py [port]
```

Open `http://127.0.0.1:8000/` in a browser.

## MangaPlus configuration

Create `manga.json` next to `server.py`, mapping each folder (relative to
`manga/`) to its MangaPlus title ID (the number in the title's
`mangaplus.shueisha.co.jp/titles/<id>` URL):

```json
{
    "Haunted Peak": {
        "title_id": 100728
    }
}
```

Folders without an entry work exactly as before. On startup the server
syncs every configured title once in the background, then re-checks at
each title's next release time. A manual check can be triggered with the
"Sync now" button shown on a configured folder's page or by requesting
`/sync`. Sync metadata (title name, next release, last check) is cached
in `mangaplus_cache.json`.

## Discord notifications

Set a Discord channel webhook URL so you are notified when a chapter
finishes downloading:

- Preferred: the `DISCORD_WEBHOOK_URL` environment variable
- Alternative: a `"discord_webhook_url"` key in `manga.json`

If neither is set, notifications are skipped. A failed notification is
logged and never affects downloaded files.
