import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from database import engine
from auth import require_super_admin, require_staff, is_super_admin, SUPER_ADMIN_ROLE
from templating import templates

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


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    # Halaman ini cuma cangkang statis; semua data lewat /admin/api/* yang dijaga require_super_admin.
    return templates.TemplateResponse(request, "admin.html")
