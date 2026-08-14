import os, re, secrets, time, asyncio
from typing import List, Optional
from collections import defaultdict
from fastapi import FastAPI, UploadFile, File, Request, HTTPException, Form
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

app = FastAPI()

STORAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)

MAX_FILE_SIZE = 500 * 1024 * 1024   # 500 MB per file
RATE_LIMIT = 5                       # maks 5 upload request
RATE_WINDOW = 60                     # per 60 detik per IP
MAX_FILES_PER_BATCH = 20             # maks file per batch upload
CLEANUP_INTERVAL = 30                # detik, jeda scan cleanup

FILES = {}
BATCHES = {}
upload_log = defaultdict(list)  # ip -> [timestamp upload]


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


@app.on_event("startup")
async def start_cleanup():
    asyncio.create_task(cleanup_expired_loop())


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
           align-items:center;justify-content:center;height:100vh;margin:0}
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
    </style></head><body>
      <h2>🌀 wormhole-mini</h2>
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
      </script>
    </body></html>
    """
