from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote
from html import escape
import json, mimetypes, os, posixpath, sys, zipfile, threading

ROOT = Path(__file__).resolve().parent
MANGA_ROOT = (ROOT / 'manga').resolve()
PROGRESS_FILE = ROOT / 'progress.json'
IMAGE_EXTS = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png'}
progress_lock = threading.Lock()


def load_progress():
    try:
        with PROGRESS_FILE.open('r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

progress = load_progress()


def save_progress():
    tmp = PROGRESS_FILE.with_suffix('.json.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(progress, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, PROGRESS_FILE)


def relative_path(path):
    return path.resolve().relative_to(MANGA_ROOT)


def safe_dir(rel):
    # URL path -> normalized POSIX relative path, then resolve it under manga root.
    rel = rel.replace('\\', '/')
    candidate = (MANGA_ROOT / rel).resolve()
    try:
        candidate.relative_to(MANGA_ROOT)
    except ValueError:
        raise ValueError('outside manga root')
    return candidate


def cbz_files(folder):
    return sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == '.cbz'],
        key=lambda p: p.name.casefold(),
    )


def subdirs(folder):
    return sorted(
        [p for p in folder.iterdir() if p.is_dir()],
        key=lambda p: p.name.casefold(),
    )


def zip_images(cbz):
    with zipfile.ZipFile(cbz) as zf:
        items = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace('\\', '/')
            suffix = Path(name).suffix.lower()
            if suffix in IMAGE_EXTS:
                # Ignore unsafe/odd archive entries; image names are only used inside the ZIP.
                items.append((name, info))
        items.sort(key=lambda x: x[0].casefold())
        return [(name, IMAGE_EXTS[Path(name).suffix.lower()]) for name, _ in items]


def manga_key(cbz):
    return relative_path(cbz).as_posix()


def page_index(cbz, page):
    images = zip_images(cbz)
    if not images:
        raise ValueError('CBZ contains no supported images')
    return max(0, min(page, len(images) - 1)), images


def html_document(title, body):
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<script src="https://unpkg.com/htmx.org@2.0.6"></script>
<style>
html, body {{ margin:0; padding:0; font-family:sans-serif; }}
body {{ padding:2rem; background:#fff; color:#111; }}
a {{ color:inherit; text-decoration:none; }}
ul {{ list-style:none; margin:0; padding:0; }}
li {{ margin:.45rem 0; }}
.reader {{ min-height:100vh; box-sizing:border-box; background:#000; margin:-2rem; padding:1rem; position:relative; text-align:center; }}
.reader img {{ display:block; width:auto; height:auto; margin:0 auto; }}
.reader .prev {{ left:0; }}
.reader .next {{ right:0; }}
.reader .back {{ position:fixed; top:.75rem; left:.75rem; z-index:3; opacity:.8; color:#fff; background:#0008; border:0; padding:.35rem .5rem; cursor:pointer; }}
@media (max-width:600px) {{ body {{ padding:1rem; }} .reader {{ margin:-1rem; }} }}
</style>
</head>
<body>{body}</body>
</html>'''


def reader_html(cbz, current):
    key = quote(manga_key(cbz), safe='')
    idx, images = page_index(cbz, current)
    current_name, mime = images[idx]
    count = len(images)
    image_url = f'/image?cbz={key}&page={idx}'
    prev_action = f"document.getElementById('page').click()" if False else ''
    body = f'''
<div class="reader" tabindex="0" data-cbz="{escape(manga_key(cbz), quote=True)}" data-page="{idx}">
<img id="page" src="{image_url}" alt="Page {idx + 1} of {count}">
<div class="prev" style="position:fixed;left:0;top:0;bottom:0;width:50%;cursor:pointer" onclick="goPrev()"></div>
<div class="next" style="position:fixed;right:0;top:0;bottom:0;width:50%;cursor:pointer" onclick="goNext()"></div>
</div>
<script>
const cbz = {json.dumps(manga_key(cbz))};
let page = {idx};
const count = {count};
const img = document.getElementById('page');
const reader = document.querySelector('.reader');
const previousPagePreload = new Image();
const nextPagePreload = new Image();
let fitScale;

function imageUrl(n) {{
  return `/image?cbz=${{encodeURIComponent(cbz)}}&page=${{n}}`;
}}

function preloadAdjacentPages() {{
  if (page > 0) previousPagePreload.src = imageUrl(page - 1);
  else previousPagePreload.removeAttribute('src');

  if (page + 1 < count) nextPagePreload.src = imageUrl(page + 1);
  else nextPagePreload.removeAttribute('src');
}}

function fitPage() {{
  if (!img.naturalWidth || !img.naturalHeight) return;

  // Establish the reading scale once. Keeping it stable lets browser zoom
  // enlarge the page instead of having a viewport-based CSS rule refit it.
  if (fitScale === undefined) {{
    const style = getComputedStyle(reader);
    const availableWidth = reader.clientWidth
      - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const availableHeight = window.innerHeight
      - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    fitScale = Math.min(1, availableWidth / img.naturalWidth,
      availableHeight / img.naturalHeight);
  }}

  img.style.width = `${{img.naturalWidth * fitScale}}px`;
  img.style.height = 'auto';
}}

function pageLoaded() {{
  fitPage();
  preloadAdjacentPages();
}}

img.addEventListener('load', pageLoaded);
if (img.complete) pageLoaded();

function navigate(n) {{
  if (n < 0 || n >= count) return;
  page = n;
  img.src = imageUrl(n);
  img.alt = `Page ${{n + 1}} of ${{count}}`;
  history.replaceState(null, '', `/read?cbz=${{encodeURIComponent(cbz)}}&page=${{n}}`);
  const body = new URLSearchParams({{cbz, page:n}});
  if (navigator.sendBeacon) navigator.sendBeacon('/progress', body);
  else fetch('/progress', {{method:'POST', headers:{{'Content-Type':'application/x-www-form-urlencoded'}}, body}});
}}
function goPrev() {{ navigate(page - 1); }}
function goNext() {{ navigate(page + 1); }}
document.addEventListener('keydown', e => {{
  if (e.key === 'ArrowLeft') {{ e.preventDefault(); goPrev(); }}
  else if (e.key === 'ArrowRight') {{ e.preventDefault(); goNext(); }}
}});
</script>'''
    return html_document(cbz.name, body), idx


class Handler(BaseHTTPRequestHandler):
    server_version = 'MinimalCBZReader/1.0'

    def send_text(self, status, text, content_type='text/html; charset=utf-8'):
        data = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == '/':
                q = parse_qs(parsed.query)
                rel = q.get('dir', [''])[0]
                self.directory_page(rel)
            elif parsed.path == '/read':
                self.read_page(parsed.query)
            elif parsed.path == '/image':
                self.image_page(parsed.query)
            else:
                self.send_text(404, 'Not found')
        except ValueError as e:
            self.send_text(400, escape(str(e)))
        except FileNotFoundError:
            self.send_text(404, 'Not found')
        except zipfile.BadZipFile:
            self.send_text(400, 'Invalid CBZ file')
        except OSError:
            self.send_text(404, 'Not found')

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != '/progress':
            self.send_text(404, 'Not found')
            return
        length = int(self.headers.get('Content-Length', '0'))
        raw = self.rfile.read(length).decode('utf-8')
        q = parse_qs(raw)
        cbz_value = q.get('cbz', [None])[0]
        page_value = q.get('page', [None])[0]
        try:
            if cbz_value is None or page_value is None:
                raise ValueError('missing progress data')
            cbz = self.safe_cbz(cbz_value)
            page = int(page_value)
            idx, _ = page_index(cbz, page)
            with progress_lock:
                progress[manga_key(cbz)] = idx
                save_progress()
            self.send_response(204)
            self.end_headers()
        except (ValueError, FileNotFoundError, zipfile.BadZipFile, OSError) as e:
            self.send_text(400, escape(str(e)))

    def safe_cbz(self, key):
        key = unquote(key).replace('\\', '/')
        # Treat this as a relative path below ./manga; reject absolute and traversal forms.
        p = Path(key)
        if p.is_absolute() or any(part == '..' for part in p.parts):
            raise ValueError('invalid manga path')
        cbz = (MANGA_ROOT / p).resolve()
        try:
            cbz.relative_to(MANGA_ROOT)
        except ValueError:
            raise ValueError('invalid manga path')
        if not cbz.is_file() or cbz.suffix.lower() != '.cbz':
            raise FileNotFoundError(key)
        return cbz

    def directory_page(self, rel):
        folder = safe_dir(unquote(rel))
        if not folder.is_dir():
            raise FileNotFoundError(rel)
        dirs = subdirs(folder)
        files = cbz_files(folder)
        items = []
        if folder != MANGA_ROOT:
            parent = relative_path(folder.parent).as_posix() if folder.parent != MANGA_ROOT else ''
            href = '/' if not parent else '/?dir=' + quote(parent, safe='')
            items.append(f'<li><a href="{href}" hx-get="{href}" hx-select="#listing" hx-target="#listing" hx-push-url="true">..</a></li>')
        for d in dirs:
            r = relative_path(d).as_posix()
            items.append(f'<li><a href="/?dir={quote(r, safe="")}" hx-get="/?dir={quote(r, safe="")}" hx-select="#listing" hx-target="#listing" hx-push-url="true">{escape(d.name)}/</a></li>')
        for f in files:
            key = manga_key(f)
            with progress_lock:
                p = progress.get(key, 0)
            try:
                n = len(zip_images(f))
                suffix = f' <small>({min(p + 1, n)}/{n})</small>'
            except (OSError, zipfile.BadZipFile):
                suffix = ''
            items.append(f'<li><a href="/read?cbz={quote(key, safe="")}" target="_blank" rel="noopener">{escape(f.stem)}</a>{suffix}</li>')
        body = '<ul id="listing" hx-target="#listing" hx-push-url="true">' + ''.join(items) + '</ul>'
        self.send_text(200, html_document('Manga', body))

    def read_page(self, query):
        q = parse_qs(query)
        key = q.get('cbz', [None])[0]
        if key is None:
            raise ValueError('missing manga path')
        cbz = self.safe_cbz(key)
        with progress_lock:
            saved = int(progress.get(manga_key(cbz), 0))
        requested = q.get('page', [None])[0]
        current = saved if requested is None else int(requested)
        html, _ = reader_html(cbz, current)
        self.send_text(200, html)

    def image_page(self, query):
        q = parse_qs(query)
        key = q.get('cbz', [None])[0]
        page = q.get('page', [None])[0]
        if key is None or page is None:
            raise ValueError('missing image parameters')
        cbz = self.safe_cbz(key)
        idx, images = page_index(cbz, int(page))
        name, mime = images[idx]
        with zipfile.ZipFile(cbz) as zf:
            info = zf.getinfo(name)
            # Defend against duplicate names: getinfo returns the last matching name;
            # our list is based on archive entries, so explicitly read that name.
            data = zf.read(info)
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'private, max-age=31536000, immutable')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    if not MANGA_ROOT.exists():
        MANGA_ROOT.mkdir(parents=True)
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    except ValueError:
        print('Usage: python server.py [port]', file=sys.stderr)
        return 2
    if not 1 <= port <= 65535:
        print('Port must be between 1 and 65535', file=sys.stderr)
        return 2
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    print(f'Manga reader: http://127.0.0.1:{port}/')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
