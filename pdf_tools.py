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
      #tool-status{margin-top:10px;font-size:13px;text-align:center}
      a.back-link{color:#888;font-size:13px;margin-bottom:16px;text-decoration:none}
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
        <button id="tool-submit">Proses & Download</button>
        <div id="tool-status"></div>
      </div>

      <script>
        const TOOLS = {
          merge: {
            endpoint: '/pdf/merge', filename: 'merged.pdf',
            fields: '<label>Pilih 2+ file PDF</label><input type="file" name="files" accept="application/pdf" multiple>'
          },
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

        let activeTool = null;
        const toolForm = document.getElementById('tool-form');
        const formFields = document.getElementById('form-fields');
        const toolStatus = document.getElementById('tool-status');
        const toolSubmit = document.getElementById('tool-submit');

        document.querySelectorAll('.tool-card').forEach(card => {
          card.onclick = () => {
            document.querySelectorAll('.tool-card').forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            activeTool = card.dataset.tool;
            formFields.innerHTML = TOOLS[activeTool].fields;
            toolForm.classList.add('open');
            toolStatus.textContent = '';
          };
        });

        toolSubmit.onclick = async () => {
          if (!activeTool) return;
          const tool = TOOLS[activeTool];
          const form = new FormData();
          let hasFile = false;

          formFields.querySelectorAll('input, select').forEach(el => {
            if (el.type === 'file') {
              if (el.multiple) {
                for (const f of el.files) { form.append(el.name, f); hasFile = true; }
              } else if (el.files[0]) {
                form.append(el.name, el.files[0]); hasFile = true;
              }
            } else {
              form.append(el.name, el.value);
            }
          });

          if (!hasFile) {
            toolStatus.textContent = 'Pilih file PDF dulu.';
            toolStatus.style.color = '#ff6b6b';
            return;
          }

          toolSubmit.disabled = true;
          toolStatus.textContent = 'Memproses...';
          toolStatus.style.color = '#999';

          try {
            const res = await fetch(tool.endpoint, { method: 'POST', body: form });
            if (!res.ok) {
              let msg = 'Gagal memproses PDF';
              try { const data = await res.json(); msg = data.detail || msg; } catch (e) {}
              throw new Error(msg);
            }
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = tool.filename;
            document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(url);
            toolStatus.textContent = '✓ Selesai, file terunduh.';
            toolStatus.style.color = '#4dff88';
          } catch (err) {
            toolStatus.textContent = err.message;
            toolStatus.style.color = '#ff6b6b';
          } finally {
            toolSubmit.disabled = false;
          }
        };
      </script>
    </body></html>
    """
