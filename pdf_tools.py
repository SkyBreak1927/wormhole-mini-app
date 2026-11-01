import io, time, json, zipfile, tempfile, os, asyncio, logging
from contextvars import ContextVar
from collections import defaultdict
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
import pymupdf
from PIL import Image
from pdf2docx import Converter
from auth import get_current_user, is_super_admin
from templating import templates

router = APIRouter()
logger = logging.getLogger(__name__)

# True kalau request ini harus memakai repair PyMuPDF (file terpotong). Dibaca pdf_response
# untuk kirim penanda X-PDF-Repaired ke frontend. Tiap request punya context sendiri.
pdf_repaired: ContextVar[bool] = ContextVar("pdf_repaired", default=False)
REPAIRED_HEADER = "X-PDF-Repaired"

PDF_MAX_SIZE = 100 * 1024 * 1024  # 100 MB per file (naik dari 50MB -- feedback customer, file merge sering lebih gede dari itu)
PDF_RATE_LIMIT = 10
PDF_RATE_WINDOW = 60              # detik
pdf_rate_log = defaultdict(list)  # ip -> [timestamp]


def check_pdf_rate_limit(ip: str, user: dict = None):
    if is_super_admin(user):
        return
    now = time.time()
    pdf_rate_log[ip] = [t for t in pdf_rate_log[ip] if now - t < PDF_RATE_WINDOW]
    if len(pdf_rate_log[ip]) >= PDF_RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Terlalu banyak proses, coba lagi sebentar.")
    pdf_rate_log[ip].append(now)


# TODO: upload dibaca penuh ke RAM lewat file.read() (risiko OOM untuk file besar), dan super
# admin tanpa batas ukuran sama sekali. Pertimbangkan baca bertahap dengan batas keras.
async def read_pdf_upload(file: UploadFile, user: dict = None) -> bytes:
    content = await file.read()
    if not is_super_admin(user) and len(content) > PDF_MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"File melebihi batas {PDF_MAX_SIZE // (1024*1024)}MB")
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="File bukan PDF yang valid.")
    return content


def _reader_from_bytes(content: bytes, password: str = None) -> PdfReader:
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        if not password:
            raise HTTPException(status_code=400, detail="PDF ini terkunci password, isi password dulu.")
        if not reader.decrypt(password):
            raise HTTPException(status_code=400, detail="Password salah.")
    return reader


def safe_read_pdf(content: bytes, password: str = None) -> PdfReader:
    try:
        return _reader_from_bytes(content, password)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("pypdf gagal membaca PDF (%s: %s), mencoba repair via PyMuPDF", type(e).__name__, e)

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
        try:
            repaired = doc.tobytes()
        finally:
            doc.close()
        reader = _reader_from_bytes(repaired, password)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Repair via PyMuPDF gagal (%s: %s)", type(e).__name__, e)
        raise HTTPException(status_code=400, detail="Gagal membaca PDF, file mungkin rusak.")

    pdf_repaired.set(True)
    return reader


def safe_open_pdf_mupdf(content: bytes, password: str = None):
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Gagal membaca PDF, file mungkin rusak.")
    if doc.is_encrypted:
        if not password:
            doc.close()
            raise HTTPException(status_code=400, detail="PDF ini terkunci password, isi password dulu.")
        if not doc.authenticate(password):
            doc.close()
            raise HTTPException(status_code=400, detail="Password salah.")
    return doc


def map_font_to_base14(font_name: str) -> str:
    # PDF bisa pakai font apa saja; kita gak selalu punya akses ke file font aslinya buat
    # nulis ulang teks baru, jadi dipetakan ke salah satu dari 14 font standar PDF yang
    # pymupdf selalu bisa render tanpa perlu embed font tambahan. Hasilnya gak 100% identik
    # ke font asli, tapi ini yang bikin fitur ini jalan tanpa perlu database font eksternal.
    name = (font_name or "").lower()
    bold = "bold" in name
    italic = "italic" in name or "oblique" in name
    if "times" in name or "serif" in name or "georgia" in name or "garamond" in name or "cambria" in name:
        if bold and italic:
            return "tibi"
        if bold:
            return "tibo"
        if italic:
            return "tiit"
        return "tiro"
    if "courier" in name or "mono" in name or "consolas" in name:
        if bold and italic:
            return "cobi"
        if bold:
            return "cobo"
        if italic:
            return "coit"
        return "cour"
    if bold and italic:
        return "hebi"
    if bold:
        return "hebo"
    if italic:
        return "heit"
    return "helv"


def _int_color_to_hex(color_int: int) -> str:
    r = (color_int >> 16) & 255
    g = (color_int >> 8) & 255
    b = color_int & 255
    return f"#{r:02x}{g:02x}{b:02x}"


# Bahasa yang di-OCR: dokumen kerja user campuran Indonesia & Inggris (mis. surat referensi visa).
# Format "ind+eng" ini syntax multi-language-nya Tesseract sendiri.
OCR_LANGUAGES = "ind+eng"


def _ocr_page_to_dict(page) -> dict:
    """
    Jalankan Tesseract OCR di 1 halaman PDF yang gak punya teks asli (hasil scan/gambar),
    lalu balikin struktur yang bentuknya SAMA PERSIS kayak page.get_text("dict") biasa --
    biar bisa lewat jalur parsing blok yang sama persis dengan teks native, gak perlu kode
    kedua. Ini jalan sinkron & bisa makan waktu beberapa detik per halaman (subprocess ke
    Tesseract), makanya dipanggil lewat asyncio.to_thread() dari endpoint biar gak nge-block
    request lain.
    """
    tessdata = os.environ.get("TESSDATA_PREFIX") or None
    try:
        ocr_textpage = page.get_textpage_ocr(
            flags=0, language=OCR_LANGUAGES, dpi=150, full=True, tessdata=tessdata,
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail="OCR belum siap di server ini (Tesseract belum terpasang / tessdata tidak ketemu). Coba lagi nanti atau hubungi admin.",
        ) from e
    return page.get_text("dict", textpage=ocr_textpage)


def _parse_text_blocks(raw: dict, page_index: int, id_prefix: str, ocr: bool = False) -> list:
    """Ubah hasil page.get_text('dict') (native ATAU dari OCR) jadi list blok siap-edit."""
    blocks_out = []
    for b_idx, block in enumerate(raw.get("blocks", [])):
        if block.get("type") != 0:
            continue  # lewati blok gambar, scope versi ini teks doang

        # MuPDF kadang nge-gabung beberapa paragraf/heading yang letaknya berdekatan jadi SATU
        # "block" gede (mis. heading + body paragraf + heading berikutnya). Kalau langsung
        # diratain jadi satu string kayak sebelumnya: (1) baris kosong pemisahnya kebuang abis,
        # jadi jarak antar-paragraf/heading yang tadinya ngasih tinggi ke kotak edit ikut ilang
        # -> pas dirender ulang teksnya numpuk pendek & nyisain kotak putih kosong gede di
        # bawahnya; (2) font/bold cuma diambil dari baris PERTAMA terus dipaksain ke SELURUH
        # isi block -> paragraf biasa ikut kebold kalau baris pertamanya heading bold. Makanya
        # di sini block mentahnya dipecah ulang jadi beberapa "paragraf" terpisah: setiap ada
        # baris kosong (pemisah paragraf yang jelas), ATAU pergantian font/bold antar baris
        # yang nempel langsung tanpa baris kosong (heading nempel langsung ke body-nya), ATAU
        # baris yang lompat jauh ke KANAN dari baris sebelumnya padahal y-nya sebaris (ini
        # tandanya dua KOLOM TABEL yang beda, mis. "Project Name:" | "SEP-41 GUARD" -- kadang
        # label & isinya sama-sama bold jadi font-signature-nya sama persis & gak ketangkep
        # sama pengecekan font di atas; makanya perlu dicek jaraknya juga). Biar tiap
        # paragraf/heading/kolom tabel punya kotak edit, ukuran, & font sendiri-sendiri
        # sesuai aslinya, bukan ketuker gabung ke tetangganya.
        def _representative_span(spans):
            # Jangan asal ambil span PERTAMA buat mewakili gaya satu baris/paragraf --
            # kadang ada bullet/ikon kecil (mis. simbol checkbox '☑') yang nempel duluan
            # di font & ukuran yang beda banget dari isi tulisannya sendiri (mis. font
            # simbol size 12pt, padahal badan tulisannya 11pt). Kalau dipaksa ambil span
            # pertama, ukuran/font ikon kecil itu malah kepake buat SELURUH baris pas
            # dirender ulang -- teksnya jadi kegedean & gampang ke-wrap gara-gara satu
            # karakter ikon doang. Makanya pilih span dengan teks TERPANJANG sebagai
            # wakil gaya baris/paragrafnya, bukan span pertama.
            best, best_len = None, -1
            for sp in spans:
                length = len((sp.get("text") or "").strip())
                if length > best_len:
                    best_len = length
                    best = sp
            return best

        def _line_signature(line):
            spans = line.get("spans", [])
            if not spans:
                return None
            sp = _representative_span(spans) or spans[0]
            font_name = (sp.get("font") or "")
            is_bold = bool(sp.get("flags", 0) & 16) or "bold" in font_name.lower()
            return (font_name, round(sp.get("size", 0.0), 1), is_bold)

        def _same_row_big_gap(prev_line, line):
            prev_bbox = prev_line.get("bbox") if prev_line else None
            cur_bbox = line.get("bbox")
            if not prev_bbox or not cur_bbox:
                return False
            y_overlap = min(prev_bbox[3], cur_bbox[3]) - max(prev_bbox[1], cur_bbox[1])
            row_height = min(prev_bbox[3] - prev_bbox[1], cur_bbox[3] - cur_bbox[1])
            if row_height <= 0 or y_overlap < row_height * 0.5:
                return False  # bukan baris yang sebaris/overlap vertikal -> bukan kolom tabel
            return (cur_bbox[0] - prev_bbox[2]) > 20  # loncat jauh ke kanan -> kolom lain

        # Sebagian PDF (biasanya hasil export dari Word/Google Docs/dsb) nulis baris kosong
        # pemisah paragraf pakai karakter tak-kasat-mata kayak zero-width space (U+200B),
        # bukan spasi biasa. str.strip() Python GAK nganggep itu whitespace, jadi baris
        # "kosong" model gini lolos dari deteksi blank-line di atas & dua paragraf yang
        # harusnya kepisah malah nempel lagi jadi satu blok gede (bug yang sama kayak
        # sebelumnya, cuma nyamar). Makanya dibuang dulu di sini sebelum dicek blank apa
        # nggak, sekalian biar gak nyelip ke teks hasil edit juga.
        _INVISIBLE_CHARS = "​‌‍﻿"

        def _visible(text):
            for ch in _INVISIBLE_CHARS:
                text = text.replace(ch, "")
            return text

        groups = []
        current_group = []
        current_sig = None
        prev_line = None
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            line_text = _visible("".join(s.get("text", "") for s in spans))
            if not line_text.strip():
                if current_group:
                    groups.append(current_group)
                    current_group = []
                    current_sig = None
                prev_line = None
                continue
            sig = _line_signature(line)
            new_column = current_group and _same_row_big_gap(prev_line, line)
            if current_group and (sig != current_sig or new_column):
                groups.append(current_group)
                current_group = []
            current_group.append(line)
            current_sig = sig
            prev_line = line
        if current_group:
            groups.append(current_group)

        for sub_idx, group_lines in enumerate(groups):
            text_parts = []
            all_spans = []
            xs0, ys0, xs1, ys1 = [], [], [], []
            for line in group_lines:
                spans = line.get("spans", [])
                line_text = _visible("".join(s.get("text", "") for s in spans))
                if line_text.strip():
                    text_parts.append(line_text)
                all_spans.extend(spans)
                line_bbox = line.get("bbox")
                if line_bbox:
                    xs0.append(line_bbox[0]); ys0.append(line_bbox[1])
                    xs1.append(line_bbox[2]); ys1.append(line_bbox[3])

            group_text = " ".join(text_parts).strip()
            # font/size/color kotak edit diambil dari span TERPANJANG di grup ini (bukan span
            # pertama) -- biar bullet/ikon kecil kayak checkbox gak nyetir gaya seluruh baris.
            rep_span = _representative_span(all_spans)
            if not group_text or rep_span is None or not xs0:
                continue

            bbox = [round(min(xs0), 2), round(min(ys0), 2), round(max(xs1), 2), round(max(ys1), 2)]
            size = round(rep_span.get("size", 11.0), 2)

            if ocr:
                # Bbox dari Tesseract ngepas ketat ke tinta huruf doang, gak ada ruang
                # ascender/descender kayak font metric PDF asli. Kalau dibiarin, insert_textbox
                # pas apply selalu gagal muat walaupun font-size-nya udah diperkecil ke minimum.
                # Kasih ruang tinggi minimum biar teks pengganti beneran bisa kepasang.
                min_height = round(size * 1.35, 2)
                if (bbox[3] - bbox[1]) < min_height:
                    bbox[3] = round(bbox[1] + min_height, 2)

            blocks_out.append({
                "id": f"p{page_index}_{id_prefix}{b_idx}_{sub_idx}",
                "bbox": bbox,
                "text": group_text,
                "font": rep_span.get("font", "Helvetica"),
                "size": size,
                "color": _int_color_to_hex(rep_span.get("color", 0)),
            })
    return blocks_out


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
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if pdf_repaired.get():
        headers[REPAIRED_HEADER] = "1"
    return StreamingResponse(output, media_type="application/pdf", headers=headers)


@router.post("/pdf/merge")
async def pdf_merge(request: Request, files: List[UploadFile] = File(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host, user)
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Pilih minimal 2 file PDF untuk digabung.")

    writer = PdfWriter()
    for f in files:
        content = await read_pdf_upload(f, user)
        reader = safe_read_pdf(content)
        for page in reader.pages:
            writer.add_page(page)

    return pdf_response(writer, "merged.pdf")


@router.post("/pdf/split")
async def pdf_split(request: Request, file: UploadFile = File(...), pages: str = Form(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host, user)
    content = await read_pdf_upload(file, user)
    reader = safe_read_pdf(content)
    indices = parse_page_range(pages, len(reader.pages))

    writer = PdfWriter()
    for i in indices:
        writer.add_page(reader.pages[i])

    return pdf_response(writer, "extracted.pdf")


@router.post("/pdf/rotate")
async def pdf_rotate(request: Request, file: UploadFile = File(...), angles: str = Form(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host, user)
    content = await read_pdf_upload(file, user)
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
async def pdf_delete_pages(request: Request, file: UploadFile = File(...), pages: str = Form(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host, user)
    content = await read_pdf_upload(file, user)
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
async def pdf_protect(request: Request, file: UploadFile = File(...), password: str = Form(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host, user)
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password minimal 4 karakter.")
    content = await read_pdf_upload(file, user)
    reader = safe_read_pdf(content)

    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)

    return pdf_response(writer, "protected.pdf")


@router.post("/pdf/unlock")
async def pdf_unlock(request: Request, file: UploadFile = File(...), password: str = Form(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host, user)
    content = await read_pdf_upload(file, user)
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
    user: dict = Depends(get_current_user),
):
    check_pdf_rate_limit(request.client.host, user)
    text = text.strip()[:100]
    if not text:
        raise HTTPException(status_code=400, detail="Teks watermark tidak boleh kosong.")
    if position not in WATERMARK_POSITIONS:
        raise HTTPException(status_code=400, detail="Posisi watermark tidak valid.")
    if font_size < 8 or font_size > 200:
        raise HTTPException(status_code=400, detail="Ukuran font harus antara 8-200.")
    r, g, b = parse_hex_color(color)

    content = await read_pdf_upload(file, user)
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
async def pdf_to_jpg(request: Request, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host, user)
    content = await read_pdf_upload(file, user)
    if not is_super_admin(user) and len(content) > CONVERT_MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"File melebihi batas {CONVERT_MAX_SIZE // (1024*1024)}MB untuk konversi.")

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Gagal membaca PDF, file mungkin rusak.")

    if not is_super_admin(user) and doc.page_count > PDF_TO_JPG_MAX_PAGES:
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
async def image_to_pdf(request: Request, images: List[UploadFile] = File(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host, user)
    if not images:
        raise HTTPException(status_code=400, detail="Pilih minimal 1 gambar.")
    if not is_super_admin(user) and len(images) > IMAGE_TO_PDF_MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Maks {IMAGE_TO_PDF_MAX_FILES} gambar sekaligus.")

    pil_images = []
    for img_file in images:
        content = await img_file.read()
        if not is_super_admin(user) and len(content) > CONVERT_MAX_SIZE:
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
async def pdf_to_word(request: Request, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host, user)
    content = await read_pdf_upload(file, user)
    if not is_super_admin(user) and len(content) > CONVERT_MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"File melebihi batas {CONVERT_MAX_SIZE // (1024*1024)}MB untuk konversi.")

    # cek jumlah halaman dulu (murah) sebelum commit ke proses konversi yang berat
    page_count_reader = safe_read_pdf(content)
    if not is_super_admin(user) and len(page_count_reader.pages) > PDF_TO_WORD_MAX_PAGES:
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


COMPRESS_MAX_PAGES = 50
COMPRESS_QUALITY_MAP = {"light": 80, "medium": 60, "max": 35}


@router.post("/pdf/compress")
async def pdf_compress(
    request: Request,
    file: UploadFile = File(...),
    level: str = Form("medium"),
    user: dict = Depends(get_current_user),
):
    check_pdf_rate_limit(request.client.host, user)
    content = await read_pdf_upload(file, user)
    if not is_super_admin(user) and len(content) > CONVERT_MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"File melebihi batas {CONVERT_MAX_SIZE // (1024*1024)}MB.")
    if level not in COMPRESS_QUALITY_MAP:
        raise HTTPException(status_code=400, detail="Level kompresi tidak valid.")
    quality = COMPRESS_QUALITY_MAP[level]

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Gagal membaca PDF, file mungkin rusak.")

    if not is_super_admin(user) and doc.page_count > COMPRESS_MAX_PAGES:
        doc.close()
        raise HTTPException(status_code=400, detail=f"Maks {COMPRESS_MAX_PAGES} halaman untuk kompresi.")

    try:
        for page in doc:
            for img_info in page.get_images(full=True):
                xref = img_info[0]
                try:
                    base_image = doc.extract_image(xref)
                    pil_img = Image.open(io.BytesIO(base_image["image"]))

                    # PDF nyimpen transparansi TERPISAH dari gambar warnanya (soft mask / smask),
                    # beda dari PNG biasa yang alpha-nya nyatu. Kalau ada smask, ambil juga,
                    # baru gabungkan ke background putih -- kalau tidak, area transparan bisa
                    # kelihatan hitam/warna acak (bug nyata yang pernah kejadian, logo jadi kotak hitam).
                    smask_xref = base_image.get("smask", 0)
                    if smask_xref:
                        smask_bytes = doc.extract_image(smask_xref)["image"]
                        alpha_img = Image.open(io.BytesIO(smask_bytes)).convert("L")
                        if alpha_img.size != pil_img.size:
                            alpha_img = alpha_img.resize(pil_img.size)
                        rgb_img = pil_img.convert("RGB")
                        white_bg = Image.new("RGB", rgb_img.size, (255, 255, 255))
                        white_bg.paste(rgb_img, mask=alpha_img)
                        pil_img = white_bg
                    elif pil_img.mode in ("RGBA", "LA") or (pil_img.mode == "P" and "transparency" in pil_img.info):
                        pil_img = pil_img.convert("RGBA")
                        white_bg = Image.new("RGB", pil_img.size, (255, 255, 255))
                        white_bg.paste(pil_img, mask=pil_img.split()[-1])
                        pil_img = white_bg
                    elif pil_img.mode != "RGB":
                        pil_img = pil_img.convert("RGB")

                    out_buf = io.BytesIO()
                    pil_img.save(out_buf, format="JPEG", quality=quality)
                    page.replace_image(xref, stream=out_buf.getvalue())
                except Exception:
                    continue  # kalau 1 gambar gagal diproses, lanjut ke yang lain, jangan gagalkan semuanya

        output = io.BytesIO()
        doc.save(output, garbage=4, deflate=True, clean=True)
        doc.close()
    except HTTPException:
        raise
    except Exception:
        doc.close()
        raise HTTPException(status_code=400, detail="Gagal mengompresi PDF ini.")

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="compressed.pdf"',
            "X-Original-Size": str(len(content)),
            "X-Compressed-Size": str(output.getbuffer().nbytes),
        },
    )


EDIT_TEXT_MAX_PAGES = 30  # render full-size per halaman lebih berat dibanding thumbnail preview biasa
EDIT_TEXT_OCR_MAX_PAGES = 8  # OCR per halaman bisa makan beberapa detik (subprocess Tesseract), batasi biar request gak timeout


@router.post("/pdf/edit/extract")
async def pdf_edit_extract(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(""),
    user: dict = Depends(get_current_user),
):
    check_pdf_rate_limit(request.client.host, user)
    content = await read_pdf_upload(file, user)
    doc = safe_open_pdf_mupdf(content, password or None)

    if not is_super_admin(user) and doc.page_count > EDIT_TEXT_MAX_PAGES:
        doc.close()
        raise HTTPException(status_code=400, detail=f"Maks {EDIT_TEXT_MAX_PAGES} halaman untuk edit teks.")

    pages_out = []
    ocr_pages_used = 0
    try:
        for page_index in range(doc.page_count):
            page = doc[page_index]
            raw = page.get_text("dict")
            blocks_out = _parse_text_blocks(raw, page_index, "b")
            is_ocr_page = False

            if not blocks_out:
                # Halaman gak punya teks asli sama sekali -- kemungkinan hasil scan/gambar.
                # Coba OCR sebagai fallback, biar tetep bisa diedit kayak PDF teks biasa.
                ocr_pages_used += 1
                if not is_super_admin(user) and ocr_pages_used > EDIT_TEXT_OCR_MAX_PAGES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Terlalu banyak halaman hasil scan (maks {EDIT_TEXT_OCR_MAX_PAGES} halaman scan per file), OCR bisa lama kalau kebanyakan.",
                    )
                raw_ocr = await asyncio.to_thread(_ocr_page_to_dict, page)
                blocks_out = _parse_text_blocks(raw_ocr, page_index, "o", ocr=True)
                is_ocr_page = True

            pages_out.append({
                "page_index": page_index,
                "width": round(page.rect.width, 2),
                "height": round(page.rect.height, 2),
                "ocr": is_ocr_page,
                "blocks": blocks_out,
            })
    finally:
        doc.close()

    return {"page_count": len(pages_out), "pages": pages_out}


@router.post("/pdf/edit/apply")
async def pdf_edit_apply(
    request: Request,
    file: UploadFile = File(...),
    password: str = Form(""),
    edits: str = Form(...),
    user: dict = Depends(get_current_user),
):
    check_pdf_rate_limit(request.client.host, user)
    content = await read_pdf_upload(file, user)
    doc = safe_open_pdf_mupdf(content, password or None)

    try:
        edit_list = json.loads(edits)
        if not isinstance(edit_list, list):
            raise ValueError
    except (ValueError, TypeError):
        doc.close()
        raise HTTPException(status_code=400, detail="Data edit tidak valid.")

    if not edit_list:
        doc.close()
        raise HTTPException(status_code=400, detail="Tidak ada perubahan teks untuk diterapkan.")

    edits_by_page = defaultdict(list)
    for e in edit_list:
        try:
            page_index = int(e["page_index"])
            bbox = e["bbox"]
            text = str(e.get("text", ""))
            font = str(e.get("font", "Helvetica"))
            size = float(e.get("size", 11.0))
            color_hex = str(e.get("color", "#000000"))
            new_bottom_y = e.get("new_bottom_y")
            new_bottom_y = float(new_bottom_y) if new_bottom_y is not None else None
        except (KeyError, ValueError, TypeError):
            doc.close()
            raise HTTPException(status_code=400, detail="Data edit tidak valid.")

        if page_index < 0 or page_index >= doc.page_count:
            doc.close()
            raise HTTPException(status_code=400, detail=f"Halaman {page_index + 1} di luar batas dokumen.")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            doc.close()
            raise HTTPException(status_code=400, detail="Posisi teks tidak valid.")

        edits_by_page[page_index].append({
            "bbox": bbox, "text": text, "font": font, "size": size,
            "color": color_hex, "new_bottom_y": new_bottom_y,
        })

    try:
        for page_index, page_edits in edits_by_page.items():
            page = doc[page_index]
            rects_info = []
            for pe in page_edits:
                x0, y0, x1, y1 = pe["bbox"]
                bottom = pe["new_bottom_y"] if (pe["new_bottom_y"] and pe["new_bottom_y"] > y1) else y1
                rect = pymupdf.Rect(x0, y0, x1, bottom)
                page.add_redact_annot(rect, fill=(1, 1, 1))
                rects_info.append((rect, pe))

            page.apply_redactions()  # hapus permanen teks lama di area yang di-redact, baru boleh nulis ulang

            for rect, pe in rects_info:
                if not pe["text"].strip():
                    continue  # dikosongkan user -> cukup dihapus, gak perlu nulis apa-apa
                r, g, b = parse_hex_color(pe["color"])
                fontname = map_font_to_base14(pe["font"])
                fontsize = pe["size"]
                while fontsize >= 6:
                    rc = page.insert_textbox(rect, pe["text"], fontsize=fontsize, fontname=fontname, color=(r, g, b), align=0)
                    if rc >= 0:
                        break
                    fontsize -= 0.5  # teks gak muat di kotak -> kecilin font bertahap sampai muat
    except HTTPException:
        raise
    except Exception:
        doc.close()
        raise HTTPException(status_code=400, detail="Gagal menerapkan perubahan teks.")

    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="text-edited.pdf"'},
    )


@router.get("/pdf-tools", response_class=HTMLResponse)
async def pdf_tools_page(request: Request):
    return templates.TemplateResponse(request, "pdf_tools.html")
