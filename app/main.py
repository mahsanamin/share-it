import asyncio
import io
import json
import os
import re
import secrets
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode

import qrcode
import yaml
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile as StarletteUploadFile

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.yaml")
with open(CONFIG_PATH) as f:
    cfg = yaml.safe_load(f) or {}

DATA_DIR = Path(cfg.get("data_dir", "/data"))
MAX_AGE_DAYS = float(cfg.get("max_age_days", 2))
CLEANUP_INTERVAL_SEC = int(cfg.get("cleanup_interval_sec", 3600))
MAX_UPLOAD_MB = int(cfg.get("max_upload_mb", 1024))
MAX_UPLOAD_MB_TEXT = int(cfg.get("max_upload_mb_text", MAX_UPLOAD_MB))
TOKEN_BYTES = int(cfg.get("token_bytes", 16))
BLOCKED_EXTS = {e.lower().lstrip(".") for e in (cfg.get("blocked_extensions") or [])}
TEXT_EXTS = {e.lower().lstrip(".") for e in (cfg.get("text_extensions") or [])}
IMAGE_EXTS = {e.lower().lstrip(".") for e in (cfg.get("image_extensions") or [])}
PAD_MAX_KB = int(cfg.get("pad_max_kb", 128))
SHARED_MAX_ITEMS = int(cfg.get("shared_max_items", 500))

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex_color(value, default: str) -> str:
    """A `#rrggbb` color from config, or the default if it is anything else."""
    v = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", v):
        v = "#" + "".join(c * 2 for c in v[1:])
    if HEX_RE.match(v):
        return v.lower()
    if v:
        print(f"[config] app_color {value!r} is not #rrggbb, using {default}", flush=True)
    return default


# Identity of this instance. Several share-its on different domains install as
# separate apps; a name and a color per instance is how you tell them apart.
APP_NAME = str(cfg.get("app_name") or "share-it").strip()[:40] or "share-it"
APP_COLOR = _hex_color(cfg.get("app_color"), "#2563eb")

DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
BATCH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STATIC_DIR = Path(__file__).parent / "static"

# Bulk download. The tokens ride in the query string so a plain link can start
# the save, which makes the request line — not storage — the binding limit.
MAX_ZIP_FILES = 200
ZIP_CHUNK = 256 * 1024

# Deflate earns its CPU only on payloads that are not already compressed, so
# it is opt-in by extension: text, logs, code, and the uncompressed image and
# audio formats. Everything else — photos, video, PDFs, office files, archives,
# and anything unrecognised — is stored verbatim, which keeps a big zip bound
# by the network rather than by this box's CPU.
ZIP_DEFLATE_EXTS = {
    "txt", "md", "markdown", "rst", "log", "csv", "tsv",
    "json", "jsonl", "ndjson", "xml", "yaml", "yml", "toml", "ini",
    "cfg", "conf", "properties", "html", "htm", "css", "svg",
    "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue",
    "py", "rb", "go", "rs", "java", "kt", "swift", "c", "h",
    "cpp", "cc", "hpp", "cs", "php", "pl", "lua", "r",
    "sh", "bash", "zsh", "fish", "sql", "diff", "patch",
    "srt", "vtt", "ics", "tex", "bib",
    "bmp", "tif", "tiff", "wav", "aif", "aiff", "ppm", "pgm", "dib",
} | TEXT_EXTS

# State files live at the top level of DATA_DIR. The sweeper only walks
# directories (one per upload), so these are never swept away with the files.
SHARED_PATH = DATA_DIR / "_shared.json"
PAD_PATH = DATA_DIR / "_pad.txt"
PAD_MAX_BYTES = PAD_MAX_KB * 1024

# Single source of truth: the repo-root VERSION file (copied next to the app in
# the image). Bump it with `make bump-patch|bump-minor|bump-major`.
try:
    APP_VERSION = (Path(__file__).parent / "VERSION").read_text().strip() or "dev"
except OSError:
    APP_VERSION = "dev"


# ---------------------------------------------------------------------------
# Live state: the shared list and the live pad.
#
# Both are deliberately global and unauthenticated — same posture as the rest
# of share-it. Anyone who can reach the page can see the shared list and type
# in the pad. The state is small enough to keep in memory and mirror to disk,
# so there is still no database.
# ---------------------------------------------------------------------------


class Hub:
    """Fan-out for live updates to every browser on this instance."""

    def __init__(self):
        self.clients: dict[WebSocket, str] = {}

    def add(self, ws: WebSocket) -> str:
        cid = secrets.token_urlsafe(8)
        self.clients[ws] = cid
        return cid

    def remove(self, ws: WebSocket):
        self.clients.pop(ws, None)

    async def broadcast(self, msg: dict, skip: WebSocket | None = None):
        payload = json.dumps(msg)
        dead = []
        for ws in list(self.clients):
            if ws is skip:
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)


hub = Hub()

# Last-write-wins, with a revision so a client can tell a remote edit from the
# echo of its own. A shared clipboard has no meaningful merge semantics; the
# newest keystroke simply wins, which is what a clipboard does anyway.
pad = {"text": "", "rev": 0}
_pad_dirty = False

try:
    if PAD_PATH.exists():
        pad["text"] = PAD_PATH.read_text(encoding="utf-8", errors="replace")
except OSError as e:
    print(f"[pad] could not read {PAD_PATH}: {e}", flush=True)

_shared_lock = asyncio.Lock()


def _load_shared() -> list[dict]:
    try:
        items = json.loads(SHARED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict) and i.get("token")]


def _write_shared(items: list[dict]):
    tmp = SHARED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items), encoding="utf-8")
    tmp.replace(SHARED_PATH)


def _prune_shared(items: list[dict]) -> tuple[list[dict], list[str]]:
    """Drop entries whose upload has been swept. Returns (kept, gone_tokens)."""
    kept, gone = [], []
    for it in items:
        token = it.get("token", "")
        if TOKEN_RE.match(token) and (DATA_DIR / token).is_dir():
            kept.append(it)
        else:
            gone.append(token)
    return kept, gone


def _token_file(token: str) -> Path | None:
    """The single stored file behind a token, or None if it is gone."""
    if not TOKEN_RE.match(token or ""):
        return None
    folder = DATA_DIR / token
    if not folder.is_dir():
        return None
    files = sorted(p for p in folder.iterdir() if p.is_file())
    return files[0] if files else None


def _clean_batch(batch: str | None) -> str | None:
    """A batch id ties one upload's files together, so a group stays a group."""
    b = (batch or "").strip()
    return b if BATCH_RE.match(b) else None


async def _add_shared(new_items: list[dict]) -> list[dict]:
    """Put entries on the shared list, newest first, and announce them.

    Shared by both paths onto the list: the upload checkbox, and the Share
    button on a file that was uploaded earlier.
    """
    fresh: list[dict] = []
    async with _shared_lock:
        items, _ = _prune_shared(_load_shared())
        have = {i.get("token") for i in items}
        fresh = [i for i in new_items if i["token"] not in have]
        if fresh:
            items = fresh + items
            del items[SHARED_MAX_ITEMS:]
            _write_shared(items)
    for it in fresh:
        await hub.broadcast({"type": "shared_add", "item": it})
    return fresh


async def _drop_shared(tokens: list[str]) -> list[str]:
    """Take entries off the shared list. Says which ones were actually on it."""
    drop = set(tokens)
    async with _shared_lock:
        items = _load_shared()
        kept = [i for i in items if i.get("token") not in drop]
        removed = [i.get("token") for i in items if i.get("token") in drop]
        if removed:
            _write_shared(kept)
    for token in removed:
        await hub.broadcast({"type": "shared_del", "token": token})
    return removed


async def cleanup_loop():
    print(
        f"[cleanup] sweeper started: removing entries older than {MAX_AGE_DAYS} day(s) "
        f"every {CLEANUP_INTERVAL_SEC}s",
        flush=True,
    )
    while True:
        try:
            cutoff = time.time() - MAX_AGE_DAYS * 86400
            removed = 0
            kept = 0
            for entry in DATA_DIR.iterdir():
                if not entry.is_dir():
                    continue
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
                else:
                    kept += 1
            print(f"[cleanup] sweep done: removed={removed} kept={kept}", flush=True)
            if removed:
                # Entries in the shared list now point at swept files; drop them
                # and tell every open page so its list doesn't go stale.
                async with _shared_lock:
                    items, gone = _prune_shared(_load_shared())
                    if gone:
                        _write_shared(items)
                for token in gone:
                    await hub.broadcast({"type": "shared_del", "token": token})
        except Exception as e:
            print(f"[cleanup] error: {e}", flush=True)
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)


async def pad_flush_loop():
    """Mirror the live pad to disk so a restart doesn't lose the clipboard."""
    global _pad_dirty
    while True:
        await asyncio.sleep(2)
        if not _pad_dirty:
            continue
        try:
            tmp = PAD_PATH.with_suffix(".txt.tmp")
            tmp.write_text(pad["text"], encoding="utf-8")
            tmp.replace(PAD_PATH)
            _pad_dirty = False
        except OSError as e:
            print(f"[pad] flush failed: {e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(cleanup_loop()), asyncio.create_task(pad_flush_loop())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # Final flush so the last keystrokes survive a clean shutdown.
        if _pad_dirty:
            try:
                PAD_PATH.write_text(pad["text"], encoding="utf-8")
            except OSError:
                pass


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": APP_VERSION}


@app.get("/version")
def version():
    return {"version": APP_VERSION}


@app.get("/stats")
def stats():
    files = 0
    total = 0
    oldest = None
    for entry in DATA_DIR.iterdir():
        if not entry.is_dir():
            continue
        for f in entry.iterdir():
            if f.is_file():
                files += 1
                total += f.stat().st_size
        m = entry.stat().st_mtime
        oldest = m if oldest is None else min(oldest, m)
    oldest_expires = (oldest + MAX_AGE_DAYS * 86400) if oldest is not None else None
    return {"files": files, "bytes": total, "oldest_expires": oldest_expires}


def _qr_svg(data: str, box: int = 4, border: int = 2) -> str:
    """Render `data` as a self-contained SVG QR code (no PIL/lxml needed)."""
    qr = qrcode.QRCode(border=border, box_size=box)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    dim = n * box
    rects = []
    for r, row in enumerate(matrix):
        for c, cell in enumerate(row):
            if cell:
                rects.append(f'<rect x="{c * box}" y="{r * box}" width="{box}" height="{box}"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" height="{dim}" '
        f'viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">'
        f'<rect width="{dim}" height="{dim}" fill="#fff"/>'
        f'<g fill="#000">{"".join(rects)}</g></svg>'
    )


@app.get("/qr")
def qr(data: str):
    if len(data) > 2048:
        raise HTTPException(414, "data too long")
    svg = _qr_svg(data)
    return Response(
        svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# Installable app: manifest, service worker, and icons in this instance's color.
#
# The icons are drawn here rather than shipped as files, so changing
# `app_color` in config.yaml recolors the favicon, the app icon and the window
# bar together, with nothing to regenerate by hand.
# ---------------------------------------------------------------------------


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i:i + 2], 16) for i in (1, 3, 5))


def _mix(rgb, other, t: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * t) for a, b in zip(rgb, other))


def _hex(rgb) -> str:
    return "#" + "".join(f"{c:02x}" for c in rgb)


BASE_RGB = _rgb(APP_COLOR)
# The icon runs from a lighter tint at the top left to a darker shade at the
# bottom right, the same shape the original blue icon had.
ICON_LIGHT = _hex(_mix(BASE_RGB, (255, 255, 255), 0.4))
ICON_DARK = _hex(_mix(BASE_RGB, (0, 0, 0), 0.2))


def _ink_for(rgb) -> str:
    """Black or white, whichever reads better on top of `rgb`."""
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])
    return "#000000" if lum > 0.4 else "#ffffff"


APP_INK = _ink_for(BASE_RGB)
ICON_INK = _ink_for(_rgb(ICON_DARK))
ICON_SIZES = (180, 192, 512)

# The share glyph in a 64-unit box: two strokes and three dots.
_GLYPH = """
    <line x1="20" y1="32" x2="44" y2="18"/>
    <line x1="20" y1="32" x2="44" y2="46"/>
    <circle cx="20" cy="32" r="9"/>
    <circle cx="44" cy="18" r="9"/>
    <circle cx="44" cy="46" r="9"/>"""


def _icon_svg(maskable: bool = False) -> str:
    # A maskable icon fills the whole square (the launcher cuts its own shape)
    # and keeps the glyph inside the middle 80%, so no launcher crops it.
    rect = '<rect width="64" height="64" fill="url(#bg)"/>' if maskable else \
        '<rect width="64" height="64" rx="13" fill="url(#bg)"/>'
    glyph_tf = ' transform="translate(32 32) scale(0.68) translate(-32 -32)"' if maskable else ""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" '
        f'aria-label="{APP_NAME}">'
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0%" stop-color="{ICON_LIGHT}"/>'
        f'<stop offset="100%" stop-color="{ICON_DARK}"/>'
        f'</linearGradient></defs>{rect}'
        f'<g{glyph_tf} stroke="{ICON_INK}" stroke-width="6" stroke-linecap="round" '
        f'fill="{ICON_INK}">{_GLYPH}</g></svg>'
    )


@lru_cache(maxsize=None)
def _icon_png(size: int, maskable: bool) -> bytes:
    """The same icon as `_icon_svg`, as a PNG. Android wants PNG app icons."""
    from PIL import Image, ImageDraw

    ss = 4  # draw big and scale down, which smooths the edges
    big = size * ss
    unit = big / 64

    # Diagonal gradient: computed small, then scaled up, which stays smooth.
    light, dark = _rgb(ICON_LIGHT), _rgb(ICON_DARK)
    grad = Image.new("RGB", (64, 64))
    grad.putdata([_mix(light, dark, (x + y) / 126) for y in range(64) for x in range(64)])
    grad = grad.resize((big, big), Image.BICUBIC)

    mask = Image.new("L", (big, big), 0)
    if maskable:
        mask.paste(255, (0, 0, big, big))
    else:
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=13 * unit, fill=255)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    scale = 0.68 if maskable else 1.0

    def pt(x, y):
        return ((32 + (x - 32) * scale) * unit, (32 + (y - 32) * scale) * unit)

    ink = _rgb(ICON_INK)
    draw = ImageDraw.Draw(img)
    width = round(6 * scale * unit)
    for end in ((44, 18), (44, 46)):
        draw.line([pt(20, 32), pt(*end)], fill=ink, width=width)
    r = 12 * scale * unit  # radius 9 plus half the 6-unit stroke
    for cx, cy in ((20, 32), (44, 18), (44, 46)):
        x, y = pt(cx, cy)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=ink)

    img = img.resize((size, size), Image.LANCZOS)
    if maskable or size == 180:
        # Apple touch icons and maskable icons should be opaque.
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


ICON_CACHE = {"Cache-Control": "public, max-age=86400"}


@app.get("/favicon.svg")
def favicon():
    return Response(_icon_svg(), media_type="image/svg+xml", headers=ICON_CACHE)


@app.get("/favicon.ico")
def favicon_ico():
    return Response(_icon_svg(), media_type="image/svg+xml", headers=ICON_CACHE)


@app.get("/icons/{kind}-{size}.png")
def icon_png(kind: str, size: int):
    if kind not in {"icon", "maskable"} or size not in ICON_SIZES:
        raise HTTPException(404)
    return Response(_icon_png(size, kind == "maskable"), media_type="image/png", headers=ICON_CACHE)


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    return Response(_icon_png(180, True), media_type="image/png", headers=ICON_CACHE)


@app.get("/manifest.webmanifest")
def manifest():
    """Built per instance, so each domain installs under its own name and color.

    The browser keys an installed app to its origin (`id` "/" resolves against
    the domain), so two share-its on two domains are always two separate apps.
    """
    icons = [
        {"src": f"/icons/{kind}-{size}.png", "sizes": f"{size}x{size}",
         "type": "image/png", "purpose": "maskable" if kind == "maskable" else "any"}
        for kind in ("icon", "maskable") for size in (192, 512)
    ]
    small = [{"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"}]
    body = {
        "id": "/",
        "name": APP_NAME,
        "short_name": APP_NAME,
        "description": "Drop a file, get a link. Plus a live clipboard shared across your devices.",
        "start_url": "/?source=app",
        "scope": "/",
        "display": "standalone",
        "background_color": APP_COLOR,
        "theme_color": APP_COLOR,
        # A launch, a shortcut, or a share reuses the open window, not a new one.
        "launch_handler": {"client_mode": "navigate-existing"},
        "icons": icons,
        "shortcuts": [
            {"name": "Upload files", "url": "/?tab=files", "icons": small},
            {"name": "Share text", "url": "/?tab=text", "icons": small},
            {"name": "Live clipboard", "url": "/?tab=live", "icons": small},
        ],
        # Android lists the installed app in its Share menu. See static/sw.js.
        "share_target": {
            "action": "/share-target",
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {
                "title": "title",
                "text": "text",
                "url": "url",
                "files": [{"name": "files", "accept": ["*/*"]}],
            },
        },
    }
    return JSONResponse(body, media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole site. `no-cache` lets
    # a new version of the worker reach installed apps on their next launch.
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.post("/share-target")
async def share_target(request: Request):
    """Fallback for a share that the service worker did not catch.

    Normally the worker takes the POST and the page uploads the files itself.
    If the worker is missing (first launch, or it was cleared), the server
    saves the files directly and the page adds them to its history.
    """
    form = await request.form()
    received = []
    for f in form.getlist("files"):
        if not isinstance(f, StarletteUploadFile) or not f.filename:
            continue
        try:
            received.append(await _save_upload(f))
        except HTTPException as e:
            print(f"[share-target] skipped {f.filename}: {e.detail}", flush=True)
    text = "\n".join(
        str(form.get(k) or "").strip() for k in ("title", "text", "url")
        if str(form.get(k) or "").strip()
    )
    params = {"share-target": "server"}
    if received:
        params["received"] = json.dumps(received, separators=(",", ":"))
    if text:
        params["text"] = text[:4000]
    return RedirectResponse("/?" + urlencode(params), status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "version": APP_VERSION,
            "app_name": APP_NAME,
            "app_color": APP_COLOR,
            "app_ink": APP_INK,
            "max_upload_mb": MAX_UPLOAD_MB,
            "max_upload_mb_text": MAX_UPLOAD_MB_TEXT,
            "max_age_days": MAX_AGE_DAYS,
            "blocked_exts": sorted(BLOCKED_EXTS),
            "text_exts": sorted(TEXT_EXTS),
            "image_exts": sorted(IMAGE_EXTS),
            "pad_max_kb": PAD_MAX_KB,
        },
    )


async def _save_upload(file: UploadFile) -> dict:
    """Store one uploaded file under a new token. Raises HTTPException on refusal."""
    filename = Path(file.filename or "file").name or "file"
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext in BLOCKED_EXTS:
        raise HTTPException(415, f"File type '.{ext}' is not allowed (executables and installers are blocked).")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    folder = DATA_DIR / token
    folder.mkdir(parents=True, exist_ok=False)

    dest = folder / filename
    limit_mb = MAX_UPLOAD_MB_TEXT if ext in TEXT_EXTS else MAX_UPLOAD_MB
    limit = limit_mb * 1024 * 1024
    written = 0

    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f"File exceeds {limit_mb} MB")
                out.write(chunk)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    return {"token": token, "path": f"/f/{token}", "filename": filename, "size": written}


@app.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    shared: str | None = Form(None),
    batch: str | None = Form(None),
):
    saved = await _save_upload(file)
    token, path, filename, written = saved["token"], saved["path"], saved["filename"], saved["size"]
    is_shared = str(shared or "").lower() in {"1", "true", "on", "yes"}
    if is_shared:
        item = {"token": token, "filename": filename, "size": written, "at": time.time()}
        # The browser sends one request per file but stamps them all with the
        # same batch, so a multi-file upload lands as one group on the list.
        group = _clean_batch(batch)
        if group:
            item["batch"] = group
        await _add_shared([item])

    # CLI clients (curl with `Accept: text/plain`) get just the full URL back,
    # so a shell helper needs no JSON parser. Browsers get JSON as before.
    accept = request.headers.get("accept", "")
    if "text/plain" in accept and "text/html" not in accept:
        url = str(request.base_url).rstrip("/") + path
        return PlainTextResponse(url + "\n")
    return JSONResponse(
        {"path": path, "filename": filename, "size": written, "shared": is_shared}
    )


@app.get("/shared")
async def shared_list():
    """Everything ticked as shared — visible to anyone who opens the page."""
    async with _shared_lock:
        items, gone = _prune_shared(_load_shared())
        if gone:
            _write_shared(items)
    return {"items": items}


@app.post("/shared")
async def shared_put(request: Request):
    """Put files that are ALREADY uploaded onto the shared list.

    Body: `{"tokens": ["a", "b"], "batch": "<id>"}`, or `{"token": "a"}` for
    one. This is what the Share button on an existing card calls, so anything
    in your history can join the list everyone on this page sees — you no
    longer have to have ticked the box before uploading.
    """
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")

    raw = body.get("tokens")
    if raw is None:
        raw = [body["token"]] if body.get("token") else []
    if not isinstance(raw, list):
        raise HTTPException(400, "'tokens' must be a list")
    tokens = [t for t in dict.fromkeys(raw) if isinstance(t, str)][:SHARED_MAX_ITEMS]
    if not tokens:
        raise HTTPException(400, "no tokens given")

    # Sharing several at once makes them a group, exactly like an upload batch.
    group = _clean_batch(body.get("batch"))
    if not group and len(tokens) > 1:
        group = secrets.token_urlsafe(6)

    now = time.time()
    new_items, missing = [], []
    for i, token in enumerate(tokens):
        f = _token_file(token)
        if f is None:
            missing.append(token)
            continue
        # A hair apart so a group keeps the order they were picked in.
        item = {"token": token, "filename": f.name, "size": f.stat().st_size,
                "at": now + i * 0.001}
        if group:
            item["batch"] = group
        new_items.append(item)

    if not new_items:
        raise HTTPException(404, "none of those files are still here")
    added = await _add_shared(new_items)
    return {"added": added, "missing": missing, "batch": group}


@app.delete("/shared")
async def shared_remove_many(request: Request):
    """Take several entries off the shared list at once (a whole group).

    Body: `{"tokens": ["a", "b"]}`. The files themselves stay until swept —
    use `DELETE /f/{token}` to actually delete them.
    """
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("tokens"), list):
        raise HTTPException(400, "expected {\"tokens\": [...]}")
    tokens = [t for t in body["tokens"] if isinstance(t, str) and TOKEN_RE.match(t)]
    if not tokens:
        raise HTTPException(400, "no tokens given")
    removed = await _drop_shared(tokens)
    return {"removed": removed}


@app.delete("/shared/{token}")
async def shared_remove(token: str):
    """Take an entry off the shared list. The file itself stays until swept."""
    if not TOKEN_RE.match(token):
        raise HTTPException(404)
    if not await _drop_shared([token]):
        raise HTTPException(404, "not on the shared list")
    return {"ok": True}


@app.get("/pad")
def pad_get(request: Request):
    """The live pad's current contents.

    `Accept: text/plain` returns the raw text, so the shell can read what
    someone typed in a browser: `curl -s http://host:3050/pad`.
    """
    accept = request.headers.get("accept", "")
    if "text/plain" in accept and "text/html" not in accept:
        return PlainTextResponse(pad["text"])
    return {"text": pad["text"], "rev": pad["rev"]}


@app.post("/pad")
async def pad_set(request: Request):
    """Replace the live pad from a raw request body (the shell-side writer).

    `some_command | curl -sf --data-binary @- http://host:3050/pad`
    """
    global _pad_dirty
    # Refuse on the declared length before buffering the body into memory.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > PAD_MAX_BYTES:
        raise HTTPException(413, f"Pad text exceeds {PAD_MAX_KB} KB")
    body = await request.body()
    if len(body) > PAD_MAX_BYTES:
        raise HTTPException(413, f"Pad text exceeds {PAD_MAX_KB} KB")
    pad["text"] = body.decode("utf-8", errors="replace")
    pad["rev"] += 1
    _pad_dirty = True
    await hub.broadcast({"type": "pad", "text": pad["text"], "rev": pad["rev"], "origin": "http"})
    return {"ok": True, "rev": pad["rev"]}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """One socket carries both live features: the pad and the shared list."""
    global _pad_dirty
    await websocket.accept()
    cid = hub.add(websocket)
    async with _shared_lock:
        items, _ = _prune_shared(_load_shared())
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "init",
                    "you": cid,
                    "pad": pad,
                    "shared": items,
                    "clients": len(hub.clients),
                    "pad_max_kb": PAD_MAX_KB,
                }
            )
        )
        await hub.broadcast({"type": "presence", "clients": len(hub.clients)}, skip=websocket)
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") != "pad":
                continue
            text = msg.get("text")
            if not isinstance(text, str):
                continue
            if len(text.encode("utf-8")) > PAD_MAX_BYTES:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": f"Pad limit is {PAD_MAX_KB} KB"})
                )
                continue
            pad["text"] = text
            pad["rev"] += 1
            _pad_dirty = True
            await hub.broadcast(
                {"type": "pad", "text": text, "rev": pad["rev"], "origin": cid}
            )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[ws] {cid} dropped: {e}", flush=True)
    finally:
        hub.remove(websocket)
        await hub.broadcast({"type": "presence", "clients": len(hub.clients)})


@app.get("/f/{token}")
def download(token: str, dl: bool = False):
    f = _token_file(token)
    if f is None:
        raise HTTPException(404)
    # `?dl=1` forces a save dialog; default stays inline so previews/embeds work.
    disp = "attachment" if dl else "inline"
    disposition = f"{disp}; filename*=UTF-8''{quote(f.name)}"
    return FileResponse(f, headers={"Content-Disposition": disposition})


@app.delete("/f/{token}")
async def delete_file(token: str):
    """Delete an upload for real, now, instead of waiting for the sweeper.

    This is what "Delete" on a card or a group calls. It also takes the entry
    off the shared list, so every open page stops offering a dead link.
    """
    if not TOKEN_RE.match(token):
        raise HTTPException(404)
    folder = DATA_DIR / token
    if not folder.is_dir():
        raise HTTPException(404)
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():
        raise HTTPException(500, "could not delete that file")
    await _drop_shared([token])
    return {"ok": True, "token": token}


# ---------------------------------------------------------------------------
# Bulk download: several uploads, one zip, nothing staged on disk.
# ---------------------------------------------------------------------------


class _ZipSink:
    """A write-only sink zipfile can build into while we stream the result.

    It has no seek/tell, which zipfile detects and answers by writing data
    descriptors instead of backfilling headers. That is what lets a multi-GB
    selection stream straight out of a small process.
    """

    def __init__(self):
        self.buf = bytearray()

    def write(self, data) -> int:
        self.buf += data
        return len(data)

    def flush(self):
        pass

    def drain(self) -> bytes:
        chunk = bytes(self.buf)
        del self.buf[:]
        return chunk


def _unique_arcname(name: str, seen: set[str]) -> str:
    """Two uploads can share a filename; inside one archive they cannot."""
    if name not in seen:
        seen.add(name)
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while f"{stem} ({n}){suffix}" in seen:
        n += 1
    out = f"{stem} ({n}){suffix}"
    seen.add(out)
    return out


def _clean_zip_name(name: str | None) -> str | None:
    """Reduce a caller-supplied archive name to something safe for a header."""
    base = Path(str(name or "")).name
    base = re.sub(r"\.zip$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[^A-Za-z0-9 ._-]+", "", base).strip(" .")
    return base[:60] or None


def _zip_stream(entries: list[tuple[str, Path]]):
    """Yield the archive as it is built, a chunk at a time."""
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", allowZip64=True) as zf:
        for arcname, path in entries:
            # Opened before the archive entry is: a file swept out from under
            # us gets skipped, rather than wedging the half-written entry.
            try:
                stat = path.stat()
                src = path.open("rb")
            except OSError as e:
                print(f"[zip] skipped {arcname}: {e}", flush=True)
                continue
            info = zipfile.ZipInfo(
                arcname, date_time=time.localtime(stat.st_mtime)[:6]
            )
            info.compress_type = (
                zipfile.ZIP_DEFLATED
                if path.suffix.lower().lstrip(".") in ZIP_DEFLATE_EXTS
                else zipfile.ZIP_STORED
            )
            info.external_attr = 0o644 << 16
            with src, zf.open(info, "w") as dest:
                while chunk := src.read(ZIP_CHUNK):
                    dest.write(chunk)
                    if len(sink.buf) >= ZIP_CHUNK:
                        yield sink.drain()
            if sink.buf:
                yield sink.drain()
    if sink.buf:
        yield sink.drain()


@app.get("/zip")
def zip_download(
    t: list[str] = Query(default=[]),
    name: str | None = None,
):
    """Several uploads as one download: `/zip?t=<token>&t=<token>`.

    Backs both "Download all" on a group and "Download selected". The total
    size is unknown until the last byte, so this streams without a
    Content-Length rather than buffering the archive to size it.
    """
    tokens = [x for x in dict.fromkeys(t) if TOKEN_RE.match(x)]
    if not tokens:
        raise HTTPException(400, "no file tokens given")
    if len(tokens) > MAX_ZIP_FILES:
        raise HTTPException(413, f"at most {MAX_ZIP_FILES} files per zip")

    seen: set[str] = set()
    entries: list[tuple[str, Path]] = []
    for token in tokens:
        f = _token_file(token)
        if f is not None:
            entries.append((_unique_arcname(f.name, seen), f))
    if not entries:
        raise HTTPException(404, "none of those files are still here")

    base = _clean_zip_name(name) or (
        f"share-it-{len(entries)}-files-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    filename = f"{base}.zip"
    return StreamingResponse(
        _zip_stream(entries),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
            "X-Zip-Files": str(len(entries)),
        },
    )
