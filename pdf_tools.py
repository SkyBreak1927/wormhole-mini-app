import io, time, json, zipfile, tempfile, os, asyncio
from collections import defaultdict
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
import pymupdf
from PIL import Image
from pdf2docx import Converter
from auth import get_current_user

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
        # baris kosong (pemisah paragraf yang jelas) ATAU pergantian font/bold antar baris
        # yang nempel langsung tanpa baris kosong (heading nempel langsung ke body-nya),
        # biar tiap paragraf/heading punya kotak edit, ukuran, & font sendiri-sendiri
        # sesuai aslinya, bukan ketuker gabung ke tetangganya.
        def _line_signature(line):
            spans = line.get("spans", [])
            if not spans:
                return None
            sp = spans[0]
            font_name = (sp.get("font") or "")
            is_bold = bool(sp.get("flags", 0) & 16) or "bold" in font_name.lower()
            return (font_name, round(sp.get("size", 0.0), 1), is_bold)

        groups = []
        current_group = []
        current_sig = None
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            line_text = "".join(s.get("text", "") for s in spans)
            if not line_text.strip():
                if current_group:
                    groups.append(current_group)
                    current_group = []
                    current_sig = None
                continue
            sig = _line_signature(line)
            if current_group and sig != current_sig:
                groups.append(current_group)
                current_group = []
            current_group.append(line)
            current_sig = sig
        if current_group:
            groups.append(current_group)

        for sub_idx, group_lines in enumerate(groups):
            text_parts = []
            first_span = None
            xs0, ys0, xs1, ys1 = [], [], [], []
            for line in group_lines:
                spans = line.get("spans", [])
                line_text = "".join(s.get("text", "") for s in spans)
                if line_text.strip():
                    text_parts.append(line_text)
                if first_span is None and spans:
                    first_span = spans[0]
                line_bbox = line.get("bbox")
                if line_bbox:
                    xs0.append(line_bbox[0]); ys0.append(line_bbox[1])
                    xs1.append(line_bbox[2]); ys1.append(line_bbox[3])

            group_text = " ".join(text_parts).strip()
            if not group_text or first_span is None or not xs0:
                continue

            bbox = [round(min(xs0), 2), round(min(ys0), 2), round(max(xs1), 2), round(max(ys1), 2)]
            size = round(first_span.get("size", 11.0), 2)

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
                "font": first_span.get("font", "Helvetica"),
                "size": size,
                "color": _int_color_to_hex(first_span.get("color", 0)),
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
    return StreamingResponse(
        output,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/pdf/merge")
async def pdf_merge(request: Request, files: List[UploadFile] = File(...), user: dict = Depends(get_current_user)):
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
async def pdf_split(request: Request, file: UploadFile = File(...), pages: str = Form(...), user: dict = Depends(get_current_user)):
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
    reader = safe_read_pdf(content)
    indices = parse_page_range(pages, len(reader.pages))

    writer = PdfWriter()
    for i in indices:
        writer.add_page(reader.pages[i])

    return pdf_response(writer, "extracted.pdf")


@router.post("/pdf/rotate")
async def pdf_rotate(request: Request, file: UploadFile = File(...), angles: str = Form(...), user: dict = Depends(get_current_user)):
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
async def pdf_delete_pages(request: Request, file: UploadFile = File(...), pages: str = Form(...), user: dict = Depends(get_current_user)):
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
async def pdf_protect(request: Request, file: UploadFile = File(...), password: str = Form(...), user: dict = Depends(get_current_user)):
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
async def pdf_unlock(request: Request, file: UploadFile = File(...), password: str = Form(...), user: dict = Depends(get_current_user)):
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
    user: dict = Depends(get_current_user),
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
async def pdf_to_jpg(request: Request, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
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
async def image_to_pdf(request: Request, images: List[UploadFile] = File(...), user: dict = Depends(get_current_user)):
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
async def pdf_to_word(request: Request, file: UploadFile = File(...), user: dict = Depends(get_current_user)):
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


COMPRESS_MAX_PAGES = 50
COMPRESS_QUALITY_MAP = {"light": 80, "medium": 60, "max": 35}


@router.post("/pdf/compress")
async def pdf_compress(
    request: Request,
    file: UploadFile = File(...),
    level: str = Form("medium"),
    user: dict = Depends(get_current_user),
):
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
    if len(content) > CONVERT_MAX_SIZE:
        raise HTTPException(status_code=413, detail=f"File melebihi batas {CONVERT_MAX_SIZE // (1024*1024)}MB.")
    if level not in COMPRESS_QUALITY_MAP:
        raise HTTPException(status_code=400, detail="Level kompresi tidak valid.")
    quality = COMPRESS_QUALITY_MAP[level]

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="Gagal membaca PDF, file mungkin rusak.")

    if doc.page_count > COMPRESS_MAX_PAGES:
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
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
    doc = safe_open_pdf_mupdf(content, password or None)

    if doc.page_count > EDIT_TEXT_MAX_PAGES:
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
                if ocr_pages_used > EDIT_TEXT_OCR_MAX_PAGES:
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
    check_pdf_rate_limit(request.client.host)
    content = await read_pdf_upload(file)
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
      #merge-zone,#rotate-zone,#split-zone,#edit-zone{border:2px dashed #555;border-radius:10px;padding:16px;text-align:center;cursor:pointer;
                  font-size:13px;color:#999;margin-top:6px;transition:border-color 0.15s}
      #merge-zone.hover,#rotate-zone.hover,#split-zone.hover,#edit-zone.hover{border-color:#4da3ff;color:#4da3ff}
      #edit-page-nav{display:flex;gap:6px;flex-wrap:wrap}
      .edit-page-btn{padding:5px 12px;border-radius:16px;border:1px solid #444;background:#1a1a1c;
                  color:#ccc;font-size:12px;cursor:pointer}
      .edit-page-btn.active{background:#4da3ff;border-color:#4da3ff;color:#fff}
      #edit-canvas-wrap{border:1px solid #333;border-radius:8px;max-width:100%;overflow:auto}
      .edit-block{transition:outline-color 0.1s,background 0.1s}
      .edit-hotspot{transition:background 0.1s}
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
      #split-thumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:10px;margin-top:12px}
      .split-thumb{position:relative;border:2px solid #333;border-radius:8px;padding:8px 6px;text-align:center;
                    background:#1a1a1c;overflow:hidden;cursor:pointer;transition:border-color 0.15s}
      .split-thumb canvas{max-width:100%;height:auto;display:block;margin:0 auto}
      .split-thumb-label{font-size:10px;color:#888;margin-top:6px}
      .split-thumb.selected{border-color:#4da3ff}
      .split-thumb.selected::after{content:'✓';position:absolute;top:5px;right:5px;width:20px;height:20px;
        border-radius:50%;background:#4da3ff;color:#fff;font-size:12px;line-height:20px;text-align:center}
      #split-note{grid-column:1/-1;font-size:11px;color:#888;margin-top:4px}
      #split-actions{display:flex;gap:8px;margin-top:10px}
      #split-actions button{flex:1;padding:6px;background:#222;border:1px solid #444;border-radius:5px;
        color:#eee;cursor:pointer;font-size:12px}
      #split-actions button:hover{border-color:#4da3ff}
      #split-count{font-size:12px;color:#999;margin-top:8px}
      #split-manual{margin-top:4px}
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
      #user-bar{position:fixed;top:14px;right:14px;display:flex;align-items:center;
        gap:8px;font-size:11px;color:#888;z-index:900}
      #logout-btn{margin:0;padding:5px 12px;background:#2a2a2c;color:#eee;border:1px solid #444;
        border-radius:6px;cursor:pointer;font-size:11px}
      #logout-btn:hover{border-color:#ff6b6b;color:#ff6b6b}
    </style></head><body>
      <a href="/" class="back-link">← Kembali</a>
      <div id="user-bar">
        <span id="user-email-display"></span>
        <button id="logout-btn">Logout</button>
      </div>
      <h2>🛠️ PDF Tools</h2>
      <div class="cat-tabs">
        <div class="cat-tab active" data-cat="all">Semua</div>
        <div class="cat-tab" data-cat="organize">Organize</div>
        <div class="cat-tab" data-cat="optimize">Optimize</div>
        <div class="cat-tab" data-cat="convert">Convert</div>
        <div class="cat-tab" data-cat="security">Security</div>
        <div class="cat-tab" data-cat="edit">Edit</div>
      </div>
      <div class="tool-grid">
        <div class="tool-card" data-tool="merge" data-cat="organize"><span class="tool-icon">🔗</span>Merge</div>
        <div class="tool-card" data-tool="split" data-cat="organize"><span class="tool-icon">✂️</span>Split / Extract</div>
        <div class="tool-card" data-tool="rotate" data-cat="organize"><span class="tool-icon">🔄</span>Rotate</div>
        <div class="tool-card" data-tool="delete" data-cat="organize"><span class="tool-icon">🗑️</span>Hapus Halaman</div>
        <div class="tool-card" data-tool="compress" data-cat="optimize"><span class="tool-icon">🗜️</span>Compress</div>
        <div class="tool-card" data-tool="pdf-to-jpg" data-cat="convert"><span class="tool-icon">🖼️</span>PDF ke JPG</div>
        <div class="tool-card" data-tool="jpg-to-pdf" data-cat="convert"><span class="tool-icon">📄</span>JPG ke PDF</div>
        <div class="tool-card" data-tool="pdf-to-word" data-cat="convert"><span class="tool-icon">📝</span>PDF ke Word</div>
        <div class="tool-card" data-tool="protect" data-cat="security"><span class="tool-icon">🔒</span>Protect</div>
        <div class="tool-card" data-tool="unlock" data-cat="security"><span class="tool-icon">🔓</span>Unlock</div>
        <div class="tool-card" data-tool="watermark" data-cat="edit"><span class="tool-icon">💧</span>Watermark</div>
        <div class="tool-card" data-tool="edit-text" data-cat="edit"><span class="tool-icon">✏️</span>Edit Teks</div>
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
        // --- Auth guard: redirect ke /login kalau belum ada token sama sekali ---
        if (!localStorage.getItem('access_token')) {
          window.location.href = '/login';
        }

        function getAccessToken() { return localStorage.getItem('access_token'); }
        function getRefreshToken() { return localStorage.getItem('refresh_token'); }

        function logoutAndRedirect() {
          localStorage.removeItem('access_token');
          localStorage.removeItem('refresh_token');
          localStorage.removeItem('user_id');
          localStorage.removeItem('user_email');
          window.location.href = '/login';
        }

        async function tryRefreshToken() {
          const refreshToken = getRefreshToken();
          if (!refreshToken) return false;
          try {
            const res = await fetch('/auth/refresh', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({refresh_token: refreshToken})
            });
            if (!res.ok) return false;
            const data = await res.json();
            localStorage.setItem('access_token', data.access_token);
            localStorage.setItem('refresh_token', data.refresh_token);
            return true;
          } catch (e) {
            return false;
          }
        }

        document.getElementById('user-email-display').textContent = localStorage.getItem('user_email') || '';
        document.getElementById('logout-btn').onclick = async () => {
          try {
            await fetch('/auth/logout', {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + getAccessToken()
              },
              body: JSON.stringify({refresh_token: getRefreshToken() || ''})
            });
          } catch (e) {}
          logoutAndRedirect();
        };

        const pdfjsReady = window.pdfjsLib
          ? Promise.resolve()
          : new Promise(resolve => window.addEventListener('pdfjs-ready', resolve, { once: true }));

        const PW_TOGGLE_HTML =
          '<button type="button" class="pw-toggle" title="Lihat/sembunyikan password">👁️</button>';

        const SIMPLE_TOOLS = {
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
          compress: {
            endpoint: '/pdf/compress', filename: 'compressed.pdf',
            fields: '<label>File PDF (maks 50 halaman)</label><input type="file" name="file" accept="application/pdf">' +
                    '<label>Level kompresi</label>' +
                    '<select name="level">' +
                      '<option value="light">Ringan (kualitas gambar paling terjaga)</option>' +
                      '<option value="medium" selected>Sedang (disarankan)</option>' +
                      '<option value="max">Maksimal (ukuran paling kecil)</option>' +
                    '</select>'
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

        const SPLIT_FIELDS_HTML =
          '<label>Pilih file PDF, lalu klik halaman yang mau diambil</label>' +
          '<div id="split-zone">Klik atau drop file PDF di sini<input id="split-file-input" type="file" accept="application/pdf" style="display:none"></div>' +
          '<div id="split-thumbs"></div>' +
          '<div id="split-actions" style="display:none">' +
            '<button type="button" id="split-select-all">Pilih Semua</button>' +
            '<button type="button" id="split-clear">Kosongkan</button>' +
          '</div>' +
          '<p id="split-count"></p>' +
          '<div id="split-manual" style="display:none">' +
            '<label>Halaman tambahan di luar preview (contoh: 45,50-52)</label>' +
            '<input type="text" id="split-manual-input" placeholder="45,50-52">' +
          '</div>';

        const EDIT_TEXT_FIELDS_HTML =
          '<label>Pilih file PDF, lalu klik paragraf yang mau diedit</label>' +
          '<div id="edit-zone">Klik atau drop file PDF di sini<input id="edit-file-input" type="file" accept="application/pdf" style="display:none"></div>' +
          '<div id="edit-password-block" style="display:none">' +
            '<label>Password PDF (file ini terkunci)</label>' +
            '<div class="pw-wrap"><input type="password" id="edit-password">' + PW_TOGGLE_HTML + '</div>' +
            '<button type="button" id="edit-password-retry" style="margin-top:6px;padding:6px 14px;background:#222;' +
              'border:1px solid #444;border-radius:5px;color:#eee;cursor:pointer;font-size:12px">Coba Lagi</button>' +
          '</div>' +
          '<p id="edit-load-status" style="font-size:12px;margin-top:8px"></p>' +
          '<div id="edit-page-nav" style="display:none;margin-top:10px"></div>' +
          '<p id="edit-ocr-notice" style="display:none;font-size:11px;color:#e0b040;margin-top:8px;' +
            'padding:6px 8px;background:rgba(224,176,64,0.1);border:1px solid rgba(224,176,64,0.3);border-radius:5px">' +
            '📄 Halaman ini hasil scan, teksnya dibaca pakai OCR — mungkin ada salah baca, cek ulang sebelum download.</p>' +
          '<div id="edit-canvas-wrap" style="display:none;margin-top:10px"></div>' +
          '<p id="edit-hint" style="font-size:11px;color:#888;margin-top:8px;display:none">' +
            'Klik teks buat edit langsung. Paragraf lain & halaman lain tidak ikut bergeser otomatis — ' +
            'pastikan hasil yang lebih panjang tidak menabrak konten di bawahnya.</p>';

        const MAX_PREVIEW_PAGES = 30;

        let activeTool = null;
        let mergeFiles = [];
        let rotateFile = null;
        let pageAngles = {};
        let rotatePageCount = 0;
        let splitFile = null;
        let selectedPages = {};
        let splitPageCount = 0;
        let editFile = null;
        let editPdfDoc = null;
        let editPagesData = null;
        let editCurrentPage = 0;
        let editEditedBlocks = {};
        let editScale = 1;

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
              const outputScale = window.devicePixelRatio || 1;
              const canvas = document.createElement('canvas');
              canvas.width = Math.floor(viewport.width * outputScale);
              canvas.height = Math.floor(viewport.height * outputScale);
              canvas.style.width = viewport.width + 'px';
              canvas.style.height = viewport.height + 'px';
              const ctx = canvas.getContext('2d');
              const renderTransform = outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null;
              await page.render({ canvasContext: ctx, viewport, transform: renderTransform }).promise;

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

        function updateSplitCount() {
          const countEl = document.getElementById('split-count');
          if (!countEl) return;
          const n = Object.keys(selectedPages).length;
          countEl.textContent = n === 0 ? 'Belum ada halaman dipilih.' : n + ' halaman dipilih.';
        }

        async function loadSplitFile(file) {
          splitFile = file;
          selectedPages = {};
          const thumbsEl = document.getElementById('split-thumbs');
          const actionsEl = document.getElementById('split-actions');
          const manualEl = document.getElementById('split-manual');
          if (!thumbsEl) return;
          thumbsEl.innerHTML = '<p style="grid-column:1/-1;font-size:12px;color:#999">Memuat preview halaman...</p>';
          actionsEl.style.display = 'none';
          manualEl.style.display = 'none';
          updateSplitCount();

          try {
            await pdfjsReady;
            const arrayBuffer = await file.arrayBuffer();
            const pdf = await window.pdfjsLib.getDocument({ data: arrayBuffer }).promise;
            splitPageCount = pdf.numPages;
            const previewCount = Math.min(splitPageCount, MAX_PREVIEW_PAGES);

            thumbsEl.innerHTML = '';
            for (let i = 1; i <= previewCount; i++) {
              const page = await pdf.getPage(i);
              const viewport = page.getViewport({ scale: 0.3 });
              const outputScale = window.devicePixelRatio || 1;
              const canvas = document.createElement('canvas');
              canvas.width = Math.floor(viewport.width * outputScale);
              canvas.height = Math.floor(viewport.height * outputScale);
              canvas.style.width = viewport.width + 'px';
              canvas.style.height = viewport.height + 'px';
              const ctx = canvas.getContext('2d');
              const renderTransform = outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null;
              await page.render({ canvasContext: ctx, viewport, transform: renderTransform }).promise;

              const wrap = document.createElement('div');
              wrap.className = 'split-thumb';
              wrap.dataset.page = String(i - 1);
              wrap.appendChild(canvas);

              const label = document.createElement('div');
              label.className = 'split-thumb-label';
              label.textContent = 'Hal. ' + i;
              wrap.appendChild(label);

              wrap.onclick = () => {
                const idx = parseInt(wrap.dataset.page);
                if (selectedPages[idx]) {
                  delete selectedPages[idx];
                  wrap.classList.remove('selected');
                } else {
                  selectedPages[idx] = true;
                  wrap.classList.add('selected');
                }
                updateSplitCount();
              };

              thumbsEl.appendChild(wrap);
            }

            actionsEl.style.display = 'flex';

            if (splitPageCount > MAX_PREVIEW_PAGES) {
              const note = document.createElement('p');
              note.id = 'split-note';
              note.textContent = 'Preview dibatasi ' + MAX_PREVIEW_PAGES + ' halaman pertama (dokumen ini ' +
                splitPageCount + ' halaman). Untuk halaman setelahnya, isi manual di bawah.';
              thumbsEl.appendChild(note);
              manualEl.style.display = 'block';
            }
          } catch (err) {
            thumbsEl.innerHTML = '<p style="grid-column:1/-1;font-size:12px;color:#ff6b6b">Gagal memuat preview PDF.</p>';
          }
        }

        function setupSplitZone() {
          const zone = document.getElementById('split-zone');
          const input = document.getElementById('split-file-input');
          if (!zone || !input) return;

          zone.onclick = () => input.click();
          input.onchange = () => { if (input.files[0]) loadSplitFile(input.files[0]); };
          zone.ondragover = e => { e.preventDefault(); zone.classList.add('hover'); };
          zone.ondragleave = () => zone.classList.remove('hover');
          zone.ondrop = e => {
            e.preventDefault();
            zone.classList.remove('hover');
            const f = e.dataTransfer.files[0];
            if (f && f.type === 'application/pdf') loadSplitFile(f);
          };

          document.getElementById('split-select-all').onclick = () => {
            document.querySelectorAll('.split-thumb').forEach(wrap => {
              selectedPages[parseInt(wrap.dataset.page)] = true;
              wrap.classList.add('selected');
            });
            updateSplitCount();
          };
          document.getElementById('split-clear').onclick = () => {
            selectedPages = {};
            document.querySelectorAll('.split-thumb').forEach(wrap => wrap.classList.remove('selected'));
            updateSplitCount();
          };
        }

        function mapFontFamily(fontName) {
          const name = (fontName || '').toLowerCase();
          const bold = name.includes('bold');
          const italic = name.includes('italic') || name.includes('oblique');
          let family = 'Helvetica, Arial, sans-serif';
          if (name.includes('times') || name.includes('serif') || name.includes('georgia') || name.includes('garamond')) {
            family = 'Georgia, "Times New Roman", serif';
          } else if (name.includes('courier') || name.includes('mono') || name.includes('consolas')) {
            family = '"Courier New", monospace';
          }
          return { family, weight: bold ? '700' : '400', style: italic ? 'italic' : 'normal' };
        }

        function setEditLoadStatus(msg, isError) {
          const el = document.getElementById('edit-load-status');
          if (!el) return;
          el.textContent = msg;
          el.style.color = isError ? '#ff6b6b' : '#999';
        }

        async function loadEditFile(file, password) {
          editFile = file;
          editPdfDoc = null;
          setEditLoadStatus('Membaca PDF... (kalau ada halaman hasil scan, OCR bisa makan beberapa detik ekstra)', false);
          document.getElementById('edit-page-nav').style.display = 'none';
          document.getElementById('edit-canvas-wrap').style.display = 'none';
          document.getElementById('edit-hint').style.display = 'none';
          document.getElementById('edit-ocr-notice').style.display = 'none';

          const form = new FormData();
          form.append('file', file);
          if (password) form.append('password', password);

          try {
            const res = await fetch('/pdf/edit/extract', {
              method: 'POST',
              headers: { 'Authorization': 'Bearer ' + getAccessToken() },
              body: form,
            });
            const data = await res.json();
            if (!res.ok) {
              if ((data.detail || '').toLowerCase().includes('password')) {
                document.getElementById('edit-password-block').style.display = 'block';
                document.getElementById('edit-password').focus();
              }
              throw new Error(data.detail || 'Gagal membaca PDF');
            }
            document.getElementById('edit-password-block').style.display = 'none';
            editPagesData = data.pages;
            editCurrentPage = 0;
            editEditedBlocks = {};
            setEditLoadStatus('', false);
            renderEditNav();
            await renderEditPage(0);
            document.getElementById('edit-hint').style.display = 'block';
          } catch (err) {
            setEditLoadStatus(err.message, true);
          }
        }

        function renderEditNav() {
          const nav = document.getElementById('edit-page-nav');
          if (!editPagesData || editPagesData.length <= 1) { nav.style.display = 'none'; return; }
          nav.style.display = 'flex';
          nav.innerHTML = editPagesData.map((p, i) =>
            `<button type="button" class="edit-page-btn${i === editCurrentPage ? ' active' : ''}" data-page="${i}">Hal. ${i + 1}</button>`
          ).join('');
          nav.querySelectorAll('.edit-page-btn').forEach(btn => {
            btn.onclick = () => renderEditPage(parseInt(btn.dataset.page));
          });
        }

        async function renderEditPage(pageIndex) {
          editCurrentPage = pageIndex;
          renderEditNav();
          const wrap = document.getElementById('edit-canvas-wrap');
          wrap.style.display = 'block';
          wrap.innerHTML = '<p style="font-size:12px;color:#999;padding:12px">Memuat halaman...</p>';
          document.getElementById('edit-ocr-notice').style.display =
            editPagesData[pageIndex].ocr ? 'block' : 'none';

          await pdfjsReady;
          if (!editPdfDoc) {
            const buf = await editFile.arrayBuffer();
            editPdfDoc = await window.pdfjsLib.getDocument({ data: buf }).promise;
          }
          const pdfPage = await editPdfDoc.getPage(pageIndex + 1);
          const baseViewport = pdfPage.getViewport({ scale: 1 });
          const containerWidth = (toolForm.clientWidth || 480) - 4;
          editScale = Math.min(1.6, Math.max(0.5, containerWidth / baseViewport.width));
          const viewport = pdfPage.getViewport({ scale: editScale });

          // canvas dirender di resolusi asli device (Retina/HiDPI) biar tajam, tapi ukuran
          // tampilnya (CSS px) tetep sama kayak sebelumnya -- jadi posisi hotspot/mask/textbox
          // yang masih pakai satuan editScale gak perlu berubah sama sekali
          const outputScale = window.devicePixelRatio || 1;
          const canvas = document.createElement('canvas');
          canvas.width = Math.floor(viewport.width * outputScale);
          canvas.height = Math.floor(viewport.height * outputScale);
          canvas.style.width = viewport.width + 'px';
          canvas.style.height = viewport.height + 'px';
          const ctx = canvas.getContext('2d');
          const renderTransform = outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null;
          await pdfPage.render({ canvasContext: ctx, viewport, transform: renderTransform }).promise;

          wrap.innerHTML = '';
          wrap.style.position = 'relative';
          wrap.style.width = viewport.width + 'px';
          wrap.style.height = viewport.height + 'px';
          wrap.appendChild(canvas);

          const pageData = editPagesData[pageIndex];
          pageData.blocks.forEach(block => {
            const editState = editEditedBlocks[block.id];
            if (editState) {
              // blok ini udah pernah diedit sebelumnya: mask + teks baru wajib tetep kepasang,
              // biar teks asli yang ketutup gak nongol dobel sama teks hasil editan
              mountEditableBlock(wrap, block, editState.text, editState.new_bottom_y, false, pageData.ocr);
            } else {
              // default: render asli PDF (canvas) dibiarin apa adanya, cuma dikasih
              // "hotspot" tak kasat mata buat nandain area yang bisa diklik buat diedit
              mountBlockHotspot(wrap, block, pageData.ocr);
            }
          });
        }

        function mountBlockHotspot(wrap, block, isOcrBlock) {
          const [x0, y0, x1, y1] = block.bbox;
          const hotspot = document.createElement('div');
          hotspot.className = 'edit-hotspot';
          hotspot.dataset.blockId = block.id;
          hotspot.style.cssText = `position:absolute;left:${x0 * editScale}px;top:${y0 * editScale}px;` +
            `width:${(x1 - x0) * editScale}px;height:${(y1 - y0) * editScale}px;` +
            `background:transparent;cursor:text;`;
          hotspot.onmouseenter = () => { hotspot.style.background = 'rgba(77,163,255,0.10)'; };
          hotspot.onmouseleave = () => { hotspot.style.background = 'transparent'; };
          hotspot.onclick = () => {
            const editState = editEditedBlocks[block.id];
            hotspot.remove();
            mountEditableBlock(wrap, block, editState ? editState.text : block.text, editState ? editState.new_bottom_y : null, true, isOcrBlock);
          };
          wrap.appendChild(hotspot);
        }

        function mountEditableBlock(wrap, block, text, forcedBottom, autofocus, isOcrBlock) {
          const [x0, y0, x1, y1] = block.bbox;
          const bottom = forcedBottom || y1;
          // Blok hasil OCR bbox-nya ngepas ketat ke lebar tulisan asli di gambar scan (yang
          // sering tebal/bold) -- diganti font pengganti generik (Arial/Helvetica reguler) suka
          // dikit lebih lebar & gampang ke-wrap padahal aslinya muat 1 baris. Kasih sedikit ruang
          // ekstra lebar biar gak gampang pecah baris buat hal sepele.
          const widthPad = isOcrBlock ? 1.15 : 1;
          const boxWidth = (x1 - x0) * editScale * widthPad;

          // kotak putih nutupin teks asli, biar teks lama & baru gak keliatan dobel pas edit
          const mask = document.createElement('div');
          mask.className = 'edit-mask';
          mask.dataset.blockId = block.id;
          mask.style.cssText = `position:absolute;left:${x0 * editScale}px;top:${y0 * editScale}px;` +
            `width:${boxWidth}px;height:${(bottom - y0) * editScale}px;background:#fff;`;
          wrap.appendChild(mask);

          const { family, weight, style } = mapFontFamily(block.font);
          // Faktor 0.82 ini koreksi buat font vektor PDF asli yang dirender via CSS (Helvetica/
          // Arial cenderung kegedean dibanding metric PDF point-size aslinya). Blok hasil OCR
          // udah punya font-size perkiraan dari tinggi baris tulisan di gambar scan-nya --
          // kalau dikalikan 0.82 lagi, teksnya jadi keliatan mini & janggal dibanding baris lain
          // yang masih gambar asli. Jadi koreksi ini cuma dipakai buat blok teks native.
          const sizeCorrection = isOcrBlock ? 1 : 0.82;
          const div = document.createElement('div');
          div.className = 'edit-block';
          div.contentEditable = 'true';
          div.spellcheck = false; // biar nama orang/istilah asing gak digarisbawahin merah, ganggu tampilan
          div.dataset.blockId = block.id;
          div.dataset.origX0 = x0; div.dataset.origY0 = y0;
          div.dataset.origX1 = x1; div.dataset.origBottom = y1;
          div.textContent = text;
          div.style.cssText = `position:absolute;left:${x0 * editScale}px;top:${y0 * editScale}px;` +
            `width:${boxWidth}px;min-height:${(y1 - y0) * editScale}px;` +
            `font-family:${family};font-weight:${weight};font-style:${style};` +
            `font-size:${block.size * editScale * sizeCorrection}px;color:${block.color};line-height:1.25;` +
            `outline:none;cursor:text;white-space:pre-wrap;word-break:break-word;padding:1px 2px;`;
          div.onfocus = () => {
            div.style.outline = '1px dashed #4da3ff';
            div.style.background = 'rgba(77,163,255,0.08)';
          };
          // Kalau teksnya kepanjangan/wrapping bikin div lebih tinggi dari mask (ini yang
          // sebelumnya bikin teks scan asli keintip nongol di bawah mask -- bug utamanya), mask
          // WAJIB ikut tumbuh biar teks asli tetep ketutup penuh. Cuma tumbuh, gak pernah nyusut,
          // biar gak balik ngintipin punya sendiri.
          const syncMaskHeight = () => {
            const neededPx = div.offsetHeight;
            if (neededPx > mask.offsetHeight) {
              mask.style.height = neededPx + 'px';
            }
          };
          div.oninput = syncMaskHeight;
          syncMaskHeight(); // langsung sync begitu dipasang, siapa tau teksnya udah wrap dari awal
          div.onblur = () => {
            div.style.outline = 'none';
            div.style.background = 'transparent';
            saveEditBlock(block, div);
            if (!editEditedBlocks[block.id]) {
              // teksnya balik sama kayak aslinya, jadi lepas mode edit &
              // balikin ke render PDF asli (hotspot lagi) biar tampilan gak berubah
              mask.remove();
              div.remove();
              mountBlockHotspot(wrap, block, isOcrBlock);
            }
          };
          wrap.appendChild(div);
          if (autofocus) {
            div.focus();
            const range = document.createRange();
            range.selectNodeContents(div);
            range.collapse(false);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
          }
        }

        function saveEditBlock(block, div) {
          const newText = div.textContent;
          const x0 = parseFloat(div.dataset.origX0);
          const y0 = parseFloat(div.dataset.origY0);
          const x1 = parseFloat(div.dataset.origX1);
          const origBottom = parseFloat(div.dataset.origBottom);
          const renderedHeightPt = div.offsetHeight / editScale;
          const newBottomY = Math.max(origBottom, y0 + renderedHeightPt);

          if (newText.trim() === block.text.trim()) {
            delete editEditedBlocks[block.id];
            return;
          }
          editEditedBlocks[block.id] = {
            page_index: editCurrentPage,
            id: block.id,
            bbox: [x0, y0, x1, origBottom],
            new_bottom_y: newBottomY,
            text: newText,
            font: block.font,
            size: block.size,
            color: block.color,
          };
        }

        function setupEditZone() {
          const zone = document.getElementById('edit-zone');
          const input = document.getElementById('edit-file-input');
          if (!zone || !input) return;

          zone.onclick = () => input.click();
          input.onchange = () => { if (input.files[0]) loadEditFile(input.files[0]); };
          zone.ondragover = e => { e.preventDefault(); zone.classList.add('hover'); };
          zone.ondragleave = () => zone.classList.remove('hover');
          zone.ondrop = e => {
            e.preventDefault();
            zone.classList.remove('hover');
            const f = e.dataTransfer.files[0];
            if (f && f.type === 'application/pdf') loadEditFile(f);
          };

          document.getElementById('edit-password-retry').onclick = () => {
            const pw = document.getElementById('edit-password').value;
            if (editFile) loadEditFile(editFile, pw);
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
            } else if (activeTool === 'split') {
              splitFile = null;
              selectedPages = {};
              formFields.innerHTML = SPLIT_FIELDS_HTML;
              setupSplitZone();
            } else if (activeTool === 'edit-text') {
              editFile = null;
              editPdfDoc = null;
              editPagesData = null;
              editEditedBlocks = {};
              formFields.innerHTML = EDIT_TEXT_FIELDS_HTML;
              setupEditZone();
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

        function submitWithProgress(url, formData, timeoutMs, isRetry) {
          return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url);
            xhr.setRequestHeader('Authorization', 'Bearer ' + getAccessToken());
            xhr.responseType = 'blob';
            xhr.timeout = timeoutMs || 180000;

            xhr.upload.onprogress = (e) => {
              if (e.lengthComputable) {
                const percent = Math.round((e.loaded / e.total) * 100);
                updateProgress(percent, percent < 100 ? ('Mengupload... ' + percent + '%') : 'Memproses di server...');
              }
            };

            xhr.onload = async () => {
              if (xhr.status === 401 && !isRetry) {
                const refreshed = await tryRefreshToken();
                if (refreshed) {
                  try {
                    resolve(await submitWithProgress(url, formData, timeoutMs, true));
                  } catch (e) {
                    reject(e);
                  }
                } else {
                  logoutAndRedirect();
                  reject(new Error('Sesi berakhir, mengalihkan ke login...'));
                }
                return;
              }
              if (xhr.status >= 200 && xhr.status < 300) {
                resolve({
                  blob: xhr.response,
                  originalSize: xhr.getResponseHeader('X-Original-Size'),
                  compressedSize: xhr.getResponseHeader('X-Compressed-Size'),
                });
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
          } else if (activeTool === 'split') {
            if (!splitFile) {
              toolStatus.textContent = 'Pilih file PDF dulu.';
              toolStatus.style.color = '#ff6b6b';
              return;
            }
            const fromThumbs = Object.keys(selectedPages).map(idx => parseInt(idx) + 1);
            const manualInput = document.getElementById('split-manual-input');
            const manualVal = manualInput ? manualInput.value.trim() : '';
            const pagesSpec = fromThumbs.sort((a, b) => a - b).join(',') +
              (manualVal ? (fromThumbs.length ? ',' : '') + manualVal : '');
            if (!pagesSpec) {
              toolStatus.textContent = 'Pilih minimal 1 halaman.';
              toolStatus.style.color = '#ff6b6b';
              return;
            }
            form.append('file', splitFile);
            form.append('pages', pagesSpec);
            endpoint = '/pdf/split';
            filename = 'extracted.pdf';
          } else if (activeTool === 'edit-text') {
            if (!editFile) {
              toolStatus.textContent = 'Pilih file PDF dulu.';
              toolStatus.style.color = '#ff6b6b';
              return;
            }
            const editsPayload = Object.values(editEditedBlocks);
            if (editsPayload.length === 0) {
              toolStatus.textContent = 'Belum ada perubahan teks. Klik salah satu paragraf buat mulai edit.';
              toolStatus.style.color = '#ff6b6b';
              return;
            }
            form.append('file', editFile);
            form.append('edits', JSON.stringify(editsPayload));
            const pwInput = document.getElementById('edit-password');
            if (pwInput && pwInput.value) form.append('password', pwInput.value);
            endpoint = '/pdf/edit/apply';
            filename = 'text-edited.pdf';
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

          toolSubmit.disabled = true;
          toolStatus.textContent = '';
          updateProgress(0, 'Mengupload... 0%');

          const customTools = ['merge', 'rotate', 'split', 'edit-text'];
          const toolConfig = !customTools.includes(activeTool) ? SIMPLE_TOOLS[activeTool] : null;
          const timeoutMs = toolConfig && toolConfig.timeout ? toolConfig.timeout : 180000;

          try {
            const result = await submitWithProgress(endpoint, form, timeoutMs);
            const blob = result.blob;
            updateProgress(100, '✓ Selesai');
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = filename;
            document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(url);

            if (activeTool === 'compress' && result.originalSize && result.compressedSize) {
              const before = parseInt(result.originalSize);
              const after = parseInt(result.compressedSize);
              const pct = Math.round((1 - after / before) * 100);
              const fmtSize = (n) => (n / 1024 / 1024 >= 1) ? (n / 1024 / 1024).toFixed(2) + ' MB' : Math.round(n / 1024) + ' KB';
              toolStatus.textContent = `✓ Selesai, file terunduh. Ukuran turun ${pct}% (${fmtSize(before)} → ${fmtSize(after)})`;
            } else {
              toolStatus.textContent = '✓ Selesai, file terunduh.';
            }
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
