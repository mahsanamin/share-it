import asyncio
import os
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import yaml
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.yaml")
with open(CONFIG_PATH) as f:
    cfg = yaml.safe_load(f) or {}

DATA_DIR = Path(cfg.get("data_dir", "/data"))
MAX_AGE_DAYS = float(cfg.get("max_age_days", 2))
CLEANUP_INTERVAL_SEC = int(cfg.get("cleanup_interval_sec", 3600))
MAX_UPLOAD_MB = int(cfg.get("max_upload_mb", 1024))
TOKEN_BYTES = int(cfg.get("token_bytes", 16))
ALLOWED_EXTS = {e.lower().lstrip(".") for e in (cfg.get("allowed_extensions") or [])}

DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "max_upload_mb": MAX_UPLOAD_MB,
            "max_age_days": MAX_AGE_DAYS,
            "allowed_exts": sorted(ALLOWED_EXTS),
        },
    )


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    filename = Path(file.filename or "file").name or "file"
    ext = Path(filename).suffix.lower().lstrip(".")
    if ALLOWED_EXTS and ext not in ALLOWED_EXTS:
        allowed = ", ".join(sorted(ALLOWED_EXTS)) or "(none)"
        raise HTTPException(415, f"File type '.{ext or '?'}' not allowed. Allowed: {allowed}")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    folder = DATA_DIR / token
    folder.mkdir(parents=True, exist_ok=False)

    dest = folder / filename
    limit = MAX_UPLOAD_MB * 1024 * 1024
    written = 0

    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise HTTPException(413, f"File exceeds {MAX_UPLOAD_MB} MB")
                out.write(chunk)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    return JSONResponse({"path": f"/f/{token}", "filename": filename, "size": written})


@app.get("/f/{token}")
def download(token: str):
    if not TOKEN_RE.match(token):
        raise HTTPException(404)
    folder = DATA_DIR / token
    if not folder.is_dir():
        raise HTTPException(404)
    files = [p for p in folder.iterdir() if p.is_file()]
    if not files:
        raise HTTPException(404)
    f = files[0]
    disposition = f"inline; filename*=UTF-8''{quote(f.name)}"
    return FileResponse(f, headers={"Content-Disposition": disposition})
