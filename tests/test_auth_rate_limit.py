"""Uji rate limit endpoint yang mengirim email (register, resend-verification, forgot-password).

Jalankan: python -m pytest tests -q   (butuh requirements-dev.txt)
Supabase diganti objek palsu dan jam diganti jam palsu, jadi tidak ada email atau jaringan sungguhan.
"""
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auth  # noqa: E402

MSG = "Terlalu sering. Coba lagi dalam beberapa menit."


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeAuth:
    """Menghitung 'email terkirim'; alamat unknown@* melempar error (seperti akun tak terdaftar)."""

    def __init__(self):
        self.sent = []

    def sign_up(self, creds):
        self.sent.append(("register", creds["email"]))
        return SimpleNamespace(user=SimpleNamespace(identities=[object()]))

    def resend(self, creds):
        self.sent.append(("resend", creds["email"]))
        if creds["email"].startswith("unknown@"):
            raise RuntimeError("user not found")

    def reset_password_for_email(self, email):
        self.sent.append(("forgot", email))
        if email.startswith("unknown@"):
            raise RuntimeError("user not found")


@pytest.fixture
def env(monkeypatch):
    clock = FakeClock()
    fake = FakeAuth()
    monkeypatch.setattr(auth, "time", clock)
    monkeypatch.setattr(auth, "supabase", SimpleNamespace(auth=fake))
    auth.email_send_log.clear()
    auth.ip_send_log.clear()
    app = FastAPI()
    app.include_router(auth.router)
    return SimpleNamespace(client=TestClient(app), clock=clock, fake=fake)


def register(env, email, headers=None):
    return env.client.post("/auth/register", json={"email": email, "password": "abcdef"}, headers=headers or {})


def resend(env, email, headers=None):
    return env.client.post("/auth/resend-verification", json={"email": email}, headers=headers or {})


def forgot(env, email, headers=None):
    return env.client.post("/auth/forgot-password", json={"email": email}, headers=headers or {})


ENDPOINTS = [("register", register), ("resend", resend), ("forgot", forgot)]


@pytest.mark.parametrize("name,call", ENDPOINTS)
def test_request_kedua_dalam_60_detik_untuk_email_sama_kena_429(env, name, call):
    assert call(env, "budi@mail.com").status_code == 200
    r = call(env, "budi@mail.com")
    assert r.status_code == 429
    assert r.json()["detail"] == MSG
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert len(env.fake.sent) == 1, "email kedua tidak boleh dikirim"


@pytest.mark.parametrize("name,call", ENDPOINTS)
def test_setelah_60_detik_boleh_lagi(env, name, call):
    assert call(env, "budi@mail.com").status_code == 200
    env.clock.advance(61)
    assert call(env, "budi@mail.com").status_code == 200


def test_email_dinormalisasi_lowercase_dan_trim(env):
    assert register(env, "Budi@Mail.com").status_code == 200
    assert forgot(env, "  budi@mail.com ").status_code == 429


def test_batas_per_email_gabungan_lintas_endpoint(env):
    assert register(env, "budi@mail.com").status_code == 200
    assert resend(env, "budi@mail.com").status_code == 429
    assert forgot(env, "budi@mail.com").status_code == 429


def test_email_lain_tidak_ikut_terblokir(env):
    assert register(env, "budi@mail.com").status_code == 200
    assert register(env, "sari@mail.com").status_code == 200


def test_maksimal_5_kali_per_jam_per_email(env):
    for _ in range(5):
        assert forgot(env, "budi@mail.com").status_code == 200
        env.clock.advance(61)  # lewati cooldown 60 detik, tetap di dalam satu jam (5 x 61 = 305 detik)
    r = forgot(env, "budi@mail.com")
    assert r.status_code == 429 and r.json()["detail"] == MSG
    assert int(r.headers["Retry-After"]) > 60, "tunggu jauh lebih lama dari cooldown biasa"
    env.clock.advance(3600)
    assert forgot(env, "budi@mail.com").status_code == 200


def test_request_ditolak_tidak_memperpanjang_hukuman(env):
    assert forgot(env, "budi@mail.com").status_code == 200
    env.clock.advance(30)
    assert forgot(env, "budi@mail.com").status_code == 429
    env.clock.advance(31)  # 61 detik sejak request yang LOLOS
    assert forgot(env, "budi@mail.com").status_code == 200


def test_maksimal_10_kali_per_jam_per_ip(env):
    for i in range(10):
        assert forgot(env, f"user{i}@mail.com").status_code == 200
    r = forgot(env, "user10@mail.com")
    assert r.status_code == 429 and r.json()["detail"] == MSG
    env.clock.advance(3601)
    assert forgot(env, "user10@mail.com").status_code == 200


def test_ip_memakai_cf_connecting_ip_bila_ada(env):
    for i in range(10):
        assert forgot(env, f"a{i}@mail.com", {"CF-Connecting-IP": "1.1.1.1"}).status_code == 200
    assert forgot(env, "a10@mail.com", {"CF-Connecting-IP": "1.1.1.1"}).status_code == 429
    # IP Cloudflare lain tidak ikut terblokir, dan IP koneksi langsung (testclient) juga bebas
    assert forgot(env, "b0@mail.com", {"CF-Connecting-IP": "2.2.2.2"}).status_code == 200
    assert forgot(env, "c0@mail.com").status_code == 200


def test_ip_memakai_x_forwarded_for_paling_kanan_bila_tanpa_cloudflare(env):
    # entri kiri bisa dipalsukan klien; yang dipakai entri paling kanan (ditambahkan proxy terdekat)
    for i in range(10):
        assert forgot(env, f"a{i}@mail.com", {"X-Forwarded-For": f"9.9.9.{i}, 3.3.3.3"}).status_code == 200
    assert forgot(env, "a10@mail.com", {"X-Forwarded-For": "8.8.8.8, 3.3.3.3"}).status_code == 429


def test_tanpa_header_memakai_alamat_koneksi(env):
    assert auth.client_ip(SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.5"))) == "10.0.0.5"
    assert auth.client_ip(SimpleNamespace(headers={}, client=None)) == "unknown"


def test_tidak_membocorkan_email_terdaftar_atau_tidak(env):
    """Respons resend dan forgot identik untuk email 'ada' dan 'tidak ada', termasuk saat kena limit."""
    for call in (resend, forgot):
        env.clock.advance(3700)
        auth.email_send_log.clear(); auth.ip_send_log.clear()
        ada, tidak = call(env, "ada@mail.com"), call(env, "unknown@mail.com")
        assert ada.status_code == tidak.status_code == 200
        assert ada.json() == tidak.json()
        ada2, tidak2 = call(env, "ada@mail.com"), call(env, "unknown@mail.com")
        assert ada2.status_code == tidak2.status_code == 429
        assert ada2.json() == tidak2.json()


def test_password_pendek_ditolak_tanpa_memakai_jatah(env):
    r = env.client.post("/auth/register", json={"email": "budi@mail.com", "password": "abc"})
    assert r.status_code == 400
    assert register(env, "budi@mail.com").status_code == 200


def test_log_dibersihkan_dari_kunci_kedaluwarsa(env, monkeypatch):
    monkeypatch.setattr(auth, "SWEEP_THRESHOLD", 3)
    for i in range(4):
        forgot(env, f"x{i}@mail.com", {"CF-Connecting-IP": f"5.5.5.{i}"})
    env.clock.advance(3700)
    forgot(env, "baru@mail.com", {"CF-Connecting-IP": "6.6.6.6"})
    assert set(auth.email_send_log) == {"baru@mail.com"}
    assert set(auth.ip_send_log) == {"6.6.6.6"}
