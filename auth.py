import os
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Header, Depends, Request
from pydantic import BaseModel
from supabase import create_client, Client
from sqlalchemy import text
from database import engine

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
SESSION_DURATION_DAYS = 7

supabase: Client = (
    create_client(SUPABASE_URL, SUPABASE_ANON_KEY) if SUPABASE_URL and SUPABASE_ANON_KEY else None
)


class RegisterRequest(BaseModel):
    email: str
    password: str


class VerifyRequest(BaseModel):
    email: str
    code: str


class LoginRequest(BaseModel):
    email: str
    password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


def require_supabase():
    if not supabase:
        raise HTTPException(status_code=503, detail="Auth belum dikonfigurasi di server.")


@router.post("/auth/register")
async def register(body: RegisterRequest):
    require_supabase()
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password minimal 6 karakter.")
    try:
        result = supabase.auth.sign_up({"email": body.email, "password": body.password})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Supabase tidak throw error untuk email yang sudah terkonfirmasi sebelumnya
    # (anti email-enumeration by design). Bedakan lewat field 'identities':
    # kosong = email sudah pernah terdaftar & terverifikasi, bukan user baru.
    if result.user and not result.user.identities:
        raise HTTPException(
            status_code=400,
            detail="Email ini sudah terdaftar. Silakan login atau gunakan Lupa Password."
        )

    return {"message": "Kode verifikasi sudah dikirim ke email kamu."}


@router.post("/auth/verify")
async def verify(body: VerifyRequest):
    require_supabase()
    try:
        supabase.auth.verify_otp({"email": body.email, "token": body.code, "type": "signup"})
    except Exception:
        raise HTTPException(status_code=400, detail="Kode salah atau sudah kadaluarsa.")
    return {"message": "Akun berhasil diverifikasi, silakan login."}


def set_session_expiry(user_id: str):
    """Set/perpanjang session_expires_at user itu jadi SESSION_DURATION_DAYS dari sekarang."""
    if not engine:
        return
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DURATION_DAYS)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE profiles SET session_expires_at = :expires_at WHERE id = :user_id"),
            {"expires_at": expires_at, "user_id": user_id},
        )


@router.post("/auth/login")
async def login(body: LoginRequest):
    require_supabase()
    try:
        result = supabase.auth.sign_in_with_password({"email": body.email, "password": body.password})
    except Exception:
        raise HTTPException(status_code=401, detail="Email atau password salah.")

    set_session_expiry(result.user.id)

    return {
        "access_token": result.session.access_token,
        "refresh_token": result.session.refresh_token,
        "user_id": result.user.id,
    }


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/auth/refresh")
async def refresh(body: RefreshRequest):
    require_supabase()
    try:
        result = supabase.auth.refresh_session(body.refresh_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Sesi sudah tidak valid, silakan login ulang.")

    set_session_expiry(result.user.id)

    return {
        "access_token": result.session.access_token,
        "refresh_token": result.session.refresh_token,
        "user_id": result.user.id,
    }


async def get_current_user(request: Request, authorization: str = Header(None)):
    """Dependency: cek token valid DAN belum lewat masa berlaku 7 hari kita sendiri."""
    require_supabase()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Belum login.")
    token = authorization.removeprefix("Bearer ").strip()

    try:
        user_response = supabase.auth.get_user(token)
        user = user_response.user
    except Exception:
        raise HTTPException(status_code=401, detail="Sesi tidak valid, silakan login ulang.")
    if not user:
        raise HTTPException(status_code=401, detail="Sesi tidak valid, silakan login ulang.")

    if not engine:
        raise HTTPException(status_code=503, detail="Database belum dikonfigurasi di server.")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT role, session_expires_at, is_banned FROM profiles WHERE id = :user_id"),
            {"user_id": user.id},
        ).fetchone()

    if not row:
        raise HTTPException(status_code=401, detail="Profil user tidak ditemukan.")

    role, expires_at, is_banned = row

    # Super admin: tidak pernah kena ban dan tidak kena batas sesi 7 hari
    # (token Supabase tetap harus valid -- itu dicek di atas).
    if role == SUPER_ADMIN_ROLE:
        current = {"id": user.id, "email": user.email, "role": role}
        request.state.user = current  # dibaca UsageMiddleware (super admin tidak dicatat)
        return current

    if is_banned:
        raise HTTPException(status_code=403, detail="Akun ini dinonaktifkan.")

    if expires_at is not None and isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is None or expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Sesi sudah kadaluarsa, silakan login ulang.")

    current = {"id": user.id, "email": user.email, "role": role}
    request.state.user = current  # dibaca UsageMiddleware
    return current


SUPER_ADMIN_ROLE = "super_admin"


def is_super_admin(user) -> bool:
    """Pintu pertama semua pengecekan limit/paywall: True = lolos tanpa batasan apa pun.
    Role dibaca dari database (bukan dari input client)."""
    return bool(user) and user.get("role") == SUPER_ADMIN_ROLE


async def require_super_admin(user: dict = Depends(get_current_user)):
    if not is_super_admin(user):
        raise HTTPException(status_code=403, detail="Akses ditolak.")
    return user


@router.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {"email": user["email"], "role": user["role"]}


class HeartbeatRequest(BaseModel):
    visible: bool = True


@router.post("/presence/heartbeat")
async def heartbeat(body: HeartbeatRequest, user: dict = Depends(get_current_user)):
    """Sinyal 'halaman sedang terbuka' dari browser. Super admin TIDAK dicatat (invisible)."""
    if is_super_admin(user) or not engine:
        return {"ok": True}
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE profiles SET last_seen_at = now(), last_seen_visible = :v WHERE id = :id"),
                {"v": body.visible, "id": user["id"]},
            )
    except Exception:
        pass  # gagal mencatat kehadiran tidak boleh mengganggu user
    return {"ok": True}


class LogoutRequest(BaseModel):
    refresh_token: str = ""


@router.post("/auth/logout")
async def logout(body: LogoutRequest, authorization: str = Header(None)):
    require_supabase()
    if authorization and authorization.startswith("Bearer ") and body.refresh_token:
        token = authorization.removeprefix("Bearer ").strip()
        scoped_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        try:
            scoped_client.auth.set_session(token, body.refresh_token)
            scoped_client.auth.sign_out()
        except Exception:
            pass  # tetap anggap berhasil dari sisi user, browser tetap hapus token-nya
    return {"message": "Berhasil logout."}


@router.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest):
    require_supabase()
    try:
        supabase.auth.reset_password_for_email(body.email)
    except Exception:
        pass  # jangan bocorkan apakah email itu terdaftar atau tidak
    return {"message": "Kalau email itu terdaftar, kode reset sudah dikirim."}


@router.post("/auth/reset-password")
async def reset_password(body: ResetPasswordRequest):
    require_supabase()
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password minimal 6 karakter.")

    # instance koneksi terpisah (bukan pakai client global) supaya sesi verifikasi
    # tidak tercampur kalau ada beberapa orang reset password bersamaan
    scoped_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    try:
        scoped_client.auth.verify_otp({"email": body.email, "token": body.code, "type": "recovery"})
        scoped_client.auth.update_user({"password": body.new_password})
    except Exception:
        raise HTTPException(status_code=400, detail="Kode salah, sudah kadaluarsa, atau gagal mengubah password.")
    return {"message": "Password berhasil diubah, silakan login dengan password baru."}
