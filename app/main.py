import asyncio
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
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
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

DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
FAVICON_PATH = Path(__file__).parent / "favicon.svg"

# Single source of truth: the repo-root VERSION file (copied next to the app in
# the image). Bump it with `make bump-patch|bump-minor|bump-major`.
try:
    APP_VERSION = (Path(__file__).parent / "VERSION").read_text().strip() or "dev"
except OSError:
    APP_VERSION = "dev"


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
        except Exception as e:
            print(f"[cleanup] error: {e}", flush=True)
        await asyncio.sleep(CLEANUP_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
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
        },
    )


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
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
    # CLI clients (curl with `Accept: text/plain`) get just the full URL back,
    # so a shell helper needs no JSON parser. Browsers get JSON as before.
    accept = request.headers.get("accept", "")
    if "text/plain" in accept and "text/html" not in accept:
        url = str(request.base_url).rstrip("/") + path
        return PlainTextResponse(url + "\n")
    return JSONResponse({"path": path, "filename": filename, "size": written})


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
