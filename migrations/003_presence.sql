-- 003_presence.sql
-- Status online/offline user di panel admin (heartbeat dari browser).
-- Hanya menyimpan: kapan terakhir terlihat + apakah tab sedang aktif. Aman dijalankan ulang.

ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS last_seen_visible boolean NOT NULL DEFAULT false;

-- Verifikasi:
-- SELECT email, last_seen_at, last_seen_visible FROM public.profiles ORDER BY last_seen_at DESC NULLS LAST;
