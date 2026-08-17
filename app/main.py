import asyncio
import json
import os
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import qrcode
import yaml
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
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
    Response,
)
from fastapi.templating import Jinja2Templates

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

DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
FAVICON_PATH = Path(__file__).parent / "favicon.svg"

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


@app.get("/favicon.svg")
def favicon():
    return FileResponse(FAVICON_PATH, media_type="image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    return FileResponse(FAVICON_PATH, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "version": APP_VERSION,
            "max_upload_mb": MAX_UPLOAD_MB,
            "max_upload_mb_text": MAX_UPLOAD_MB_TEXT,
            "max_age_days": MAX_AGE_DAYS,
            "blocked_exts": sorted(BLOCKED_EXTS),
            "text_exts": sorted(TEXT_EXTS),
            "image_exts": sorted(IMAGE_EXTS),
            "pad_max_kb": PAD_MAX_KB,
        },
    )


@app.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    shared: str | None = Form(None),
):
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

    path = f"/f/{token}"
    is_shared = str(shared or "").lower() in {"1", "true", "on", "yes"}
    if is_shared:
        item = {"token": token, "filename": filename, "size": written, "at": time.time()}
        async with _shared_lock:
            items, _ = _prune_shared(_load_shared())
            items.insert(0, item)
            del items[SHARED_MAX_ITEMS:]
            _write_shared(items)
        await hub.broadcast({"type": "shared_add", "item": item})

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


@app.delete("/shared/{token}")
async def shared_remove(token: str):
    """Take an entry off the shared list. The file itself stays until swept."""
    if not TOKEN_RE.match(token):
        raise HTTPException(404)
    async with _shared_lock:
        items = _load_shared()
        kept = [i for i in items if i.get("token") != token]
        if len(kept) == len(items):
            raise HTTPException(404, "not on the shared list")
        _write_shared(kept)
    await hub.broadcast({"type": "shared_del", "token": token})
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
    if not TOKEN_RE.match(token):
        raise HTTPException(404)
    folder = DATA_DIR / token
    if not folder.is_dir():
        raise HTTPException(404)
    files = [p for p in folder.iterdir() if p.is_file()]
    if not files:
        raise HTTPException(404)
    f = files[0]
    # `?dl=1` forces a save dialog; default stays inline so previews/embeds work.
    disp = "attachment" if dl else "inline"
    disposition = f"{disp}; filename*=UTF-8''{quote(f.name)}"
    return FileResponse(f, headers={"Content-Disposition": disposition})
