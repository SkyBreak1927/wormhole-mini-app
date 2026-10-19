-- 002_usage_events.sql
-- Pencatatan aktivitas user (kirim file + tool PDF). Hanya metadata: siapa, jenis aksi,
-- jumlah file, total ukuran. TIDAK menyimpan nama file maupun isi file.
-- Aman dijalankan ulang (idempotent).

CREATE TABLE IF NOT EXISTS public.usage_events (
  id          bigserial PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  kind        text NOT NULL,              -- upload, upload-batch, pdf/merge, pdf/compress, image/to-pdf, ...
  file_count  integer NOT NULL DEFAULT 1,
  bytes       bigint  NOT NULL DEFAULT 0, -- perkiraan total ukuran request (~ ukuran file yang dikirim)
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS usage_events_created_at_idx ON public.usage_events (created_at);
CREATE INDEX IF NOT EXISTS usage_events_user_created_idx ON public.usage_events (user_id, created_at);

-- Tanpa policy = tidak bisa diakses lewat anon key / PostgREST; hanya backend (DATABASE_URL).
ALTER TABLE public.usage_events ENABLE ROW LEVEL SECURITY;

-- Retensi 90 hari dijalankan otomatis oleh backend (loop harian di main.py).
-- Kalau mau hapus manual: DELETE FROM public.usage_events WHERE created_at < now() - interval '90 days';
