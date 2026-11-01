let isSuper = false;
const token = () => localStorage.getItem('access_token');
if (!token()) window.location.href = '/login';
document.getElementById('who').textContent = localStorage.getItem('user_email') || '';
const msg = document.getElementById('msg');
const content = document.getElementById('content');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, method) {
  const res = await fetch(path, {method: method || 'GET', headers: {'Authorization': 'Bearer ' + token()}});
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { window.location.href = '/login'; throw new Error('Sesi berakhir'); }
  if (!res.ok) throw new Error(data.detail || ('Gagal (' + res.status + ')'));
  return data;
}
const confirmDlg = document.getElementById('confirm-dlg');
// Dialog konfirmasi: resolve true hanya kalau pengguna menekan tombol OK (Esc/Batal = false).
function confirmAction({title, body, okLabel, danger}) {
  return new Promise(resolve => {
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-body').textContent = body;
    const ok = document.getElementById('confirm-ok');
    ok.textContent = okLabel;
    ok.className = 'btn ' + (danger ? 'btn-danger' : 'btn-primary');
    confirmDlg.returnValue = '';
    confirmDlg.addEventListener('close', () => resolve(confirmDlg.returnValue === 'ok'), {once: true});
    confirmDlg.showModal();
  });
}
function say(t, err) { msg.textContent = t; msg.style.color = err ? '#ff6b6b' : '#4dff88'; }
const fmtSize = b => b == null ? '-' : (b / 1048576).toFixed(1) + ' MB';
const fmtDate = d => d ? new Date(d).toLocaleString() : '-';

let usersTimer = null;
function ago(sec) {
  if (sec == null) return 'belum pernah';
  if (sec < 60) return 'baru saja';
  if (sec < 3600) return Math.floor(sec / 60) + ' menit lalu';
  if (sec < 86400) return Math.floor(sec / 3600) + ' jam lalu';
  return Math.floor(sec / 86400) + ' hari lalu';
}
const LAMP = {
  online: ['#4dff88', 'Online'], idle: ['#ffc94d', 'Idle (tab di background)'],
  offline: ['#555', 'Offline'], hidden: ['transparent', 'tidak dilacak']
};
function lamp(u) {
  const [c, label] = LAMP[u.status];
  if (u.status === 'hidden') return '<span style="color:var(--color-text-muted)">-</span>';
  const glow = u.status === 'offline' ? '' : 'box-shadow:0 0 6px ' + c + ';';
  return '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;background:' + c + ';' + glow + '"></span>' +
    label + (u.status === 'offline' ? ' <span style="color:var(--color-text-muted)">(' + ago(u.last_seen_ago) + ')</span>' : '');
}

async function showUsers() {
  clearInterval(usersTimer);
  const {users} = await api('/admin/api/users');
  const nOn = users.filter(u => u.status === 'online').length;
  const nIdle = users.filter(u => u.status === 'idle').length;
  content.innerHTML =
    '<div class="summary"><div class="stat">Online<b style="color:#4dff88">' + nOn + '</b></div>' +
    '<div class="stat">Idle<b style="color:#ffc94d">' + nIdle + '</b></div>' +
    '<div class="stat">Total user<b>' + users.length + '</b></div></div>' +
    '<table><tr><th>No</th><th>Status</th><th>Email</th><th>Role</th><th>Daftar</th><th></th></tr>' + users.map((u, i) =>
    '<tr><td>' + (i + 1) + '</td><td>' + lamp(u) + '</td><td>' + esc(u.email) + '</td><td><span class="badge ' + (u.role === 'super_admin' ? 'sa' : '') + '">' + esc(u.role) + '</span>' +
    (u.is_banned ? ' <span class="badge ban">banned</span>' : '') + '</td><td>' + fmtDate(u.created_at) + '</td><td>' +
    (u.role === 'super_admin' || !isSuper ? '' :
      (u.is_banned ? '<button class="btn btn-primary btn-sm" data-act="unban" data-id="' + esc(u.id) + '" data-email="' + esc(u.email) + '">Unban</button>'
                   : '<button class="btn btn-danger btn-sm" data-act="ban" data-id="' + esc(u.id) + '" data-email="' + esc(u.email) + '">Ban</button>')) +
    '</td></tr>').join('') + '</table>' +
    '<p style="color:var(--color-text-muted);font-size:12px">Status diperbarui otomatis tiap 30 detik. Offline terdeteksi sekitar 75 detik setelah tab ditutup.</p>';
  content.querySelectorAll('button[data-act]').forEach(b => b.onclick = async () => {
    const ban = b.dataset.act === 'ban';
    const yes = await confirmAction(ban
      ? {title: 'Ban pengguna?', body: b.dataset.email + ' tidak bisa login sampai di-unban.', okLabel: 'Ban', danger: true}
      : {title: 'Unban pengguna?', body: b.dataset.email + ' bisa login lagi.', okLabel: 'Unban', danger: false});
    if (!yes) return;
    try { await api('/admin/api/users/' + b.dataset.id + '/' + b.dataset.act, 'POST'); say('Berhasil.'); showUsers(); }
    catch (e) { say(e.message, true); }
  });
  usersTimer = setInterval(() => {
    if (document.hidden) return;
    showUsers().catch(() => {});
  }, 30000);
}

async function showFiles() {
  const {files} = await api('/admin/api/files');
  if (!files.length) { content.innerHTML = '<p style="color:#888">Tidak ada file aktif.</p>'; return; }
  content.innerHTML = '<table><tr><th>No</th><th>Pengirim</th><th>Nama</th><th>Ukuran</th><th>Kadaluarsa</th><th></th></tr>' + files.map((f, i) =>
    '<tr><td>' + (i + 1) + '</td><td>' + (f.owner ? esc(f.owner) : '<span style="color:var(--color-text-muted)">tidak diketahui</span>') + '</td><td>' + esc(f.filename) + '</td><td>' + fmtSize(f.size) + '</td><td>' +
    (f.expires_at ? fmtDate(f.expires_at * 1000) : 'sekali unduh') + '</td><td>' +
    (isSuper ? '<button class="btn btn-danger btn-sm" data-token="' + esc(f.token) + '" data-name="' + esc(f.filename) + '">Hapus</button>' : '') + '</td></tr>').join('') + '</table>';
  content.querySelectorAll('button[data-token]').forEach(b => b.onclick = async () => {
    const yes = await confirmAction({title: 'Hapus file?', body: '"' + b.dataset.name + '" langsung hilang dan linknya mati. Tidak bisa dibatalkan.', okLabel: 'Hapus', danger: true});
    if (!yes) return;
    try { await api('/admin/api/files/' + b.dataset.token, 'DELETE'); say('File dihapus.'); showFiles(); }
    catch (e) { say(e.message, true); }
  });
}

function toolsText(t) {
  return Object.entries(t).map(([k, v]) => k.replace(/^pdf\//, '').replace(/^image\//, '') + ' ×' + v).join(', ') || '-';
}

function csvCell(v) {
  let t = String(v ?? '');
  if (/^[=+\-@]/.test(t)) t = "'" + t;  // cegah formula injection di Excel
  return '"' + t.replace(/"/g, '""') + '"';
}
function downloadActivity(d) {
  const NL = String.fromCharCode(13, 10);
  const head = ['No', 'Email', 'Kirim file (aksi)', 'File terkirim', 'PDF (aksi)', 'Total ukuran (KB)', 'Detail PDF'];
  const rows = d.users.map((u, i) => [i + 1, u.email, u.share_actions, u.share_files, u.pdf_actions,
    Math.round(u.bytes / 1024), Object.keys(u.tools).length ? toolsText(u.tools) : '']);
  // baris pertama 'sep=;' supaya Excel (termasuk setelan Indonesia) memisahkan kolom dengan benar
  const csv = 'sep=;' + NL + [head, ...rows].map(r => r.map(csvCell).join(';')).join(NL);
  const blob = new Blob(['﻿' + csv], {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'aktivitas_' + d.date + '.csv';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function showActivity(dateVal) {
  const q = dateVal ? '?date=' + encodeURIComponent(dateVal) : '';
  const d = await api('/admin/api/activity' + q);
  const t = d.totals;
  content.innerHTML =
    '<div>Tanggal: <input type="date" id="act-date" value="' + esc(d.date) + '"> ' +
      (d.users.length ? '<button id="act-dl" class="btn btn-secondary btn-sm" style="margin-left:8px">&#11015; Download CSV (Excel)</button>' : '') + '</div>' +
    '<div class="summary">' +
      '<div class="stat">User aktif<b>' + t.active_users + '</b></div>' +
      '<div class="stat">Kirim file<b>' + t.share_actions + 'x (' + t.share_files + ' file)</b></div>' +
      '<div class="stat">Layanan PDF<b>' + t.pdf_actions + 'x</b></div>' +
      '<div class="stat">Total ukuran<b>' + fmtSize(t.bytes) + '</b></div>' +
    '</div>' +
    (d.users.length ? '<table><tr><th>No</th><th>Email</th><th>Kirim file</th><th>PDF</th><th>Total ukuran</th><th>Detail PDF</th></tr>' +
      d.users.map((u, i) => '<tr><td>' + (i + 1) + '</td><td>' + esc(u.email) + '</td><td>' + u.share_actions + 'x (' + u.share_files + ' file)</td><td>' +
        u.pdf_actions + 'x</td><td>' + fmtSize(u.bytes) + '</td><td>' + esc(toolsText(u.tools)) + '</td></tr>').join('') + '</table>'
      : '<p style="color:#888">Belum ada aktivitas di tanggal ini.</p>');
  const dl = document.getElementById('act-dl');
  if (dl) dl.onclick = () => downloadActivity(d);
  document.getElementById('act-date').onchange = e => showActivity(e.target.value).catch(err => say(err.message, true));
}

async function showAudit() {
  const {log} = await api('/admin/api/audit');
  content.innerHTML = log.length ? '<table><tr><th>Waktu</th><th>Aktor</th><th>Aksi</th><th>Target</th></tr>' + log.map(l =>
    '<tr><td>' + fmtDate(l.at) + '</td><td>' + esc(l.actor) + '</td><td>' + esc(l.action) + '</td><td>' + esc(l.target) + '</td></tr>').join('') + '</table>'
    : '<p style="color:#888">Belum ada aksi tercatat.</p>';
}

const views = {users: showUsers, activity: () => showActivity(''), files: showFiles, audit: showAudit};
function open(name) {
  clearInterval(usersTimer);
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === name);
    t.setAttribute('aria-selected', String(t.dataset.tab === name));
  });
  msg.textContent = '';
  views[name]().catch(e => { content.innerHTML = ''; say(e.message, true); });
}
(async () => {
  try {
    const me = await api('/auth/me');
    if (me.role !== 'super_admin' && me.role !== 'admin') { window.location.href = '/'; return; }
    isSuper = me.role === 'super_admin';
    document.getElementById('who').textContent = (localStorage.getItem('user_email') || '') + ' (' + me.role + ')';
    if (!isSuper) document.querySelector('.tab[data-tab="audit"]').remove();
  } catch (e) { return; }
  document.querySelectorAll('.tab').forEach(t => t.onclick = () => open(t.dataset.tab));
  open('users');
})();
