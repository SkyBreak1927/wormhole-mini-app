// Dipakai halaman masuk, daftar, dan lupa password: status (aria-live), pesan error yang
// manusiawi, kirim JSON, hitung mundur kirim ulang, dan tombol lihat-password.
const statusEl = document.getElementById('status');

function setStatus(msg, isError) {
  statusEl.textContent = msg;
  statusEl.className = msg ? (isError ? 'status-error' : 'status-ok') : '';
}

const MSG_OFFLINE = 'Koneksi terputus. Cek internetmu, lalu coba lagi.';
const MSG_SERVER = 'Server lagi bermasalah. Coba lagi sebentar.';
const MSG_BUSY = 'Terlalu banyak percobaan. Tunggu sebentar, lalu coba lagi.';
const MSG_FORM = 'Data belum lengkap atau formatnya belum benar. Periksa lagi isianmu.';

// Pesan dari server yang sudah berbahasa Indonesia dipakai apa adanya; pesan teknis berbahasa
// Inggris (mis. dari Supabase) diganti kalimat yang bisa dipahami.
function humanizeDetail(status, detail, fallback) {
  if (status === 429) return MSG_BUSY;
  if (status >= 500) return MSG_SERVER;
  if (Array.isArray(detail)) return MSG_FORM;
  if (typeof detail !== 'string' || !detail.trim()) return fallback;
  const indonesian = /\b(kamu|silakan|salah|sudah|minimal|kode|akun|sesi|gagal|isi|terlalu|belum|tidak|diubah|dinonaktifkan)\b/i;
  if (indonesian.test(detail)) return detail;
  if (/invalid.*email|email.*(invalid|format)|validate email/i.test(detail)) return 'Format email belum benar.';
  if (/rate limit|too many|seconds|security purposes/i.test(detail)) return MSG_BUSY;
  if (/at least|characters|password/i.test(detail)) return 'Password minimal 6 karakter.';
  return fallback;
}

async function postJSON(url, payload, fallback) {
  let res;
  try {
    res = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  } catch (e) {
    throw new Error(MSG_OFFLINE);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.ok) return data || {};
  throw new Error(humanizeDetail(res.status, data && data.detail, fallback));
}

// Hitung mundur pada tombol kirim ulang; tombol aktif lagi saat habis.
function startCooldown(btn, seconds, label) {
  clearInterval(btn._cooldown);
  let left = seconds;
  btn.disabled = true;
  const tick = () => {
    btn.textContent = left > 0 ? label + ' (' + left + ' dtk)' : label;
    if (left <= 0) { clearInterval(btn._cooldown); btn.disabled = false; }
    left--;
  };
  tick();
  btn._cooldown = setInterval(tick, 1000);
}

// Pesan error hilang begitu pengguna mulai memperbaiki isian.
document.addEventListener('input', () => {
  if (statusEl.classList.contains('status-error')) setStatus('', false);
});

document.querySelectorAll('.pw-toggle').forEach(btn => {
  btn.onclick = () => {
    const input = document.getElementById(btn.dataset.target);
    input.type = input.type === 'password' ? 'text' : 'password';
    btn.style.opacity = input.type === 'password' ? '1' : '0.6';
    btn.setAttribute('aria-pressed', input.type === 'password' ? 'false' : 'true');
  };
});
