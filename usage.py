import asyncio
from sqlalchemy import text
from database import engine
from auth import is_super_admin

RETENTION_DAYS = 90
PURGE_INTERVAL = 24 * 60 * 60  # detik

# Aksi yang dicatat (hanya POST yang BERHASIL, status < 400).
TRACKED_EXACT = {"/upload", "/upload-batch", "/image/to-pdf"}
TRACKED_PREFIX = ("/pdf/",)


def _is_tracked(method: str, path: str) -> bool:
    if method != "POST":
        return False
    return path in TRACKED_EXACT or path.startswith(TRACKED_PREFIX)


def _write_event(user_id: str, kind: str, file_count: int, size: int):
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO usage_events (user_id, kind, file_count, bytes) "
                    "VALUES (:u, :k, :f, :b)"
                ),
                {"u": user_id, "k": kind, "f": file_count, "b": size},
            )
    except Exception:
        pass  # gagal mencatat tidak boleh mengganggu pemakaian


class UsageMiddleware:
    """Mencatat metadata aktivitas user (jenis aksi, jumlah file, ukuran). Tidak menyimpan
    nama file / isi file. Aktivitas super_admin TIDAK pernah dicatat (invisible)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_tracked(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return

        status = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        try:
            if status["code"] >= 400 or not engine:
                return
            state = scope.get("state") or {}
            user = state.get("user")
            if not user or is_super_admin(user):
                return
            headers = dict(scope.get("headers") or [])
            size = int(headers.get(b"content-length", b"0") or 0)
            file_count = int(state.get("usage_files", 1))
            kind = scope["path"].lstrip("/")
            asyncio.create_task(
                asyncio.to_thread(_write_event, user["id"], kind, file_count, size)
            )
        except Exception:
            pass


def purge_old_usage():
    if not engine:
        return
    try:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM usage_events WHERE created_at < now() - make_interval(days => :d)"),
                {"d": RETENTION_DAYS},
            )
    except Exception:
        pass


async def purge_usage_loop():
    while True:
        await asyncio.to_thread(purge_old_usage)
        await asyncio.sleep(PURGE_INTERVAL)
