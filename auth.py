import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client

router = APIRouter()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")

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


def require_supabase():
    if not supabase:
        raise HTTPException(status_code=503, detail="Auth belum dikonfigurasi di server.")


@router.post("/auth/register")
async def register(body: RegisterRequest):
    require_supabase()
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password minimal 6 karakter.")
    try:
        supabase.auth.sign_up({"email": body.email, "password": body.password})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"message": "Kode verifikasi sudah dikirim ke email kamu."}


@router.post("/auth/verify")
async def verify(body: VerifyRequest):
    require_supabase()
    try:
        supabase.auth.verify_otp({"email": body.email, "token": body.code, "type": "signup"})
    except Exception:
        raise HTTPException(status_code=400, detail="Kode salah atau sudah kadaluarsa.")
    return {"message": "Akun berhasil diverifikasi, silakan login."}


@router.post("/auth/login")
async def login(body: LoginRequest):
    require_supabase()
    try:
        result = supabase.auth.sign_in_with_password({"email": body.email, "password": body.password})
    except Exception:
        raise HTTPException(status_code=401, detail="Email atau password salah.")
    return {"access_token": result.session.access_token, "user_id": result.user.id}
