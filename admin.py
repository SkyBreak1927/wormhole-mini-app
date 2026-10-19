import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from database import engine
from auth import require_super_admin, SUPER_ADMIN_ROLE

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


@router.get("/admin/api/users")
async def list_users(admin: dict = Depends(require_super_admin)):
    require_db()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, email, role, is_banned, created_at FROM profiles ORDER BY created_at DESC")
        ).fetchall()
    return {
        "users": [
            {
                "id": str(r[0]),
                "email": r[1],
                "role": r[2],
                "is_banned": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
            }
            for r in rows
        ]
    }


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
async def list_files(admin: dict = Depends(require_super_admin)):
    out = []
    for token, entry in list(_FILES.items()):
        path = entry.get("path")
        size = os.path.getsize(path) if path and os.path.exists(path) else None
        out.append(
            {
                "token": token,
                "filename": entry.get("filename"),
                "size": size,
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
  #msg{font-size:13px;min-height:18px;margin-bottom:10px}
  a{color:#4da3ff;text-decoration:none;font-size:13px}
</style></head><body>
  <a href="/">&larr; Kembali</a>
  <h2>Panel Super Admin</h2>
  <div class="sub" id="who"></div>
  <div id="msg"></div>
  <div class="tabs">
    <div class="tab active" data-tab="users">User</div>
    <div class="tab" data-tab="files">File aktif</div>
    <div class="tab" data-tab="audit">Audit log</div>
  </div>
  <div id="content"></div>

  <script>
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

    async function showUsers() {
      const {users} = await api('/admin/api/users');
      content.innerHTML = '<table><tr><th>Email</th><th>Role</th><th>Daftar</th><th></th></tr>' + users.map(u =>
        '<tr><td>' + esc(u.email) + '</td><td><span class="badge ' + (u.role === 'super_admin' ? 'sa' : '') + '">' + esc(u.role) + '</span>' +
        (u.is_banned ? ' <span class="badge ban">banned</span>' : '') + '</td><td>' + fmtDate(u.created_at) + '</td><td>' +
        (u.role === 'super_admin' ? '' :
          (u.is_banned ? '<button data-act="unban" data-id="' + esc(u.id) + '">Unban</button>'
                       : '<button class="danger" data-act="ban" data-id="' + esc(u.id) + '">Ban</button>')) +
        '</td></tr>').join('') + '</table>';
      content.querySelectorAll('button[data-act]').forEach(b => b.onclick = async () => {
        try { await api('/admin/api/users/' + b.dataset.id + '/' + b.dataset.act, 'POST'); say('Berhasil.'); showUsers(); }
        catch (e) { say(e.message, true); }
      });
    }

    async function showFiles() {
      const {files} = await api('/admin/api/files');
      if (!files.length) { content.innerHTML = '<p style="color:#888">Tidak ada file aktif.</p>'; return; }
      content.innerHTML = '<table><tr><th>Nama</th><th>Ukuran</th><th>Kadaluarsa</th><th></th></tr>' + files.map(f =>
        '<tr><td>' + esc(f.filename) + '</td><td>' + fmtSize(f.size) + '</td><td>' +
        (f.expires_at ? fmtDate(f.expires_at * 1000) : 'sekali unduh') + '</td><td>' +
        '<button class="danger" data-token="' + esc(f.token) + '">Hapus</button></td></tr>').join('') + '</table>';
      content.querySelectorAll('button[data-token]').forEach(b => b.onclick = async () => {
        try { await api('/admin/api/files/' + b.dataset.token, 'DELETE'); say('File dihapus.'); showFiles(); }
        catch (e) { say(e.message, true); }
      });
    }

    async function showAudit() {
      const {log} = await api('/admin/api/audit');
      content.innerHTML = log.length ? '<table><tr><th>Waktu</th><th>Aktor</th><th>Aksi</th><th>Target</th></tr>' + log.map(l =>
        '<tr><td>' + fmtDate(l.at) + '</td><td>' + esc(l.actor) + '</td><td>' + esc(l.action) + '</td><td>' + esc(l.target) + '</td></tr>').join('') + '</table>'
        : '<p style="color:#888">Belum ada aksi tercatat.</p>';
    }

    const views = {users: showUsers, files: showFiles, audit: showAudit};
    function open(name) {
      document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
      msg.textContent = '';
      views[name]().catch(e => { content.innerHTML = ''; say(e.message, true); });
    }
    document.querySelectorAll('.tab').forEach(t => t.onclick = () => open(t.dataset.tab));
    open('users');
  </script>
</body></html>
"""


@router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    # Halaman ini cuma cangkang statis; semua data lewat /admin/api/* yang dijaga require_super_admin.
    return ADMIN_PAGE
