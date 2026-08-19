import io, time, json, zipfile, tempfile, os
from collections import defaultdict
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
import pymupdf
from PIL import Image
from pdf2docx import Converter

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
async def pdf_rotate(request: Request, file: UploadFile = File(...), angles: str = Form(...)):
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
    reader = safe_read_pdf(content)

    try:
        angle_map = json.loads(angles)
        if not isinstance(angle_map, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Data rotasi tidak valid.")

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        raw_angle = angle_map.get(str(i), 0)
        try:
            angle = int(raw_angle)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"Sudut rotasi halaman {i + 1} tidak valid.")
        if angle not in (0, 90, 180, 270):
            raise HTTPException(status_code=400, detail=f"Sudut rotasi halaman {i + 1} harus 0, 90, 180, atau 270.")
        if angle:
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


WATERMARK_POSITIONS = {
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
}
WATERMARK_MARGIN = 50


def watermark_xy(position: str, w: float, h: float):
    m = WATERMARK_MARGIN
    return {
        "top-left": (m, h - m),
        "top-center": (w / 2, h - m),
        "top-right": (w - m, h - m),
        "middle-left": (m, h / 2),
        "center": (w / 2, h / 2),
        "middle-right": (w - m, h / 2),
        "bottom-left": (m, m),
        "bottom-center": (w / 2, m),
        "bottom-right": (w - m, m),
    }[position]


def parse_hex_color(color: str):
    color = (color or "").strip().lstrip("#")
    if len(color) != 6:
        raise HTTPException(status_code=400, detail="Format warna tidak valid, pakai kode hex (contoh: #808080).")
    try:
        r = int(color[0:2], 16) / 255
        g = int(color[2:4], 16) / 255
        b = int(color[4:6], 16) / 255
    except ValueError:
        raise HTTPException(status_code=400, detail="Format warna tidak valid, pakai kode hex (contoh: #808080).")
    return r, g, b


@router.post("/pdf/watermark")
async def pdf_watermark(
    request: Request,
    file: UploadFile = File(...),
    text: str = Form(...),
    position: str = Form("center"),
    font_size: int = Form(40),
    color: str = Form("#808080"),
):
    check_pdf_rate_limit(request.client.host)
    text = text.strip()[:100]
    if not text:
        raise HTTPException(status_code=400, detail="Teks watermark tidak boleh kosong.")
    if position not in WATERMARK_POSITIONS:
        raise HTTPException(status_code=400, detail="Posisi watermark tidak valid.")
    if font_size < 8 or font_size > 200:
        raise HTTPException(status_code=400, detail="Ukuran font harus antara 8-200.")
    r, g, b = parse_hex_color(color)

    content = await read_pdf_upload(file)
    reader = safe_read_pdf(content)

    writer = PdfWriter()
    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        x, y = watermark_xy(position, w, h)

        wm_buf = io.BytesIO()
        c = canvas.Canvas(wm_buf, pagesize=(w, h))
        c.saveState()
        c.setFont("Helvetica-Bold", font_size)
        c.setFillColorRGB(r, g, b, alpha=0.4)
        c.translate(x, y)
        if position == "center":
            c.rotate(45)
        c.drawCentredString(0, 0, text)
        c.restoreState()
        c.save()
        wm_buf.seek(0)

        wm_reader = PdfReader(wm_buf)
        page.merge_page(wm_reader.pages[0])
        writer.add_page(page)

    return pdf_response(writer, "watermarked.pdf")


PDF_TO_JPG_MAX_PAGES = 30
IMAGE_TO_PDF_MAX_FILES = 30
CONVERT_MAX_SIZE = 30 * 1024 * 1024  # 30 MB, konversi lebih berat dari operasi PDF biasa


@router.post("/pdf/to-jpg")
async def pdf_to_jpg(request: Request, file: UploadFile = File(...)):
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
    if len(content) > CONVERT_MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"File melebihi batas {CONVERT_MAX_SIZE // (1024*1024)}MB untuk konversi.")

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Gagal membaca PDF, file mungkin rusak.")

    if doc.page_count > PDF_TO_JPG_MAX_PAGES:
        doc.close()
        raise HTTPException(status_code=400, detail=f"Maks {PDF_TO_JPG_MAX_PAGES} halaman untuk konversi ke JPG.")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(doc.page_count):
            page = doc[i]
            pix = page.get_pixmap(dpi=150)
            zf.writestr(f"page_{i + 1}.jpg", pix.tobytes("jpg"))
    doc.close()
    zip_buf.seek(0)

    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="pdf_to_jpg.zip"'},
    )


@router.post("/image/to-pdf")
async def image_to_pdf(request: Request, images: List[UploadFile] = File(...)):
    check_pdf_rate_limit(request.client.host)
    if not images:
        raise HTTPException(status_code=400, detail="Pilih minimal 1 gambar.")
    if len(images) > IMAGE_TO_PDF_MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Maks {IMAGE_TO_PDF_MAX_FILES} gambar sekaligus.")

    pil_images = []
    for img_file in images:
        content = await img_file.read()
        if len(content) > CONVERT_MAX_SIZE:
            raise HTTPException(status_code=413, detail=f"{img_file.filename} melebihi batas {CONVERT_MAX_SIZE // (1024*1024)}MB.")
        try:
            im = Image.open(io.BytesIO(content))
            im = im.convert("RGB")
            pil_images.append(im)
        except Exception:
            raise HTTPException(status_code=400, detail=f"{img_file.filename} bukan file gambar yang valid.")

    output = io.BytesIO()
    pil_images[0].save(output, format="PDF", save_all=True, append_images=pil_images[1:])
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="images_to_pdf.pdf"'},
    )


PDF_TO_WORD_MAX_PAGES = 50  # konversi Word jauh lebih berat per-halaman dibanding operasi PDF lain


@router.post("/pdf/to-word")
async def pdf_to_word(request: Request, file: UploadFile = File(...)):
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
    if len(content) > CONVERT_MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"File melebihi batas {CONVERT_MAX_SIZE // (1024*1024)}MB untuk konversi.")

    # cek jumlah halaman dulu (murah) sebelum commit ke proses konversi yang berat
    page_count_reader = safe_read_pdf(content)
    if len(page_count_reader.pages) > PDF_TO_WORD_MAX_PAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Maks {PDF_TO_WORD_MAX_PAGES} halaman untuk konversi ke Word (server terbatas, dokumen besar butuh waktu sangat lama).",
        )

    tmp_in_path = None
    tmp_out_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_in:
            tmp_in.write(content)
            tmp_in_path = tmp_in.name
        tmp_out_path = tmp_in_path[:-4] + ".docx"

        cv = Converter(tmp_in_path)
        cv.convert(tmp_out_path)
        cv.close()

        with open(tmp_out_path, "rb") as f:
            result_bytes = f.read()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Gagal mengonversi PDF ini ke Word. Coba file lain atau layout yang lebih sederhana.")
    finally:
        if tmp_in_path and os.path.exists(tmp_in_path):
            os.remove(tmp_in_path)
        if tmp_out_path and os.path.exists(tmp_out_path):
            os.remove(tmp_out_path)

    output = io.BytesIO(result_bytes)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="converted.docx"'},
    )


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
      #tool-form{width:100%;max-width:520px;margin-top:20px;display:none}
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
      #merge-zone,#rotate-zone{border:2px dashed #555;border-radius:10px;padding:16px;text-align:center;cursor:pointer;
                  font-size:13px;color:#999;margin-top:6px;transition:border-color 0.15s}
      #merge-zone.hover,#rotate-zone.hover{border-color:#4da3ff;color:#4da3ff}
      #merge-file-list{margin-top:10px}
      .merge-item{display:flex;align-items:center;gap:6px;padding:6px 8px;border:1px solid #333;
                  border-radius:6px;margin-bottom:6px;font-size:12px}
      .merge-item span.mname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .merge-item button{padding:3px 7px;background:#222;border:1px solid #444;border-radius:5px;
                  color:#eee;cursor:pointer;font-size:11px}
      .merge-item button:disabled{opacity:0.3;cursor:not-allowed}
      .merge-item button.mremove{color:#ff6b6b}
      #rotate-thumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:10px;margin-top:12px}
      .rotate-thumb{position:relative;border:1px solid #333;border-radius:8px;padding:8px 6px;text-align:center;
                    background:#1a1a1c;overflow:hidden}
      .rotate-thumb canvas{max-width:100%;height:auto;transition:transform 0.2s ease;display:block;margin:0 auto}
      .rotate-thumb-label{font-size:10px;color:#888;margin-top:6px}
      .rotate-thumb-btn{position:absolute;top:5px;right:5px;width:24px;height:24px;border-radius:50%;
        background:#4da3ff;color:#fff;border:none;cursor:pointer;font-size:14px;line-height:1;
        display:flex;align-items:center;justify-content:center}
      .rotate-thumb-btn:hover{background:#6db4ff}
      #rotate-note{grid-column:1/-1;font-size:11px;color:#888;margin-top:4px}
      #tool-progress-container{width:100%;margin-top:14px;display:none}
      #tool-progress-bar-bg{width:100%;height:8px;background:#222;border-radius:4px;overflow:hidden}
      #tool-progress-bar{height:100%;width:0%;background:#4da3ff;transition:width 0.15s linear}
      #tool-progress-text{font-size:12px;color:#999;margin-top:4px;text-align:center}
      .pos-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-top:6px;width:140px}
      .pos-btn{aspect-ratio:1;border:1px solid #444;border-radius:6px;background:#1a1a1c;cursor:pointer}
      .pos-btn.active{border-color:#4da3ff;background:#2a3a4a}
      .pos-btn:hover{border-color:#4da3ff}
      input[type=color]{width:50px;height:32px;padding:2px;margin-top:6px;border:1px solid #444;
        border-radius:6px;background:#1a1a1c;cursor:pointer}
      .pw-wrap{position:relative;margin-top:6px}
      .pw-wrap input{width:100%;padding:8px 36px 8px 8px;background:#1a1a1c;color:#eee;
        border:1px solid #444;border-radius:6px;box-sizing:border-box;font-size:13px}
      .pw-toggle{position:absolute;right:4px;top:50%;transform:translateY(-50%);background:none;
        border:none;cursor:pointer;font-size:15px;padding:4px;margin:0}
      .cat-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;justify-content:center}
      .cat-tab{padding:7px 16px;border-radius:20px;border:1px solid #444;background:#1a1a1c;
        color:#ccc;font-size:13px;cursor:pointer;white-space:nowrap}
      .cat-tab:hover{border-color:#4da3ff}
      .cat-tab.active{background:#4da3ff;border-color:#4da3ff;color:#fff}
      .tool-card{display:flex}
      .tool-card.hidden{display:none}
      .drop-multi{margin-top:6px}
    </style></head><body>
      <a href="/" class="back-link">← Kembali</a>
      <h2>🛠️ PDF Tools</h2>
      <div class="cat-tabs">
        <div class="cat-tab active" data-cat="all">Semua</div>
        <div class="cat-tab" data-cat="organize">Organize</div>
        <div class="cat-tab" data-cat="convert">Convert</div>
        <div class="cat-tab" data-cat="security">Security</div>
        <div class="cat-tab" data-cat="edit">Edit</div>
      </div>
      <div class="tool-grid">
        <div class="tool-card" data-tool="merge" data-cat="organize"><span class="tool-icon">🔗</span>Merge</div>
        <div class="tool-card" data-tool="split" data-cat="organize"><span class="tool-icon">✂️</span>Split / Extract</div>
        <div class="tool-card" data-tool="rotate" data-cat="organize"><span class="tool-icon">🔄</span>Rotate</div>
        <div class="tool-card" data-tool="delete" data-cat="organize"><span class="tool-icon">🗑️</span>Hapus Halaman</div>
        <div class="tool-card" data-tool="pdf-to-jpg" data-cat="convert"><span class="tool-icon">🖼️</span>PDF ke JPG</div>
        <div class="tool-card" data-tool="jpg-to-pdf" data-cat="convert"><span class="tool-icon">📄</span>JPG ke PDF</div>
        <div class="tool-card" data-tool="pdf-to-word" data-cat="convert"><span class="tool-icon">📝</span>PDF ke Word</div>
        <div class="tool-card" data-tool="protect" data-cat="security"><span class="tool-icon">🔒</span>Protect</div>
        <div class="tool-card" data-tool="unlock" data-cat="security"><span class="tool-icon">🔓</span>Unlock</div>
        <div class="tool-card" data-tool="watermark" data-cat="edit"><span class="tool-icon">💧</span>Watermark</div>
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

      <script type="module">
        import * as pdfjsLib from 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.min.mjs';
        pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.worker.min.mjs';
        window.pdfjsLib = pdfjsLib;
        window.dispatchEvent(new Event('pdfjs-ready'));
      </script>

      <script>
        const pdfjsReady = window.pdfjsLib
          ? Promise.resolve()
          : new Promise(resolve => window.addEventListener('pdfjs-ready', resolve, { once: true }));

        const PW_TOGGLE_HTML =
          '<button type="button" class="pw-toggle" title="Lihat/sembunyikan password">👁️</button>';

        const SIMPLE_TOOLS = {
          split: {
            endpoint: '/pdf/split', filename: 'extracted.pdf',
            fields: '<label>File PDF</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Halaman yang diambil (contoh: 1-3,5,7-9)</label><input type="text" name="pages" placeholder="1-3,5">'
          },
          delete: {
            endpoint: '/pdf/delete-pages', filename: 'edited.pdf',
            fields: '<label>File PDF</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Halaman yang dihapus (contoh: 2,4-6)</label><input type="text" name="pages" placeholder="2,4-6">'
          },
          protect: {
            endpoint: '/pdf/protect', filename: 'protected.pdf',
            fields: '<label>File PDF</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Password baru</label>' +
                    '<div class="pw-wrap"><input type="password" name="password" placeholder="Min. 4 karakter">' + PW_TOGGLE_HTML + '</div>'
          },
          unlock: {
            endpoint: '/pdf/unlock', filename: 'unlocked.pdf',
            fields: '<label>File PDF (terkunci)</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Password saat ini</label>' +
                    '<div class="pw-wrap"><input type="password" name="password">' + PW_TOGGLE_HTML + '</div>'
          },
          watermark: {
            endpoint: '/pdf/watermark', filename: 'watermarked.pdf',
            fields: '<label>File PDF</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Teks watermark</label><input type="text" name="text" placeholder="Contoh: CONFIDENTIAL">' +
                    '<label>Posisi watermark</label>' +
                    '<div class="pos-grid">' +
                      ['top-left','top-center','top-right','middle-left','center','middle-right','bottom-left','bottom-center','bottom-right']
                        .map(p => `<button type="button" class="pos-btn${p === 'center' ? ' active' : ''}" data-pos="${p}"></button>`).join('') +
                    '</div>' +
                    '<input type="hidden" name="position" value="center">' +
                    '<label>Ukuran font</label><input type="number" name="font_size" value="40" min="8" max="200">' +
                    '<label>Warna</label><input type="color" name="color" value="#808080">'
          },
          'pdf-to-jpg': {
            endpoint: '/pdf/to-jpg', filename: 'pdf_to_jpg.zip',
            fields: '<label>File PDF (maks 30 halaman)</label><input type="file" name="file" accept="application/pdf">'
          },
          'pdf-to-word': {
            endpoint: '/pdf/to-word', filename: 'converted.docx', timeout: 300000,
            fields: '<label>File PDF (maks 50 halaman)</label><input type="file" name="file" accept="application/pdf">' +
                    '<p style="font-size:11px;color:#888;margin-top:8px">Hasil terbaik untuk PDF berbasis teks/tabel. Layout kompleks (kolom rumit, grafis berat) mungkin tidak sempurna. Dokumen banyak halaman bisa butuh beberapa menit.</p>'
          },
          'jpg-to-pdf': {
            endpoint: '/image/to-pdf', filename: 'images_to_pdf.pdf',
            fields: '<label>Pilih 1 atau lebih gambar (JPG/PNG)</label>' +
                    '<input type="file" name="images" accept="image/*" multiple class="drop-multi">'
          }
        };

        const MERGE_FIELDS_HTML =
          '<label>Tambahkan file PDF (urutan menentukan hasil merge)</label>' +
          '<div id="merge-zone">Klik atau drop file PDF di sini<input id="merge-file-input" type="file" accept="application/pdf" multiple style="display:none"></div>' +
          '<div id="merge-file-list"></div>';

        const ROTATE_FIELDS_HTML =
          '<label>Pilih file PDF, lalu klik ikon \u21bb di tiap halaman untuk memutar</label>' +
          '<div id="rotate-zone">Klik atau drop file PDF di sini<input id="rotate-file-input" type="file" accept="application/pdf" style="display:none"></div>' +
          '<div id="rotate-thumbs"></div>';

        const MAX_PREVIEW_PAGES = 30;

        let activeTool = null;
        let mergeFiles = [];
        let rotateFile = null;
        let pageAngles = {};
        let rotatePageCount = 0;

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

        async function loadRotateFile(file) {
          rotateFile = file;
          pageAngles = {};
          const thumbsEl = document.getElementById('rotate-thumbs');
          if (!thumbsEl) return;
          thumbsEl.innerHTML = '<p style="grid-column:1/-1;font-size:12px;color:#999">Memuat preview halaman...</p>';

          try {
            await pdfjsReady;
            const arrayBuffer = await file.arrayBuffer();
            const pdf = await window.pdfjsLib.getDocument({ data: arrayBuffer }).promise;
            rotatePageCount = pdf.numPages;
            const previewCount = Math.min(rotatePageCount, MAX_PREVIEW_PAGES);

            thumbsEl.innerHTML = '';
            for (let i = 1; i <= previewCount; i++) {
              const page = await pdf.getPage(i);
              const viewport = page.getViewport({ scale: 0.3 });
              const canvas = document.createElement('canvas');
              canvas.width = viewport.width;
              canvas.height = viewport.height;
              const ctx = canvas.getContext('2d');
              await page.render({ canvasContext: ctx, viewport }).promise;

              const wrap = document.createElement('div');
              wrap.className = 'rotate-thumb';
              wrap.dataset.page = String(i - 1);
              wrap.appendChild(canvas);

              const label = document.createElement('div');
              label.className = 'rotate-thumb-label';
              label.textContent = 'Hal. ' + i;
              wrap.appendChild(label);

              const btn = document.createElement('button');
              btn.type = 'button';
              btn.className = 'rotate-thumb-btn';
              btn.title = 'Putar 90°';
              btn.textContent = '↻';
              btn.onclick = () => {
                const idx = parseInt(wrap.dataset.page);
                const current = pageAngles[idx] || 0;
                const next = (current + 90) % 360;
                pageAngles[idx] = next;
                canvas.style.transform = 'rotate(' + next + 'deg)';
              };
              wrap.appendChild(btn);

              thumbsEl.appendChild(wrap);
            }

            if (rotatePageCount > MAX_PREVIEW_PAGES) {
              const note = document.createElement('p');
              note.id = 'rotate-note';
              note.textContent = 'Preview dibatasi ' + MAX_PREVIEW_PAGES + ' halaman pertama (dokumen ini ' +
                rotatePageCount + ' halaman). Halaman setelahnya tidak diputar.';
              thumbsEl.appendChild(note);
            }
          } catch (err) {
            thumbsEl.innerHTML = '<p style="grid-column:1/-1;font-size:12px;color:#ff6b6b">Gagal memuat preview PDF.</p>';
          }
        }

        function setupRotateZone() {
          const zone = document.getElementById('rotate-zone');
          const input = document.getElementById('rotate-file-input');
          if (!zone || !input) return;

          zone.onclick = () => input.click();
          input.onchange = () => { if (input.files[0]) loadRotateFile(input.files[0]); };
          zone.ondragover = e => { e.preventDefault(); zone.classList.add('hover'); };
          zone.ondragleave = () => zone.classList.remove('hover');
          zone.ondrop = e => {
            e.preventDefault();
            zone.classList.remove('hover');
            const f = e.dataTransfer.files[0];
            if (f && f.type === 'application/pdf') loadRotateFile(f);
          };
        }

        document.querySelectorAll('.cat-tab').forEach(tab => {
          tab.onclick = () => {
            document.querySelectorAll('.cat-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            const cat = tab.dataset.cat;
            document.querySelectorAll('.tool-card').forEach(card => {
              card.classList.toggle('hidden', cat !== 'all' && card.dataset.cat !== cat);
            });
          };
        });

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
            } else if (activeTool === 'rotate') {
              rotateFile = null;
              pageAngles = {};
              formFields.innerHTML = ROTATE_FIELDS_HTML;
              setupRotateZone();
            } else {
              formFields.innerHTML = SIMPLE_TOOLS[activeTool].fields;
              if (activeTool === 'watermark') setupPositionGrid();
            }
            toolForm.classList.add('open');
          };
        });

        function setupPositionGrid() {
          const hiddenInput = formFields.querySelector('input[name="position"]');
          formFields.querySelectorAll('.pos-btn').forEach(btn => {
            btn.onclick = () => {
              formFields.querySelectorAll('.pos-btn').forEach(b => b.classList.remove('active'));
              btn.classList.add('active');
              if (hiddenInput) hiddenInput.value = btn.dataset.pos;
            };
          });
        }

        // Password show/hide toggle, works for any .pw-toggle button added now or later
        formFields.addEventListener('click', (e) => {
          const btn = e.target.closest('.pw-toggle');
          if (!btn) return;
          const input = btn.closest('.pw-wrap').querySelector('input');
          if (input.type === 'password') {
            input.type = 'text';
            btn.style.opacity = '0.6';
          } else {
            input.type = 'password';
            btn.style.opacity = '1';
          }
        });

        function updateProgress(percent, label) {
          progressContainer.style.display = 'block';
          progressBar.style.width = percent + '%';
          progressText.textContent = label || (percent + '%');
        }

        function submitWithProgress(url, formData, timeoutMs) {
          return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url);
            xhr.responseType = 'blob';
            xhr.timeout = timeoutMs || 180000;

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
          } else if (activeTool === 'rotate') {
            if (!rotateFile) {
              toolStatus.textContent = 'Pilih file PDF dulu.';
              toolStatus.style.color = '#ff6b6b';
              return;
            }
            form.append('file', rotateFile);
            form.append('angles', JSON.stringify(pageAngles));
            endpoint = '/pdf/rotate';
            filename = 'rotated.pdf';
          } else {
            const tool = SIMPLE_TOOLS[activeTool];
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
            endpoint = tool.endpoint;
            filename = tool.filename;
          }

          const toolConfig = (activeTool !== 'merge' && activeTool !== 'rotate') ? SIMPLE_TOOLS[activeTool] : null;
          const timeoutMs = toolConfig && toolConfig.timeout ? toolConfig.timeout : 180000;

          toolSubmit.disabled = true;
          toolStatus.textContent = '';
          updateProgress(0, 'Mengupload... 0%');

          try {
            const blob = await submitWithProgress(endpoint, form, timeoutMs);
            updateProgress(100, '✓ Selesai');
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = filename;
            document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(url);
            toolStatus.textContent = '✓ Selesai, file terunduh.';
            toolStatus.style.color = '#4dff88';
            setTimeout(() => { progressContainer.style.display = 'none'; }, 800);
          } catch (err) {
            toolStatus.textContent = err.message;
            toolStatus.style.color = '#ff6b6b';
            progressContainer.style.display = 'none';
          } finally {
            toolSubmit.disabled = false;
          }
        };
      </script>
    </body></html>
    """
