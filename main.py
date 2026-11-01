import os, re, secrets, time, asyncio
from urllib.parse import quote
from typing import List, Optional
from collections import defaultdict
from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pdf_tools import router as pdf_router
from database import check_db_connection
from auth import router as auth_router
from auth import get_current_user, is_super_admin
from admin import router as admin_router, init_admin
from usage import UsageMiddleware, purge_usage_loop
from templating import templates, STATIC_DIR
from fastapi import Depends

app = FastAPI()
app.include_router(pdf_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.add_middleware(UsageMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

STORAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)

REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

MAX_FILE_SIZE = 500 * 1024 * 1024   # 500 MB per file
RATE_LIMIT = 5                       # maks 5 upload request
RATE_WINDOW = 60                     # per 60 detik per IP
MAX_FILES_PER_BATCH = 20             # maks file per batch upload
CLEANUP_INTERVAL = 30                # detik, jeda scan cleanup

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
MAX_SCREENSHOT_SIZE = 10 * 1024 * 1024  # 10 MB
REPORT_RATE_LIMIT = 5
REPORT_RATE_WINDOW = 300             # 5 menit
REPORT_MAX_AGE = 30 * 24 * 60 * 60   # laporan otomatis dihapus setelah 30 hari
REPORT_CLEANUP_INTERVAL = 3600       # cek tiap 1 jam

FILES = {}
BATCHES = {}
REPORTS = []  # list of dict: id, description, screenshot_path, screenshot_name, timestamp
init_admin(FILES)
upload_log = defaultdict(list)   # ip -> [timestamp upload]
report_log = defaultdict(list)   # ip -> [timestamp laporan]


def sanitize_filename(name: str) -> str:
    name = os.path.basename(name)
    name = re.sub(r"[^\w.\-() ]", "_", name)
    return name[:255] or "file"


def check_rate_limit(ip: str):
    now = time.time()
    upload_log[ip] = [t for t in upload_log[ip] if now - t < RATE_WINDOW]
    if len(upload_log[ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Terlalu banyak upload, coba lagi sebentar.")
    upload_log[ip].append(now)


def check_report_rate_limit(ip: str):
    now = time.time()
    report_log[ip] = [t for t in report_log[ip] if now - t < REPORT_RATE_WINDOW]
    if len(report_log[ip]) >= REPORT_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Terlalu banyak laporan, coba lagi nanti.")
    report_log[ip].append(now)


def parse_expiry(expiry_minutes: str) -> Optional[float]:
    try:
        minutes = int(expiry_minutes)
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return None
    return time.time() + minutes * 60


def fmt_remaining(minutes: int) -> str:
    """Sisa waktu yang enak dibaca: '45 menit', '2 jam', '4 jam 57 menit'."""
    if minutes < 60:
        return f"{minutes} menit"
    jam, menit = divmod(minutes, 60)
    return f"{jam} jam" if menit == 0 else f"{jam} jam {menit} menit"


def is_expired(entry: dict) -> bool:
    return entry.get("expires_at") is not None and time.time() > entry["expires_at"]


async def save_upload(file: UploadFile, expires_at: Optional[float], unlimited: bool = False, owner: Optional[str] = None, owner_super: bool = False) -> dict:
    filename = sanitize_filename(file.filename)
    token = secrets.token_urlsafe(16)
    path = os.path.join(STORAGE_DIR, token)

    size = 0
    with open(path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if not unlimited and size > MAX_FILE_SIZE:
                f.close()
                os.remove(path)
                raise HTTPException(status_code=413, detail=f"{filename} melebihi batas {MAX_FILE_SIZE // (1024*1024)}MB")
            f.write(chunk)

    FILES[token] = {"path": path, "filename": filename, "expires_at": expires_at, "owner": owner, "owner_super": owner_super}
    return {"token": token, "filename": filename}


async def cleanup_expired_loop():
    while True:
        expired_tokens = [t for t, e in list(FILES.items()) if is_expired(e)]
        for t in expired_tokens:
            entry = FILES.pop(t, None)
            if entry and os.path.exists(entry["path"]):
                os.remove(entry["path"])
        await asyncio.sleep(CLEANUP_INTERVAL)


def prune_old_reports():
    now = time.time()
    keep = []
    for r in REPORTS:
        if now - r["timestamp"] > REPORT_MAX_AGE:
            if r["screenshot_path"] and os.path.exists(r["screenshot_path"]):
                os.remove(r["screenshot_path"])
        else:
            keep.append(r)
    REPORTS[:] = keep


async def cleanup_old_reports_loop():
    while True:
        prune_old_reports()
        await asyncio.sleep(REPORT_CLEANUP_INTERVAL)


@app.on_event("startup")
async def start_cleanup():
    asyncio.create_task(cleanup_expired_loop())
    asyncio.create_task(cleanup_old_reports_loop())
    asyncio.create_task(purge_usage_loop())
    if check_db_connection():
        print("[startup] Database connection: OK")
    else:
        print("[startup] Database connection: FAILED (cek DATABASE_URL di Environment)")


@app.get("/db-health")
async def db_health():
    ok = check_db_connection()
    return {"database_connected": ok}


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), expiry_minutes: str = Form("0"), user: dict = Depends(get_current_user)):
    unlimited = is_super_admin(user)
    if not unlimited:
        check_rate_limit(request.client.host)
    expires_at = parse_expiry(expiry_minutes)
    result = await save_upload(file, expires_at, unlimited, user["email"], unlimited)
    return {"token": result["token"]}


@app.post("/upload-batch")
async def upload_batch(request: Request, files: List[UploadFile] = File(...), expiry_minutes: str = Form("0"), user: dict = Depends(get_current_user)):
    unlimited = is_super_admin(user)
    if not unlimited:
        check_rate_limit(request.client.host)

    if not unlimited and len(files) > MAX_FILES_PER_BATCH:
        raise HTTPException(status_code=400, detail=f"Maks {MAX_FILES_PER_BATCH} file per batch")

    expires_at = parse_expiry(expiry_minutes)
    request.state.usage_files = len(files)
    batch_token = secrets.token_urlsafe(16)
    batch_items = [await save_upload(file, expires_at, unlimited, user["email"], unlimited) for file in files]
    BATCHES[batch_token] = batch_items
    return {"batch_token": batch_token}


@app.post("/report-bug")
async def report_bug(request: Request, description: str = Form(...), screenshot: UploadFile = File(None)):
    check_report_rate_limit(request.client.host)

    description = description.strip()[:1000]
    if not description:
        raise HTTPException(status_code=400, detail="Deskripsi tidak boleh kosong.")

    report_id = secrets.token_urlsafe(8)
    screenshot_path = None
    screenshot_name = None

    if screenshot and screenshot.filename:
        if not (screenshot.content_type or "").startswith("image/"):
            raise HTTPException(status_code=400, detail="File harus berupa gambar.")
        screenshot_name = sanitize_filename(screenshot.filename)
        screenshot_path = os.path.join(REPORTS_DIR, f"{report_id}_{screenshot_name}")
        size = 0
        with open(screenshot_path, "wb") as f:
            while chunk := await screenshot.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_SCREENSHOT_SIZE:
                    f.close()
                    os.remove(screenshot_path)
                    raise HTTPException(status_code=413, detail="Screenshot melebihi batas 10MB")
                f.write(chunk)

    REPORTS.append({
        "id": report_id,
        "description": description,
        "screenshot_path": screenshot_path,
        "screenshot_name": screenshot_name,
        "timestamp": time.time(),
    })
    return {"ok": True}


@app.get("/admin/reports", response_class=HTMLResponse)
async def admin_reports(request: Request, key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Akses ditolak.")

    reports = [
        {
            "ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["timestamp"])),
            "description": r["description"],
            "img_url": f"/admin/reports/{r['id']}/screenshot?key={key}" if r["screenshot_path"] else "",
        }
        for r in reversed(REPORTS)
    ]
    return templates.TemplateResponse(request, "admin_reports.html", {"reports": reports, "total": len(REPORTS)})


@app.get("/admin/reports/{report_id}/screenshot")
async def admin_report_screenshot(report_id: str, key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Akses ditolak.")
    report = next((r for r in REPORTS if r["id"] == report_id), None)
    if not report or not report["screenshot_path"] or not os.path.exists(report["screenshot_path"]):
        raise HTTPException(status_code=404, detail="Screenshot tidak ditemukan.")
    return FileResponse(report["screenshot_path"])


@app.get("/d/{token}", response_class=HTMLResponse)
async def download_page(request: Request, token: str):
    entry = FILES.get(token)
    if not entry or not os.path.exists(entry["path"]) or is_expired(entry):
        return templates.TemplateResponse(request, "message.html", {"title": "Link tidak berlaku", "message": "Link ini sudah tidak bisa dipakai. Mungkin filenya sudah diunduh (tiap link hanya berlaku sekali) atau sudah kedaluwarsa. Minta pengirim membagikan link baru."}, status_code=404)

    remaining_min = None
    if entry.get("expires_at"):
        remaining_min = max(0, int((entry["expires_at"] - time.time()) / 60))

    return templates.TemplateResponse(request, "download.html", {
        "remaining_text": fmt_remaining(remaining_min) if remaining_min is not None else None,
        "filename": entry["filename"],
        "download_url": f"/d/{token}/{quote(entry['filename'])}",
        "remaining_min": remaining_min,
    })


@app.get("/d/{token}/{filename_hint}")
async def download_file(request: Request, token: str, filename_hint: str):
    # filename_hint (bagian akhir URL) tidak dipakai buat lookup (token yang jadi kunci
    # sebenarnya) -- ini cuma supaya browser (khususnya Safari iOS) yang kadang ambil nama
    # file dari URL, bukan dari header, tetap dapat nama yang benar saat file di-share.
    entry = FILES.get(token)
    if not entry or not os.path.exists(entry["path"]) or is_expired(entry):
        FILES.pop(token, None)
        return templates.TemplateResponse(request, "message.html", {"title": "Link tidak berlaku", "message": "Link ini sudah tidak bisa dipakai. Mungkin filenya sudah diunduh (tiap link hanya berlaku sekali) atau sudah kedaluwarsa. Minta pengirim membagikan link baru."}, status_code=404)
    FILES.pop(token, None)
    return FileResponse(
        entry["path"],
        filename=entry["filename"],
        background=BackgroundTask(os.remove, entry["path"]),
    )


@app.get("/b/{batch_token}", response_class=HTMLResponse)
async def batch_page(request: Request, batch_token: str):
    items = BATCHES.get(batch_token)
    if not items:
        return templates.TemplateResponse(request, "message.html", {"title": "Link tidak ditemukan", "message": "Link kumpulan file ini tidak ditemukan atau sudah kedaluwarsa. Minta pengirim membagikan link baru."}, status_code=404)

    rows = []
    for item in items:
        entry = FILES.get(item["token"])
        available = bool(entry and not is_expired(entry))
        remaining_min = None
        if available and entry.get("expires_at"):
            remaining_min = max(0, int((entry["expires_at"] - time.time()) / 60))
        rows.append({
            "filename": item["filename"],
            "available": available,
            "remaining_text": fmt_remaining(remaining_min) if remaining_min is not None else None,
            "url": f"/d/{item['token']}/{quote(item['filename'])}",
        })
    return templates.TemplateResponse(request, "batch.html", {"items": rows})


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html")
