import io, time
from collections import defaultdict
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

router = APIRouter()

PDF_MAX_SIZE = 50 * 1024 * 1024   # 50 MB per file
PDF_RATE_LIMIT = 10
PDF_RATE_WINDOW = 60              # detik
pdf_rate_log = defaultdict(list)  # ip -> [timestamp]


def check_pdf_rate_limit(ip: str):
    now = time.time()
    pdf_rate_log[ip] = [t for t in pdf_rate_log[ip] if now - t < PDF_RATE_WINDOW]
    if len(pdf_rate_log[ip]) >= PDF_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Terlalu banyak proses, coba lagi sebentar.")
    pdf_rate_log[ip].append(now)


async def read_pdf_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if len(content) > PDF_MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"File melebihi batas {PDF_MAX_SIZE // (1024*1024)}MB")
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="File bukan PDF yang valid.")
    return content


def safe_read_pdf(content: bytes, password: str = None) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            if not password:
                raise HTTPException(status_code=400, detail="PDF ini terkunci password, isi password dulu.")
            if not reader.decrypt(password):
                raise HTTPException(status_code=400, detail="Password salah.")
        return reader
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Gagal membaca PDF, file mungkin rusak.")


def parse_page_range(spec: str, total: int) -> List[int]:
    indices = []
    spec = (spec or "").strip()
    if not spec:
        raise HTTPException(status_code=400, detail="Isi nomor halaman dulu (contoh: 1-3,5,7-9).")
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                raise HTTPException(status_code=400, detail=f"Format halaman tidak valid: '{part}'")
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Format halaman tidak valid: '{part}'")
            if start < 1 or end > total or start > end:
                raise HTTPException(status_code=400, detail=f"Rentang '{part}' di luar batas (1-{total}).")
            indices.extend(range(start - 1, end))
        else:
            try:
                n = int(part)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Format halaman tidak valid: '{part}'")
            if n < 1 or n > total:
                raise HTTPException(status_code=400, detail=f"Halaman {n} di luar batas (1-{total}).")
            indices.append(n - 1)
    return sorted(set(indices))


def pdf_response(writer: PdfWriter, filename: str) -> StreamingResponse:
    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/pdf/merge")
async def pdf_merge(request: Request, files: List[UploadFile] = File(...)):
    check_pdf_rate_limit(request.client.host)
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Pilih minimal 2 file PDF untuk digabung.")

    writer = PdfWriter()
    for f in files:
        content = await read_pdf_upload(f)
        reader = safe_read_pdf(content)
        for page in reader.pages:
            writer.add_page(page)

    return pdf_response(writer, "merged.pdf")
    @router.post("/pdf/split")
async def pdf_split(request: Request, file: UploadFile = File(...), pages: str = Form(...)):
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
    reader = safe_read_pdf(content)
    indices = parse_page_range(pages, len(reader.pages))

    writer = PdfWriter()
    for i in indices:
        writer.add_page(reader.pages[i])

    return pdf_response(writer, "extracted.pdf")


@router.post("/pdf/rotate")
async def pdf_rotate(request: Request, file: UploadFile = File(...), angle: int = Form(...)):
    check_pdf_rate_limit(request.client.host)
    if angle not in (90, 180, 270):
        raise HTTPException(status_code=400, detail="Sudut rotasi harus 90, 180, atau 270.")
    content = await read_pdf_upload(file)
    reader = safe_read_pdf(content)

    writer = PdfWriter()
    for page in reader.pages:
        page.rotate(angle)
        writer.add_page(page)

    return pdf_response(writer, "rotated.pdf")


@router.post("/pdf/delete-pages")
async def pdf_delete_pages(request: Request, file: UploadFile = File(...), pages: str = Form(...)):
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
    reader = safe_read_pdf(content)
    total = len(reader.pages)
    remove_indices = set(parse_page_range(pages, total))

    if len(remove_indices) >= total:
        raise HTTPException(status_code=400, detail="Tidak bisa menghapus semua halaman.")

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i not in remove_indices:
            writer.add_page(page)

    return pdf_response(writer, "edited.pdf")


@router.post("/pdf/protect")
async def pdf_protect(request: Request, file: UploadFile = File(...), password: str = Form(...)):
    check_pdf_rate_limit(request.client.host)
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password minimal 4 karakter.")
    content = await read_pdf_upload(file)
    reader = safe_read_pdf(content)

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)

    return pdf_response(writer, "protected.pdf")


@router.post("/pdf/unlock")
async def pdf_unlock(request: Request, file: UploadFile = File(...), password: str = Form(...)):
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
    reader = safe_read_pdf(content, password=password)

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)

    return pdf_response(writer, "unlocked.pdf")


@router.post("/pdf/watermark")
async def pdf_watermark(request: Request, file: UploadFile = File(...), text: str = Form(...)):
    check_pdf_rate_limit(request.client.host)
    text = text.strip()[:100]
    if not text:
        raise HTTPException(status_code=400, detail="Teks watermark tidak boleh kosong.")
    content = await read_pdf_upload(file)
    reader = safe_read_pdf(content)

    writer = PdfWriter()
    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)

        wm_buf = io.BytesIO()
        c = canvas.Canvas(wm_buf, pagesize=(w, h))
        c.saveState()
        c.setFont("Helvetica-Bold", 40)
        c.setFillColorRGB(0.5, 0.5, 0.5, alpha=0.3)
        c.translate(w / 2, h / 2)
        c.rotate(45)
        c.drawCentredString(0, 0, text)
        c.restoreState()
        c.save()
        wm_buf.seek(0)

        wm_reader = PdfReader(wm_buf)
        page.merge_page(wm_reader.pages[0])
        writer.add_page(page)

    return pdf_response(writer, "watermarked.pdf")


@router.get("/pdf-tools", response_class=HTMLResponse)
async def pdf_tools_page():
    return """
    <html><head><style>
      body{background:#0f0f10;color:#eee;font-family:sans-serif;display:flex;flex-direction:column;
           align-items:center;min-height:100vh;margin:0;padding:30px 16px}
      h2{margin-bottom:20px}
      .tool-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;width:100%;max-width:600px}
      .tool-card{border:1px solid #333;border-radius:10px;padding:16px 10px;text-align:center;cursor:pointer;
                 font-size:13px;transition:border-color 0.15s}
      .tool-card:hover{border-color:#4da3ff}
      .tool-card.active{border-color:#4da3ff;background:#1a1a1c}
      .tool-icon{font-size:26px;display:block;margin-bottom:6px}
      #tool-form{width:100%;max-width:400px;margin-top:20px;display:none}
      #tool-form.open{display:block}
      #tool-form input[type=text],#tool-form input[type=number],#tool-form input[type=password]{
        width:100%;padding:8px;background:#1a1a1c;color:#eee;border:1px solid #444;border-radius:6px;
        margin-top:6px;box-sizing:border-box;font-size:13px}
      #tool-form input[type=file]{width:100%;margin-top:6px;font-size:12px;color:#999}
      #tool-form label{font-size:12px;color:#999;display:block;margin-top:12px}
      #tool-form select{width:100%;padding:8px;background:#1a1a1c;color:#eee;border:1px solid #444;
        border-radius:6px;margin-top:6px;font-size:13px}
      #tool-submit{width:100%;margin-top:16px;padding:10px;background:#4da3ff;color:#fff;border:none;
        border-radius:6px;cursor:pointer;font-size:14px}
      #tool-submit:disabled{opacity:0.5;cursor:not-allowed}
      #tool-status{margin-top:10px;font-size:13px;text-align:center}
      a.back-link{color:#888;font-size:13px;margin-bottom:16px;text-decoration:none}
      #merge-zone{border:2px dashed #555;border-radius:10px;padding:16px;text-align:center;cursor:pointer;
                  font-size:13px;color:#999;margin-top:6px;transition:border-color 0.15s}
      #merge-zone.hover{border-color:#4da3ff;color:#4da3ff}
      #merge-file-list{margin-top:10px}
      .merge-item{display:flex;align-items:center;gap:6px;padding:6px 8px;border:1px solid #333;
                  border-radius:6px;margin-bottom:6px;font-size:12px}
      .merge-item span.mname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .merge-item button{padding:3px 7px;background:#222;border:1px solid #444;border-radius:5px;
                  color:#eee;cursor:pointer;font-size:11px}
      .merge-item button:disabled{opacity:0.3;cursor:not-allowed}
      .merge-item button.mremove{color:#ff6b6b}
      #tool-progress-container{width:100%;margin-top:14px;display:none}
      #tool-progress-bar-bg{width:100%;height:8px;background:#222;border-radius:4px;overflow:hidden}
      #tool-progress-bar{height:100%;width:0%;background:#4da3ff;transition:width 0.15s linear}
      #tool-progress-text{font-size:12px;color:#999;margin-top:4px;text-align:center}
    </style></head><body>
      <a href="/" class="back-link">← Kembali</a>
      <h2>🛠️ PDF Tools</h2>
      <div class="tool-grid">
        <div class="tool-card" data-tool="merge"><span class="tool-icon">🔗</span>Merge</div>
        <div class="tool-card" data-tool="split"><span class="tool-icon">✂️</span>Split / Extract</div>
        <div class="tool-card" data-tool="rotate"><span class="tool-icon">🔄</span>Rotate</div>
        <div class="tool-card" data-tool="delete"><span class="tool-icon">🗑️</span>Hapus Halaman</div>
        <div class="tool-card" data-tool="protect"><span class="tool-icon">🔒</span>Protect</div>
        <div class="tool-card" data-tool="unlock"><span class="tool-icon">🔓</span>Unlock</div>
        <div class="tool-card" data-tool="watermark"><span class="tool-icon">💧</span>Watermark</div>
      </div>

      <div id="tool-form">
        <div id="form-fields"></div>
        <div id="tool-progress-container">
          <div id="tool-progress-bar-bg"><div id="tool-progress-bar"></div></div>
          <div id="tool-progress-text">0%</div>
        </div>
        <button id="tool-submit">Proses & Download</button>
        <div id="tool-status"></div>
      </div>

      <script>
        const SIMPLE_TOOLS = {
          split: {
            endpoint: '/pdf/split', filename: 'extracted.pdf',
            fields: '<label>File PDF</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Halaman yang diambil (contoh: 1-3,5,7-9)</label><input type="text" name="pages" placeholder="1-3,5">'
          },
          rotate: {
            endpoint: '/pdf/rotate', filename: 'rotated.pdf',
            fields: '<label>File PDF</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Sudut rotasi</label><select name="angle"><option value="90">90°</option><option value="180">180°</option><option value="270">270°</option></select>'
          },
          delete: {
            endpoint: '/pdf/delete-pages', filename: 'edited.pdf',
            fields: '<label>File PDF</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Halaman yang dihapus (contoh: 2,4-6)</label><input type="text" name="pages" placeholder="2,4-6">'
          },
          protect: {
            endpoint: '/pdf/protect', filename: 'protected.pdf',
            fields: '<label>File PDF</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Password baru</label><input type="password" name="password" placeholder="Min. 4 karakter">'
          },
          unlock: {
            endpoint: '/pdf/unlock', filename: 'unlocked.pdf',
            fields: '<label>File PDF (terkunci)</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Password saat ini</label><input type="password" name="password">'
          },
          watermark: {
            endpoint: '/pdf/watermark', filename: 'watermarked.pdf',
            fields: '<label>File PDF</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Teks watermark</label><input type="text" name="text" placeholder="Contoh: CONFIDENTIAL">'
          }
        };

        const MERGE_FIELDS_HTML =
          '<label>Tambahkan file PDF (urutan menentukan hasil merge)</label>' +
          '<div id="merge-zone">Klik atau drop file PDF di sini<input id="merge-file-input" type="file" accept="application/pdf" multiple style="display:none"></div>' +
          '<div id="merge-file-list"></div>';

        let activeTool = null;
        let mergeFiles = [];

        const toolForm = document.getElementById('tool-form');
        const formFields = document.getElementById('form-fields');
        const toolStatus = document.getElementById('tool-status');
        const toolSubmit = document.getElementById('tool-submit');
        const progressContainer = document.getElementById('tool-progress-container');
        const progressBar = document.getElementById('tool-progress-bar');
        const progressText = document.getElementById('tool-progress-text');

        function renderMergeList() {
          const listEl = document.getElementById('merge-file-list');
          if (!listEl) return;
          listEl.innerHTML = mergeFiles.map((f, i) => `
            <div class="merge-item">
              <span class="mname">${i + 1}. ${f.name}</span>
              <button type="button" data-action="up" data-idx="${i}" ${i === 0 ? 'disabled' : ''}>↑</button>
              <button type="button" data-action="down" data-idx="${i}" ${i === mergeFiles.length - 1 ? 'disabled' : ''}>↓</button>
              <button type="button" class="mremove" data-action="remove" data-idx="${i}">✕</button>
            </div>
          `).join('');

          listEl.querySelectorAll('button').forEach(btn => {
            btn.onclick = () => {
              const idx = parseInt(btn.dataset.idx);
              const action = btn.dataset.action;
              if (action === 'up' && idx > 0) {
                [mergeFiles[idx - 1], mergeFiles[idx]] = [mergeFiles[idx], mergeFiles[idx - 1]];
              } else if (action === 'down' && idx < mergeFiles.length - 1) {
                [mergeFiles[idx + 1], mergeFiles[idx]] = [mergeFiles[idx], mergeFiles[idx + 1]];
              } else if (action === 'remove') {
                mergeFiles.splice(idx, 1);
              }
              renderMergeList();
            };
          });
        }

        function setupMergeZone() {
          const zone = document.getElementById('merge-zone');
          const input = document.getElementById('merge-file-input');
          if (!zone || !input) return;

          zone.onclick = () => input.click();
          input.onchange = () => {
            for (const f of input.files) mergeFiles.push(f);
            input.value = '';
            renderMergeList();
          };
          zone.ondragover = e => { e.preventDefault(); zone.classList.add('hover'); };
          zone.ondragleave = () => zone.classList.remove('hover');
          zone.ondrop = e => {
            e.preventDefault();
            zone.classList.remove('hover');
            for (const f of e.dataTransfer.files) {
              if (f.type === 'application/pdf') mergeFiles.push(f);
            }
            renderMergeList();
          };
        }

        document.querySelectorAll('.tool-card').forEach(card => {
          card.onclick = () => {
            document.querySelectorAll('.tool-card').forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            activeTool = card.dataset.tool;
            toolStatus.textContent = '';
            progressContainer.style.display = 'none';

            if (activeTool === 'merge') {
              mergeFiles = [];
              formFields.innerHTML = MERGE_FIELDS_HTML;
              setupMergeZone();
              renderMergeList();
            } else {
              formFields.innerHTML = SIMPLE_TOOLS[activeTool].fields;
            }
            toolForm.classList.add('open');
          };
        });

        function updateProgress(percent, label) {
          progressContainer.style.display = 'block';
          progressBar.style.width = percent + '%';
          progressText.textContent = label || (percent + '%');
        }

        function submitWithProgress(url, formData) {
          return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url);
            xhr.responseType = 'blob';
            xhr.timeout = 180000;

            xhr.upload.onprogress = (e) => {
              if (e.lengthComputable) {
                const percent = Math.round((e.loaded / e.total) * 100);
                updateProgress(percent, percent < 100 ? ('Mengupload... ' + percent + '%') : 'Memproses di server...');
              }
            };

            xhr.onload = () => {
              if (xhr.status >= 200 && xhr.status < 300) {
                resolve(xhr.response);
              } else {
                const reader = new FileReader();
                reader.onload = () => {
                  try {
                    const data = JSON.parse(reader.result);
                    reject(new Error(data.detail || 'Gagal memproses PDF'));
                  } catch (e) {
                    reject(new Error('Gagal memproses PDF'));
                  }
                };
                reader.onerror = () => reject(new Error('Gagal memproses PDF'));
                reader.readAsText(xhr.response);
              }
            };
            xhr.onerror = () => reject(new Error('Koneksi terputus'));
            xhr.ontimeout = () => reject(new Error('Timeout, koneksi macet — coba lagi'));

            xhr.send(formData);
          });
        }

        toolSubmit.onclick = async () => {
          if (!activeTool) return;

          const form = new FormData();
          let endpoint, filename;

          if (activeTool === 'merge') {
            if (mergeFiles.length < 2) {
              toolStatus.textContent = 'Tambahkan minimal 2 file PDF.';
              toolStatus.style.color = '#ff6b6b';
              return;
            }
            mergeFiles.forEach(f => form.append('files', f));
            endpoint = '/pdf/merge';
            filename = 'merged.pdf';
          } else {
            const tool = SIMPLE_TOOLS[activeTool];
            let hasFile = false;
            formFields.querySelectorAll('input, select').forEach(el => {
              if (el.type === 'file') {
                if (el.files[0]) { form.append(el.name, el.files[0]); hasFile = true; }
              } else {
                form.append(el.name, el.value);
              }
            });
            if (!hasFile) {
              toolStatus.textContent = 'Pilih file PDF dulu.';
              toolStatus.style.color = '#ff6b6b';
              return;
            }
            endpoint = tool.endpoint;
            filename = tool.filename;
          }

          toolSubmit.disabled = true;
          toolStatus.textContent = '';
          updateProgress(0, 'Mengupload... 0%');

          try {
            const blob = await submitWithProgress(endpoint, form);
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = filename;
            document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(url);
            toolStatus.textContent = '✓ Selesai, file terunduh.';
            toolStatus.style.color = '#4dff88';
          } catch (err) {
            toolStatus.textContent = err.message;
            toolStatus.style.color = '#ff6b6b';
          } finally {
            toolSubmit.disabled = false;
            progressContainer.style.display = 'none';
          }
        };
      </script>
    </body></html>
    """
