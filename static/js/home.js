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

// Link panel admin muncul hanya untuk super admin (keamanan tetap di backend)
fetch('/auth/me', {headers: {'Authorization': 'Bearer ' + getAccessToken()}})
  .then(r => r.ok ? r.json() : null)
  .then(me => {
    if (me && (me.role === 'super_admin' || me.role === 'admin')) {
      const a = document.createElement('a');
      a.href = '/admin';
      a.textContent = '⚙️ Admin';
      a.className = 'user-bar-link';
      document.getElementById('user-bar').prepend(a);
    }
  })
  .catch(() => {});
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

const drop = document.getElementById('drop');
const fileInput = document.getElementById('file');
const expirySelect = document.getElementById('expiry');
drop.onclick = () => fileInput.click();
drop.ondragover = e => { e.preventDefault(); drop.classList.add('hover'); };
drop.ondragleave = () => drop.classList.remove('hover');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('hover'); handleFiles(e.dataTransfer.files); };
fileInput.onchange = () => handleFiles(fileInput.files);

// Klik di elemen non-teks (kotak drop, tombol bantuan, dll) bikin browser mindahin fokus ke
// elemen itu — begitu fokusnya bukan di body/input/textarea, event 'paste' sering nggak kebaca
// sama sekali walau user udah pencet Ctrl+V. Jadi balikin fokus ke halaman tiap kali klik di luar
// elemen yang memang butuh diketik, biar paste selalu nyampe ke listener di bawah.
document.addEventListener('click', e => {
  const el = document.activeElement;
  const tag = el && el.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (el && el.isContentEditable)) return;
  if (el && el.blur) el.blur();
});

// Tempel (Ctrl+V/Cmd+V) file atau gambar yang sudah dicopy, langsung upload lewat jalur yang sama.
// Diabaikan kalau user sedang ngetik di input/textarea lain (misal kotak lapor bug) dan clipboard-nya cuma teks.
document.addEventListener('paste', e => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;

  const files = [];
  for (const item of items) {
    if (item.kind === 'file') {
      const f = item.getAsFile();
      if (f) files.push(f);
    }
  }
  if (files.length === 0) return;

  const activeTag = document.activeElement && document.activeElement.tagName;
  if (activeTag === 'INPUT' || activeTag === 'TEXTAREA') return;

  e.preventDefault();
  handleFiles(files);
});

function showResult(link) {
  document.getElementById('result').innerHTML =
    `<p>Link:</p><input type="text" value="${link}" readonly onclick="this.select()">
     <br><button id="copybtn" class="btn btn-primary" onclick="copyLink('${link}')">Salin Link</button>
     <div id="qrcode" style="margin-top:14px;display:inline-block;background:#fff;padding:10px;border-radius:8px"></div>
     <p style="color:#888;font-size:12px;margin-top:6px">Scan pakai kamera HP (Android/iOS) buat buka link ini</p>`;
  new QRCode(document.getElementById('qrcode'), {
    text: link,
    width: 160,
    height: 160,
    colorDark: "#000000",
    colorLight: "#ffffff",
    correctLevel: QRCode.CorrectLevel.M
  });
}

function copyLink(link) {
  navigator.clipboard.writeText(link);
  const btn = document.getElementById('copybtn');
  btn.textContent = "✓ Tersalin!";
  setTimeout(() => { btn.textContent = "Salin Link"; }, 1500);
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

function uploadWithProgress(url, formData, isRetry) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("Authorization", "Bearer " + getAccessToken());
    xhr.timeout = 180000;

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        updateProgress(Math.round((e.loaded / e.total) * 100));
      }
    };

    xhr.onload = async () => {
      if (xhr.status === 401 && !isRetry) {
        const refreshed = await tryRefreshToken();
        if (refreshed) {
          try {
            resolve(await uploadWithProgress(url, formData, true));
          } catch (e) {
            reject(e);
          }
        } else {
          logoutAndRedirect();
          reject(new Error("Sesi berakhir, mengalihkan ke halaman masuk..."));
        }
        return;
      }
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
    drop.textContent = "Tarik file (bisa lebih dari 1) ke sini, atau klik untuk pilih";
  }
}

// --- FAQ widget ---
const helpBtn = document.getElementById('help-btn');
const helpPanel = document.getElementById('help-panel');
const helpClose = document.getElementById('help-close');

helpBtn.onclick = () => helpPanel.classList.toggle('open');
helpClose.onclick = () => helpPanel.classList.remove('open');

document.querySelectorAll('.faq-q').forEach(q => {
  q.onclick = () => q.setAttribute('aria-expanded', String(q.parentElement.classList.toggle('open')));
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
