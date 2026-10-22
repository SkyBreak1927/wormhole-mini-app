import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from database import engine
from auth import require_super_admin, require_staff, is_super_admin, SUPER_ADMIN_ROLE

router = APIRouter()

# Diisi dari main.py (init_admin) supaya panel bisa lihat/hapus file yang sedang aktif
# tanpa import melingkar.
_FILES: dict = {}


def init_admin(files: dict):
    global _FILES
    _FILES = files


def require_db():
    if not engine:
        raise HTTPException(status_code=503, detail="Database belum dikonfigurasi di server.")


def log_action(actor: dict, action: str, target: str = "", detail: str = ""):
    """Catat aksi admin ke admin_audit_log. Gagal mencatat tidak boleh menggagalkan aksinya."""
    if not engine:
        return
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO admin_audit_log (actor_id, actor_email, action, target, detail) "
                    "VALUES (:actor_id, :actor_email, :action, :target, :detail)"
                ),
                {
                    "actor_id": actor["id"],
                    "actor_email": actor.get("email"),
                    "action": action,
                    "target": target,
                    "detail": detail[:500],
                },
            )
    except Exception:
        pass


ONLINE_WINDOW_SECONDS = 75  # 2 heartbeat (tiap 30 dtk) + toleransi


@router.get("/admin/api/users")
async def list_users(admin: dict = Depends(require_staff)):
    require_db()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, email, role, is_banned, created_at, last_seen_visible, "
                "EXTRACT(EPOCH FROM (now() - last_seen_at)) FROM profiles ORDER BY created_at DESC"
            )
        ).fetchall()
    users = []
    for r in rows:
        if r[2] == SUPER_ADMIN_ROLE and not is_super_admin(admin):
            continue  # admin biasa tidak melihat akun super admin
        ago = float(r[6]) if r[6] is not None else None
        if r[2] == "super_admin":
            status = "hidden"  # super admin tidak dilacak
        elif ago is None:
            status = "offline"
        elif ago <= ONLINE_WINDOW_SECONDS:
            status = "online" if r[5] else "idle"
        else:
            status = "offline"
        users.append(
            {
                "id": str(r[0]),
                "email": r[1],
                "role": r[2],
                "is_banned": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
                "status": status,
                "last_seen_ago": int(ago) if ago is not None else None,
            }
        )
    order = {"online": 0, "idle": 1, "offline": 2, "hidden": 3}
    users.sort(key=lambda u: (order[u["status"]], u["last_seen_ago"] if u["last_seen_ago"] is not None else 10**12))
    return {"users": users}


def _set_ban(user_id: str, banned: bool, admin: dict):
    require_db()
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Tidak bisa mengubah status akun sendiri.")
    with engine.begin() as conn:
        target = conn.execute(
            text("SELECT email, role FROM profiles WHERE id = :id"), {"id": user_id}
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User tidak ditemukan.")
        if target[1] == SUPER_ADMIN_ROLE:
            raise HTTPException(status_code=403, detail="Super admin tidak bisa di-ban.")
        conn.execute(
            text(
                "UPDATE profiles SET is_banned = :b, banned_at = CASE WHEN :b THEN now() ELSE NULL END "
                "WHERE id = :id"
            ),
            {"b": banned, "id": user_id},
        )
    log_action(admin, "ban_user" if banned else "unban_user", target=target[0] or user_id)
    return {"ok": True}


@router.post("/admin/api/users/{user_id}/ban")
async def ban_user(user_id: str, admin: dict = Depends(require_super_admin)):
    return _set_ban(user_id, True, admin)


@router.post("/admin/api/users/{user_id}/unban")
async def unban_user(user_id: str, admin: dict = Depends(require_super_admin)):
    return _set_ban(user_id, False, admin)


@router.get("/admin/api/files")
async def list_files(admin: dict = Depends(require_staff)):
    out = []
    for token, entry in list(_FILES.items()):
        if entry.get("owner_super") and not is_super_admin(admin):
            continue  # file milik super admin tidak tampil untuk admin biasa
        path = entry.get("path")
        size = os.path.getsize(path) if path and os.path.exists(path) else None
        out.append(
            {
                "token": token,
                "filename": entry.get("filename"),
                "size": size,
                "owner": entry.get("owner"),
                "expires_at": entry.get("expires_at"),
            }
        )
    return {"files": out}


@router.delete("/admin/api/files/{token}")
async def delete_file(token: str, admin: dict = Depends(require_super_admin)):
    entry = _FILES.pop(token, None)
    if not entry:
        raise HTTPException(status_code=404, detail="File tidak ditemukan.")
    if entry.get("path") and os.path.exists(entry["path"]):
        os.remove(entry["path"])
    log_action(admin, "delete_file", target=entry.get("filename") or token)
    return {"ok": True}


@router.get("/admin/api/audit")
async def audit_log(admin: dict = Depends(require_super_admin)):
    require_db()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT created_at, actor_email, action, target FROM admin_audit_log "
                "ORDER BY id DESC LIMIT 100"
            )
        ).fetchall()
    return {
        "log": [
            {"at": r[0].isoformat(), "actor": r[1], "action": r[2], "target": r[3]} for r in rows
        ]
    }


@router.get("/admin/api/activity")
async def activity(date: str = "", admin: dict = Depends(require_staff)):
    """Ringkasan aktivitas per user untuk satu hari (zona waktu Asia/Jakarta). Default: hari ini."""
    require_db()
    day = date.strip() or None
    if day:
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Format tanggal harus YYYY-MM-DD.")
    with engine.connect() as conn:
        if not day:
            day = conn.execute(text("SELECT (now() AT TIME ZONE 'Asia/Jakarta')::date::text")).scalar()
        rows = conn.execute(
            text(
                "SELECT p.email, e.user_id::text, e.kind, count(*) AS n, "
                "       coalesce(sum(e.file_count), 0) AS files, coalesce(sum(e.bytes), 0) AS bytes "
                "FROM usage_events e JOIN profiles p ON p.id = e.user_id "
                "WHERE (e.created_at AT TIME ZONE 'Asia/Jakarta')::date = CAST(:d AS date) "
                "GROUP BY p.email, e.user_id, e.kind"
            ),
            {"d": day},
        ).fetchall()

    users = {}
    for email, uid, kind, n, files, size in rows:
        u = users.setdefault(
            uid,
            {"email": email, "share_actions": 0, "share_files": 0, "pdf_actions": 0, "bytes": 0, "tools": {}},
        )
        u["bytes"] += int(size)
        if kind in ("upload", "upload-batch"):
            u["share_actions"] += int(n)
            u["share_files"] += int(files)
        else:
            u["pdf_actions"] += int(n)
            u["tools"][kind] = u["tools"].get(kind, 0) + int(n)

    result = sorted(users.values(), key=lambda u: (u["share_actions"] + u["pdf_actions"]), reverse=True)
    return {
        "date": day,
        "users": result,
        "totals": {
            "active_users": len(result),
            "share_actions": sum(u["share_actions"] for u in result),
            "share_files": sum(u["share_files"] for u in result),
            "pdf_actions": sum(u["pdf_actions"] for u in result),
            "bytes": sum(u["bytes"] for u in result),
        },
    }


ADMIN_PAGE = """
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>
  body{background:#0f0f10;color:#eee;font-family:sans-serif;margin:0;padding:24px 16px;max-width:860px;margin:0 auto}
  h2{margin:0 0 4px} .sub{color:#888;font-size:13px;margin-bottom:18px}
  .tabs{display:flex;gap:8px;margin-bottom:16px}
  .tab{padding:8px 14px;background:#1a1a1c;border:1px solid #333;border-radius:6px;cursor:pointer;font-size:13px}
  .tab.active{border-color:#4da3ff;color:#4da3ff}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #2a2a2c;word-break:break-all}
  th{color:#888;font-weight:normal}
  button{padding:5px 10px;border:none;border-radius:6px;background:#4da3ff;color:#fff;cursor:pointer;font-size:12px}
  button.danger{background:#ff6b6b}
  .badge{font-size:11px;padding:2px 6px;border-radius:4px;background:#2a2a2c}
  .badge.sa{background:#4da3ff;color:#fff} .badge.ban{background:#ff6b6b;color:#fff}
  #msg{font-size:13px;min-height:18px;margin-bottom:10px}\n  .summary{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}\n  .stat{background:#1a1a1c;border:1px solid #2a2a2c;border-radius:8px;padding:10px 14px;font-size:12px;color:#888}\n  .stat b{display:block;color:#eee;font-size:18px;margin-top:2px}\n  input[type=date]{background:#1a1a1c;color:#eee;border:1px solid #444;border-radius:6px;padding:6px 8px}
  a{color:#4da3ff;text-decoration:none;font-size:13px}
</style></head><body>
  <a href="/">&larr; Kembali</a>
  <h2>Panel Admin</h2>
  <div class="sub" id="who"></div>
  <div id="msg"></div>
  <div class="tabs">
    <div class="tab active" data-tab="users">User</div>
    <div class="tab" data-tab="activity">Aktivitas</div>
    <div class="tab" data-tab="files">File aktif</div>
    <div class="tab" data-tab="audit">Audit log</div>
  </div>
  <div id="content"></div>

  <script>
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
      if (u.status === 'hidden') return '<span style="color:#666">-</span>';
      const glow = u.status === 'offline' ? '' : 'box-shadow:0 0 6px ' + c + ';';
      return '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;background:' + c + ';' + glow + '"></span>' +
        label + (u.status === 'offline' ? ' <span style="color:#666">(' + ago(u.last_seen_ago) + ')</span>' : '');
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
          (u.is_banned ? '<button data-act="unban" data-id="' + esc(u.id) + '">Unban</button>'
                       : '<button class="danger" data-act="ban" data-id="' + esc(u.id) + '">Ban</button>')) +
        '</td></tr>').join('') + '</table>' +
        '<p style="color:#666;font-size:12px">Status diperbarui otomatis tiap 30 detik. Offline terdeteksi sekitar 75 detik setelah tab ditutup.</p>';
      content.querySelectorAll('button[data-act]').forEach(b => b.onclick = async () => {
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
        '<tr><td>' + (i + 1) + '</td><td>' + (f.owner ? esc(f.owner) : '<span style="color:#666">tidak diketahui</span>') + '</td><td>' + esc(f.filename) + '</td><td>' + fmtSize(f.size) + '</td><td>' +
        (f.expires_at ? fmtDate(f.expires_at * 1000) : 'sekali unduh') + '</td><td>' +
        (isSuper ? '<button class="danger" data-token="' + esc(f.token) + '">Hapus</button>' : '') + '</td></tr>').join('') + '</table>';
      content.querySelectorAll('button[data-token]').forEach(b => b.onclick = async () => {
        try { await api('/admin/api/files/' + b.dataset.token, 'DELETE'); say('File dihapus.'); showFiles(); }
        catch (e) { say(e.message, true); }
      });
    }

    function toolsText(t) {
      return Object.entries(t).map(([k, v]) => k.replace(/^pdf\\//, '').replace(/^image\\//, '') + ' ×' + v).join(', ') || '-';
    }

    function csvCell(v) {
      let t = String(v ?? '');
      if (/^[=+\\-@]/.test(t)) t = "'" + t;  // cegah formula injection di Excel
      return '"' + t.replace(/"/g, '""') + '"';
    }
    function downloadActivity(d) {
      const NL = String.fromCharCode(13, 10);
      const head = ['No', 'Email', 'Kirim file (aksi)', 'File terkirim', 'PDF (aksi)', 'Total ukuran (KB)', 'Detail PDF'];
      const rows = d.users.map((u, i) => [i + 1, u.email, u.share_actions, u.share_files, u.pdf_actions,
        Math.round(u.bytes / 1024), Object.keys(u.tools).length ? toolsText(u.tools) : '']);
      // baris pertama 'sep=;' supaya Excel (termasuk setelan Indonesia) memisahkan kolom dengan benar
      const csv = 'sep=;' + NL + [head, ...rows].map(r => r.map(csvCell).join(';')).join(NL);
      const blob = new Blob(['\ufeff' + csv], {type: 'text/csv;charset=utf-8'});
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
          (d.users.length ? '<button id="act-dl" style="margin-left:8px">&#11015; Download CSV (Excel)</button>' : '') + '</div>' +
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
      document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
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
  </script>
</body></html>
"""


@router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    # Halaman ini cuma cangkang statis; semua data lewat /admin/api/* yang dijaga require_super_admin.
    return ADMIN_PAGE
