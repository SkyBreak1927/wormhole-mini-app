import os, re, secrets, time, asyncio
from typing import List, Optional
from collections import defaultdict
from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Form
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from pdf_tools import router as pdf_router
from database import check_db_connection
from auth import router as auth_router

app = FastAPI()
app.include_router(pdf_router)
app.include_router(auth_router)

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


def is_expired(entry: dict) -> bool:
    return entry.get("expires_at") is not None and time.time() > entry["expires_at"]


async def save_upload(file: UploadFile, expires_at: Optional[float]) -> dict:
    filename = sanitize_filename(file.filename)
    token = secrets.token_urlsafe(16)
    path = os.path.join(STORAGE_DIR, token)

    size = 0
    with open(path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                f.close()
                os.remove(path)
                raise HTTPException(status_code=413, detail=f"{filename} melebihi batas {MAX_FILE_SIZE // (1024*1024)}MB")
            f.write(chunk)

    FILES[token] = {"path": path, "filename": filename, "expires_at": expires_at}
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
    if check_db_connection():
        print("[startup] Database connection: OK")
    else:
        print("[startup] Database connection: FAILED (cek DATABASE_URL di Environment)")


@app.get("/db-health")
async def db_health():
    ok = check_db_connection()
    return {"database_connected": ok}


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), expiry_minutes: str = Form("0")):
    check_rate_limit(request.client.host)
    expires_at = parse_expiry(expiry_minutes)
    result = await save_upload(file, expires_at)
    return {"token": result["token"]}


@app.post("/upload-batch")
async def upload_batch(request: Request, files: List[UploadFile] = File(...), expiry_minutes: str = Form("0")):
    check_rate_limit(request.client.host)

    if len(files) > MAX_FILES_PER_BATCH:
        raise HTTPException(status_code=400, detail=f"Maks {MAX_FILES_PER_BATCH} file per batch")

    expires_at = parse_expiry(expiry_minutes)
    batch_token = secrets.token_urlsafe(16)
    batch_items = [await save_upload(file, expires_at) for file in files]
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
async def admin_reports(key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Akses ditolak.")

    rows = ""
    for r in reversed(REPORTS):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["timestamp"]))
        img_html = ""
        if r["screenshot_path"]:
            img_url = f"/admin/reports/{r['id']}/screenshot?key={key}"
            img_html = f'<a href="{img_url}" target="_blank"><img src="{img_url}" style="max-width:220px;border-radius:6px;margin-top:8px;display:block"></a>'
        rows += f"""
        <div style="border:1px solid #333;border-radius:8px;padding:14px;margin-bottom:14px">
          <div style="color:#888;font-size:12px">{ts}</div>
          <div style="margin-top:6px;white-space:pre-wrap">{r['description']}</div>
          {img_html}
        </div>"""

    if not rows:
        rows = "<p style='color:#888'>Belum ada laporan.</p>"

    return f"""
    <html><body style="background:#0f0f10;color:#eee;font-family:sans-serif;padding:30px 20px;max-width:600px;margin:0 auto">
      <h2>🐛 Laporan Bug ({len(REPORTS)})</h2>
      {rows}
    </body></html>
    """


@app.get("/admin/reports/{report_id}/screenshot")
async def admin_report_screenshot(report_id: str, key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Akses ditolak.")
    report = next((r for r in REPORTS if r["id"] == report_id), None)
    if not report or not report["screenshot_path"] or not os.path.exists(report["screenshot_path"]):
        raise HTTPException(status_code=404, detail="Screenshot tidak ditemukan.")
    return FileResponse(report["screenshot_path"])


@app.get("/d/{token}", response_class=HTMLResponse)
async def download_page(token: str):
    entry = FILES.get(token)
    if not entry or not os.path.exists(entry["path"]) or is_expired(entry):
        return HTMLResponse("<h3>Link sudah tidak berlaku (kadaluarsa atau sudah pernah diunduh).</h3>", status_code=404)

    expiry_note = ""
    if entry.get("expires_at"):
        remaining_min = max(0, int((entry["expires_at"] - time.time()) / 60))
        expiry_note = f'<p style="color:#888;font-size:12px">Kadaluarsa dalam ~{remaining_min} menit</p>'

    return f"""
    <html><body style="background:#0f0f10;color:#eee;font-family:sans-serif;
    display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;margin:0">
      <h3>📦 {entry['filename']}</h3>
      <a href="/d/{token}/file">
        <button style="padding:10px 20px;background:#4da3ff;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:15px">
          Download
        </button>
      </a>
      <p style="color:#888;font-size:13px;margin-top:12px">File ini hanya bisa diunduh sekali.</p>
      {expiry_note}
    </body></html>
    """


@app.get("/d/{token}/file")
async def download_file(token: str):
    entry = FILES.get(token)
    if not entry or not os.path.exists(entry["path"]) or is_expired(entry):
        FILES.pop(token, None)
        return HTMLResponse("<h3>Link sudah tidak berlaku (kadaluarsa atau sudah pernah diunduh).</h3>", status_code=404)
    FILES.pop(token, None)
    return FileResponse(
        entry["path"],
        filename=entry["filename"],
        background=BackgroundTask(os.remove, entry["path"]),
    )


@app.get("/b/{batch_token}", response_class=HTMLResponse)
async def batch_page(batch_token: str):
    items = BATCHES.get(batch_token)
    if not items:
        return HTMLResponse("<h3>Link batch tidak ditemukan atau sudah kadaluarsa.</h3>", status_code=404)

    rows = ""
    for item in items:
        entry = FILES.get(item["token"])
        if entry and not is_expired(entry):
            note = ""
            if entry.get("expires_at"):
                remaining_min = max(0, int((entry["expires_at"] - time.time()) / 60))
                note = f'<span style="font-size:11px;color:#666"> ({remaining_min}m lagi)</span>'
            rows += f"""
            <div class="row">
              <span>{item['filename']}{note}</span>
              <a href="/d/{item['token']}/file"><button>Download</button></a>
            </div>"""
        else:
            rows += f"""
            <div class="row done">
              <span>{item['filename']}</span>
              <span class="done-label">✓ tidak tersedia lagi</span>
            </div>"""

    return f"""
    <html><head><style>
      body{{background:#0f0f10;color:#eee;font-family:sans-serif;display:flex;flex-direction:column;
           align-items:center;min-height:100vh;margin:0;padding:30px 0}}
      .row{{display:flex;justify-content:space-between;align-items:center;width:360px;
           padding:10px 14px;border:1px solid #333;border-radius:8px;margin-bottom:8px}}
      .row.done{{opacity:0.5}}
      .done-label{{font-size:13px;color:#4dff88}}
      button{{padding:6px 14px;border:none;border-radius:6px;background:#4da3ff;color:#fff;cursor:pointer}}
    </style></head><body>
      <h3>📦 {len(items)} file dibagikan</h3>
      {rows}
      <p style="color:#888;font-size:13px;margin-top:12px">Tiap file hanya bisa diunduh sekali.</p>
    </body></html>
    """


@app.get("/", response_class=HTMLResponse)
async def home():
    return """
    <html><head><style>
      body{background:#0f0f10;color:#eee;font-family:sans-serif;display:flex;flex-direction:column;
           align-items:center;justify-content:center;height:100vh;margin:0;padding-top:26px;box-sizing:border-box}
      #owner-marquee{position:fixed;top:0;left:0;width:100%;height:26px;background:#4da3ff;
        overflow:hidden;white-space:nowrap;z-index:2000}
      #owner-marquee span{display:inline-block;position:relative;left:-100%;
        animation:owner-marquee-scroll 14s linear infinite;line-height:26px;
        color:#fff;font-size:12px;font-weight:600;padding-left:100%}
      @keyframes owner-marquee-scroll{
        0%{left:-100%}
        100%{left:100%}
      }
      #drop{border:2px dashed #555;border-radius:12px;width:360px;height:200px;display:flex;
            align-items:center;justify-content:center;text-align:center;cursor:pointer;color:#999;padding:10px}
      #drop.hover{border-color:#4da3ff;color:#4da3ff}
      #expiry-wrap{width:360px;margin-top:14px;text-align:left}
      #expiry-wrap label{font-size:12px;color:#999;display:block;margin-bottom:4px}
      #expiry{width:100%;padding:8px;background:#1a1a1c;color:#eee;border:1px solid #444;border-radius:6px}
      #result{margin-top:20px;word-break:break-all;max-width:360px;text-align:center}
      input[type=text]{width:100%;padding:8px;background:#1a1a1c;color:#eee;border:1px solid #444;border-radius:6px}
      button{margin-top:8px;padding:8px 16px;border:none;border-radius:6px;background:#4da3ff;color:#fff;cursor:pointer}
      #progress-container{width:360px;margin-top:14px;display:none}
      #progress-bar-bg{width:100%;height:8px;background:#222;border-radius:4px;overflow:hidden}
      #progress-bar{height:100%;width:0%;background:#4da3ff;transition:width 0.15s linear}
      #progress-text{font-size:12px;color:#999;margin-top:4px;text-align:center}
      .error-text{color:#ff6b6b;font-size:13px}
      #help-btn{position:fixed;bottom:20px;right:20px;width:52px;height:52px;border-radius:50%;background:#4da3ff;
        display:flex;align-items:center;justify-content:center;font-size:22px;cursor:pointer;
        box-shadow:0 2px 8px rgba(0,0,0,0.4);z-index:1000}
      #help-panel{position:fixed;bottom:84px;right:20px;width:320px;max-height:420px;background:#1a1a1c;
        border:1px solid #333;border-radius:12px;display:none;flex-direction:column;overflow:hidden;z-index:1000}
      #help-panel.open{display:flex}
      #help-header{padding:12px;background:#222;font-size:14px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
      #help-close{cursor:pointer;color:#888}
      #help-list{overflow-y:auto;padding:8px}
      .faq-item{border-bottom:1px solid #2a2a2c}
      .faq-q{padding:10px 6px;cursor:pointer;font-size:13px;display:flex;justify-content:space-between;align-items:center}
      .faq-q:hover{color:#4da3ff}
      .faq-a{padding:0 6px 10px 6px;font-size:12px;color:#aaa;line-height:1.5;display:none}
      .faq-item.open .faq-a{display:block}
      .faq-arrow{transition:transform 0.15s}
      .faq-item.open .faq-arrow{transform:rotate(90deg)}
      #bug-btn{position:fixed;bottom:20px;left:20px;width:52px;height:52px;border-radius:50%;background:#ff6b6b;
        display:flex;align-items:center;justify-content:center;font-size:22px;cursor:pointer;
        box-shadow:0 2px 8px rgba(0,0,0,0.4);z-index:1000}
      #bug-panel{position:fixed;bottom:84px;left:20px;width:280px;background:#1a1a1c;border:1px solid #333;
        border-radius:12px;display:none;flex-direction:column;overflow:hidden;z-index:1000}
      #bug-panel.open{display:flex}
      #bug-header{padding:12px;background:#222;font-size:14px;display:flex;justify-content:space-between;align-items:center}
      #bug-close{cursor:pointer;color:#888}
      #bug-body{padding:10px;display:flex;flex-direction:column;gap:8px}
      #bug-desc{width:100%;height:70px;padding:8px;background:#1a1a1c;color:#eee;border:1px solid #444;
        border-radius:6px;font-size:13px;resize:none;box-sizing:border-box;font-family:inherit}
      #bug-screenshot{font-size:12px;color:#999}
      #bug-submit{padding:8px;background:#ff6b6b;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px;margin-top:0}
      #bug-status{font-size:12px}
    </style></head><body>
      <div id="owner-marquee"><span>This Property Owned by SkyBreak1927 !!!</span></div>
      <h2>🌀 wormhole-mini</h2>
      <a href="/pdf-tools" style="color:#4da3ff;font-size:13px;margin-bottom:14px;text-decoration:none">🛠️ Buka PDF Tools →</a>
      <div id="drop">Drop file (bisa lebih dari 1) di sini, atau klik untuk pilih<input id="file" type="file" multiple style="display:none"></div>
      <div id="expiry-wrap">
        <label for="expiry">Link tersedia selama</label>
        <select id="expiry">
          <option value="0">Instan (sekali unduh)</option>
          <option value="15">15 menit</option>
          <option value="30">30 menit</option>
          <option value="45">45 menit</option>
          <option value="60">1 jam</option>
        </select>
      </div>
      <div id="progress-container">
        <div id="progress-bar-bg"><div id="progress-bar"></div></div>
        <div id="progress-text">0%</div>
      </div>
      <div id="result"></div>

      <div id="help-btn" title="Bantuan">❓</div>
      <div id="help-panel">
        <div id="help-header">Pertanyaan Umum <span id="help-close">&times;</span></div>
        <div id="help-list">
          <div class="faq-item">
            <div class="faq-q">Cara upload file? <span class="faq-arrow">›</span></div>
            <div class="faq-a">Drop file ke kotak di tengah halaman, atau klik kotaknya untuk pilih file dari device kamu. Bisa pilih lebih dari 1 file sekaligus.</div>
          </div>
          <div class="faq-item">
            <div class="faq-q">Apa itu pilihan "tersedia selama"? <span class="faq-arrow">›</span></div>
            <div class="faq-a">"Instan" = link mati begitu file diunduh sekali. Pilihan 15/30/45/60 menit = link tetap mati setelah diunduh sekali, TAPI juga otomatis mati kalau waktu itu habis duluan meski belum pernah diunduh.</div>
          </div>
          <div class="faq-item">
            <div class="faq-q">Kenapa link saya bilang "tidak berlaku"? <span class="faq-arrow">›</span></div>
            <div class="faq-a">Dua kemungkinan: filenya sudah pernah diunduh sekali (link cuma bisa dipakai 1x), atau waktu kadaluarsanya sudah habis.</div>
          </div>
          <div class="faq-item">
            <div class="faq-q">Bagaimana cara kirim banyak file sekaligus? <span class="faq-arrow">›</span></div>
            <div class="faq-a">Pilih/drop lebih dari 1 file di halaman utama. Nanti dapat 1 link berisi daftar semua file, tiap file bisa diunduh terpisah (tetap 1x per file).</div>
          </div>
          <div class="faq-item">
            <div class="faq-q">Apakah file saya aman? <span class="faq-arrow">›</span></div>
            <div class="faq-a">File tidak dienkripsi end-to-end dan tidak ada password — siapa pun yang pegang link bisa akses. Keamanannya mengandalkan link acak yang sangat susah ditebak. Jangan kirim file sangat sensitif lewat sini.</div>
          </div>
          <div class="faq-item">
            <div class="faq-q">Berapa ukuran file maksimal? <span class="faq-arrow">›</span></div>
            <div class="faq-a">Maksimal 500MB per file.</div>
          </div>
        </div>
      </div>

      <div id="bug-btn" title="Laporkan bug">🐛</div>
      <div id="bug-panel">
        <div id="bug-header">Laporkan Bug <span id="bug-close">&times;</span></div>
        <div id="bug-body">
          <textarea id="bug-desc" placeholder="Ceritakan bug/error yang kamu temukan..."></textarea>
          <input id="bug-screenshot" type="file" accept="image/*">
          <button id="bug-submit">Kirim Laporan</button>
          <div id="bug-status"></div>
        </div>
      </div>

      <script>
        const drop = document.getElementById('drop');
        const fileInput = document.getElementById('file');
        const expirySelect = document.getElementById('expiry');
        drop.onclick = () => fileInput.click();
        drop.ondragover = e => { e.preventDefault(); drop.classList.add('hover'); };
        drop.ondragleave = () => drop.classList.remove('hover');
        drop.ondrop = e => { e.preventDefault(); drop.classList.remove('hover'); handleFiles(e.dataTransfer.files); };
        fileInput.onchange = () => handleFiles(fileInput.files);

        function showResult(link) {
          document.getElementById('result').innerHTML =
            `<p>Link:</p><input type="text" value="${link}" readonly onclick="this.select()">
             <br><button id="copybtn" onclick="copyLink('${link}')">Copy Link</button>`;
        }

        function copyLink(link) {
          navigator.clipboard.writeText(link);
          const btn = document.getElementById('copybtn');
          btn.textContent = "✓ Tercopy!";
          setTimeout(() => { btn.textContent = "Copy Link"; }, 1500);
        }

        function showProgress() {
          document.getElementById('progress-container').style.display = 'block';
          updateProgress(0);
        }

        function updateProgress(percent) {
          document.getElementById('progress-bar').style.width = percent + '%';
          document.getElementById('progress-text').textContent = percent + '%';
        }

        function hideProgress() {
          document.getElementById('progress-container').style.display = 'none';
        }

        function uploadWithProgress(url, formData) {
          return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open("POST", url);
            xhr.timeout = 180000;

            xhr.upload.onprogress = (e) => {
              if (e.lengthComputable) {
                updateProgress(Math.round((e.loaded / e.total) * 100));
              }
            };

            xhr.onload = () => {
              if (xhr.status >= 200 && xhr.status < 300) {
                resolve(JSON.parse(xhr.responseText));
              } else {
                let msg = "Upload gagal (" + xhr.status + ")";
                try {
                  const errData = JSON.parse(xhr.responseText);
                  if (errData.detail) msg = errData.detail;
                } catch (e) {}
                reject(new Error(msg));
              }
            };
            xhr.onerror = () => reject(new Error("Upload gagal (koneksi terputus)"));
            xhr.ontimeout = () => reject(new Error("Upload timeout — koneksi macet, coba lagi"));

            xhr.send(formData);
          });
        }

        async function handleFiles(fileList) {
          if (!fileList || fileList.length === 0) return;
          drop.textContent = "Mengupload...";
          document.getElementById('result').innerHTML = "";
          showProgress();

          const expiryMinutes = expirySelect.value;

          try {
            if (fileList.length === 1) {
              const form = new FormData();
              form.append("file", fileList[0]);
              form.append("expiry_minutes", expiryMinutes);
              const data = await uploadWithProgress("/upload", form);
              showResult(window.location.origin + "/d/" + data.token);
            } else {
              const form = new FormData();
              for (const f of fileList) form.append("files", f);
              form.append("expiry_minutes", expiryMinutes);
              const data = await uploadWithProgress("/upload-batch", form);
              showResult(window.location.origin + "/b/" + data.batch_token);
            }
          } catch (err) {
            document.getElementById('result').innerHTML = `<p class="error-text">${err.message}</p>`;
          } finally {
            hideProgress();
            drop.textContent = "Drop file (bisa lebih dari 1) di sini, atau klik untuk pilih";
          }
        }

        // --- FAQ widget ---
        const helpBtn = document.getElementById('help-btn');
        const helpPanel = document.getElementById('help-panel');
        const helpClose = document.getElementById('help-close');

        helpBtn.onclick = () => helpPanel.classList.toggle('open');
        helpClose.onclick = () => helpPanel.classList.remove('open');

        document.querySelectorAll('.faq-q').forEach(q => {
          q.onclick = () => q.parentElement.classList.toggle('open');
        });

        // --- Bug report widget ---
        const bugBtn = document.getElementById('bug-btn');
        const bugPanel = document.getElementById('bug-panel');
        const bugClose = document.getElementById('bug-close');
        const bugDesc = document.getElementById('bug-desc');
        const bugScreenshot = document.getElementById('bug-screenshot');
        const bugSubmit = document.getElementById('bug-submit');
        const bugStatus = document.getElementById('bug-status');

        bugBtn.onclick = () => bugPanel.classList.toggle('open');
        bugClose.onclick = () => bugPanel.classList.remove('open');

        bugSubmit.onclick = async () => {
          const desc = bugDesc.value.trim();
          if (!desc) {
            bugStatus.textContent = "Isi deskripsi dulu ya.";
            bugStatus.style.color = '#ff6b6b';
            return;
          }

          const form = new FormData();
          form.append('description', desc);
          if (bugScreenshot.files[0]) form.append('screenshot', bugScreenshot.files[0]);

          bugSubmit.disabled = true;
          bugStatus.textContent = "Mengirim...";
          bugStatus.style.color = '#999';

          try {
            const res = await fetch('/report-bug', { method: 'POST', body: form });
            const data = await res.json();
            if (res.ok) {
              bugStatus.textContent = "✓ Terkirim, terima kasih!";
              bugStatus.style.color = '#4dff88';
              bugDesc.value = '';
              bugScreenshot.value = '';
            } else {
              bugStatus.textContent = data.detail || "Gagal mengirim.";
              bugStatus.style.color = '#ff6b6b';
            }
          } catch (e) {
            bugStatus.textContent = "Gagal terhubung ke server.";
            bugStatus.style.color = '#ff6b6b';
          } finally {
            bugSubmit.disabled = false;
          }
        };
      </script>
    </body></html>
    """
