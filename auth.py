import math, os, time
from collections import defaultdict
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

# --- Rate limit endpoint yang mengirim email (register, kirim ulang verifikasi, lupa password) ---
# Disimpan di memori (reset saat deploy). Pola sama dengan check_pdf_rate_limit di pdf_tools.py.
# Batas per email dihitung gabungan lintas endpoint, supaya satu alamat tidak bisa dibanjiri
# lewat tiga pintu berbeda. Request yang ditolak tidak dicatat, jadi tidak memperpanjang hukuman.
EMAIL_SEND_COOLDOWN = 60        # detik: maks 1 kali per email per 60 detik
EMAIL_SEND_HOURLY_MAX = 5       # maks 5 kali per email per jam
IP_SEND_HOURLY_MAX = 10         # maks 10 kali per IP per jam
SEND_WINDOW = 3600              # detik
RATE_LIMIT_MESSAGE = "Terlalu sering. Coba lagi dalam beberapa menit."
SWEEP_THRESHOLD = 5000          # bersihkan kunci kedaluwarsa kalau log melebihi ini
email_send_log = defaultdict(list)   # email (lowercase) -> [timestamp]
ip_send_log = defaultdict(list)      # ip -> [timestamp]

supabase: Client = (
    create_client(SUPABASE_URL, SUPABASE_ANON_KEY) if SUPABASE_URL and SUPABASE_ANON_KEY else None
)


def trust_cloudflare() -> bool:
    """CF-Connecting-IP hanya boleh dipercaya kalau SEMUA traffic memang lewat Cloudflare.
    Selama domain Railway (*.up.railway.app) masih bisa diakses langsung, klien bisa memalsukan
    header itu. Set TRUST_CLOUDFLARE=1 di Railway hanya setelah domain dipindah ke Cloudflare."""
    return os.environ.get("TRUST_CLOUDFLARE", "").strip().lower() in ("1", "true", "yes")


def client_ip(request: Request) -> str:
    """IP pengunjung. TRUST_CLOUDFLARE=1: CF-Connecting-IP. Selain itu header itu DIABAIKAN dan
    dipakai entri paling kanan X-Forwarded-For (ditambahkan proxy terdekat, jadi tidak bisa
    dipalsukan klien); terakhir alamat koneksi langsung."""
    if trust_cloudflare():
        cf = request.headers.get("cf-connecting-ip")
        if cf and cf.strip():
            return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff and xff.split(",")[-1].strip():
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _recent(log: dict, key: str, now: float) -> list:
    hits = [t for t in log[key] if now - t < SEND_WINDOW]
    if hits:
        log[key] = hits
    else:
        log.pop(key, None)
    return hits


def check_email_send_limit(request: Request, email: str):
    """Panggil sebelum mengirim email. Melempar 429 kalau kena batas; kalau lolos, mencatat percobaan.
    Berlaku sama untuk email terdaftar maupun tidak, jadi tidak membocorkan status akun."""
    now = time.time()
    key = (email or "").strip().lower()
    ip = client_ip(request)
    if len(email_send_log) + len(ip_send_log) > SWEEP_THRESHOLD:
        for log in (email_send_log, ip_send_log):
            for k in list(log):
                _recent(log, k, now)

    ip_hits = _recent(ip_send_log, ip, now)
    mail_hits = _recent(email_send_log, key, now)
    if len(ip_hits) >= IP_SEND_HOURLY_MAX:
        wait = ip_hits[0] + SEND_WINDOW - now
    elif mail_hits and now - mail_hits[-1] < EMAIL_SEND_COOLDOWN:
        wait = mail_hits[-1] + EMAIL_SEND_COOLDOWN - now
    elif len(mail_hits) >= EMAIL_SEND_HOURLY_MAX:
        wait = mail_hits[0] + SEND_WINDOW - now
    else:
        email_send_log[key].append(now)
        ip_send_log[ip].append(now)
        return
    raise HTTPException(status_code=429, detail=RATE_LIMIT_MESSAGE, headers={"Retry-After": str(max(1, math.ceil(wait)))})


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
async def register(body: RegisterRequest, request: Request):
    require_supabase()
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password minimal 6 karakter.")
    check_email_send_limit(request, body.email)
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
            detail="Email ini sudah terdaftar. Silakan masuk atau gunakan Lupa Password."
        )

    return {"message": "Kode verifikasi sudah dikirim ke email kamu."}


class ResendRequest(BaseModel):
    email: str


@router.post("/auth/resend-verification")
async def resend_verification(body: ResendRequest, request: Request):
    require_supabase()
    check_email_send_limit(request, body.email)
    try:
        supabase.auth.resend({"type": "signup", "email": body.email})
    except Exception:
        pass  # jangan bocorkan status email (sama seperti forgot-password)
    return {"message": "Kalau email itu menunggu verifikasi, kode baru sudah dikirim."}


@router.post("/auth/verify")
async def verify(body: VerifyRequest):
    require_supabase()
    try:
        supabase.auth.verify_otp({"email": body.email, "token": body.code, "type": "signup"})
    except Exception:
        raise HTTPException(status_code=400, detail="Kode salah atau sudah kedaluwarsa.")
    return {"message": "Akun berhasil diverifikasi, silakan masuk."}


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
        raise HTTPException(status_code=401, detail="Sesi sudah tidak valid, silakan masuk ulang.")

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
        raise HTTPException(status_code=401, detail="Belum masuk.")
    token = authorization.removeprefix("Bearer ").strip()

    try:
        user_response = supabase.auth.get_user(token)
        user = user_response.user
    except Exception:
        raise HTTPException(status_code=401, detail="Sesi tidak valid, silakan masuk ulang.")
    if not user:
        raise HTTPException(status_code=401, detail="Sesi tidak valid, silakan masuk ulang.")

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
        raise HTTPException(status_code=401, detail="Sesi sudah kedaluwarsa, silakan masuk ulang.")

    current = {"id": user.id, "email": user.email, "role": role}
    request.state.user = current  # dibaca UsageMiddleware
    return current


SUPER_ADMIN_ROLE = "super_admin"


def is_super_admin(user) -> bool:
    """Pintu pertama semua pengecekan limit/paywall: True = lolos tanpa batasan apa pun.
    Role dibaca dari database (bukan dari input client)."""
    return bool(user) and user.get("role") == SUPER_ADMIN_ROLE


ADMIN_ROLE = "admin"


def is_staff(user) -> bool:
    """admin (pengamat, hak terbatas) atau super_admin. TIDAK membebaskan dari limit/paywall."""
    return bool(user) and user.get("role") in (ADMIN_ROLE, SUPER_ADMIN_ROLE)


async def require_staff(user: dict = Depends(get_current_user)):
    if not is_staff(user):
        raise HTTPException(status_code=403, detail="Akses ditolak.")
    return user


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
    return {"message": "Berhasil keluar."}


@router.post("/auth/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    require_supabase()
    check_email_send_limit(request, body.email)
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
        verified = scoped_client.auth.verify_otp({"email": body.email, "token": body.code, "type": "recovery"})
    except Exception as e:
        print(f"[reset-password] verify_otp gagal: {type(e).__name__}: {e}", flush=True)
        raise HTTPException(
            status_code=400,
            detail="Kode salah atau sudah kedaluwarsa. Setiap kode hanya bisa dipakai sekali; klik Kirim ulang kode untuk minta yang baru.",
        )

    # Kode sudah valid dan terpakai di titik ini. Kalau update password gagal, kode lama
    # tidak bisa dipakai lagi, jadi pesannya harus jelas supaya user tidak mengira kodenya salah.
    try:
        scoped_client.auth.update_user({"password": body.new_password})
    except Exception as e:
        print(f"[reset-password] update_user gagal: {type(e).__name__}: {e}", flush=True)
        msg = str(e).lower()
        if "same_password" in msg or "different from the old password" in msg:
            raise HTTPException(
                status_code=400,
                detail="Password baru sama dengan password lama. Kalau kamu ingat password itu, langsung masuk saja dengan password tersebut, atau minta kode baru dan pakai password yang berbeda.",
            )
        if "weak_password" in msg or "password should" in msg:
            raise HTTPException(
                status_code=400,
                detail="Password terlalu lemah. Coba kombinasi yang lebih panjang, lalu minta kode baru.",
            )
        raise HTTPException(
            status_code=400,
            detail="Kode valid tetapi password gagal diubah. Minta kode baru lalu coba lagi.",
        )

    # Kode email sudah membuktikan kepemilikan akun, jadi langsung masuk dengan sesi yang
    # sama seperti /auth/login. Kalau sesi tidak tersedia, frontend jatuh ke halaman masuk.
    session = getattr(verified, "session", None)
    user = getattr(verified, "user", None)
    if session and user:
        set_session_expiry(user.id)
        return {
            "message": "Password berhasil diubah.",
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "user_id": user.id,
        }
    return {"message": "Password berhasil diubah, silakan masuk dengan password baru."}
