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

// Heartbeat: kasih tahu server bahwa halaman ini sedang terbuka (untuk indikator online di panel admin)
let hbStopped = false;
function sendHeartbeat() {
  if (hbStopped) return;
  fetch('/presence/heartbeat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + getAccessToken()},
    body: JSON.stringify({visible: document.visibilityState === 'visible'})
  }).then(r => { if (r.status === 403) hbStopped = true; }).catch(() => {});
}
sendHeartbeat();
setInterval(sendHeartbeat, 30000);
document.addEventListener('visibilitychange', sendHeartbeat);
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
  '<label>Pilih file PDF, lalu klik ikon ↻ di tiap halaman untuk memutar</label>' +
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

  // Div teksnya dikasih CSS padding:1px 2px (liat di bawah) biar tulisan gak nempel
  // pas-pasan ke tepi kotak. Tapi padding itu NAMBAH ukuran kotak yang beneran
  // dirender di layar (2px total tinggi, 4px total lebar) -- kalau mask putihnya
  // disamain persis sama lebar/tinggi box aslinya (tanpa itung padding), sisa 2-4px
  // di pinggir/bawah kotak jadi GAK ketutup, nyisain sliver tipis teks/gambar asli
  // (scan atau PDF asli) keintip nongol pas-pasan langsung di sebelah teks hasil edit.
  // Efeknya keliatan kayak tulisan "kotor"/blur/dobel, atau kayak ada huruf yang
  // "kepotong" -- padahal teks hasil editnya sendiri lengkap, cuma ketutup separuh
  // sama sisa pixel lama di baliknya. Makanya mask WAJIB dilebihin sebesar padding
  // div-nya juga, biar bener-bener nutup penuh area yang dipakai div.
  const padX = 4; // padding kiri+kanan div (2px + 2px)
  const padY = 2; // padding atas+bawah div (1px + 1px)

  // kotak putih nutupin teks asli, biar teks lama & baru gak keliatan dobel pas edit
  const mask = document.createElement('div');
  mask.className = 'edit-mask';
  mask.dataset.blockId = block.id;
  mask.style.cssText = `position:absolute;left:${x0 * editScale}px;top:${y0 * editScale}px;` +
    `width:${boxWidth + padX}px;height:${(bottom - y0) * editScale + padY}px;background:#fff;`;
  wrap.appendChild(mask);

  const { family, weight, style } = mapFontFamily(block.font);
  // Dulu ada faktor 0.82 di sini buat "koreksi" font vektor PDF asli, asumsinya
  // Helvetica/Arial substitute-nya CSS keliatan lebih gede dari ukuran point PDF
  // aslinya. Udah diukur langsung pakai canvas.measureText() dibanding lebar bbox
  // teks asli dari PDF-nya (bukan nebak lagi): ternyata substitute font-nya malah
  // SEDIKIT LEBIH SEMPIT (cuma beda ~2-5%) di kebanyakan dokumen, bukan lebih lebar.
  // Faktor 0.82 (nyusutin 18%) jusru bikin teks hasil edit keliatan mini & gak pas
  // dibanding teks asli di sebelahnya -- makanya dihapus, dipakein ukuran asli PDF-nya.
  const sizeCorrection = 1;
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
  // Baru diukur SESUDAH div beneran nempel di DOM -- div yang belum di-appendChild
  // belum punya layout box sama sekali, jadi offsetHeight-nya selalu kebaca 0 & mask
  // gak pernah ikut tumbuh biarpun teksnya udah wrap ke banyak baris sejak awal dipasang.
  syncMaskHeight();
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
        repaired: xhr.getResponseHeader('X-PDF-Repaired') === '1',
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
    if (result.repaired) {
      toolStatus.textContent += ' Salah satu file tampak terpotong dan sudah diperbaiki otomatis. Periksa hasilnya.';
      toolStatus.style.color = '#e0b040';
    }
    setTimeout(() => { progressContainer.style.display = 'none'; }, 800);
  } catch (err) {
    toolStatus.textContent = err.message;
    toolStatus.style.color = '#ff6b6b';
    progressContainer.style.display = 'none';
  } finally {
    toolSubmit.disabled = false;
  }
};
