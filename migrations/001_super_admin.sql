-- 001_super_admin.sql
-- Menambah role 'super_admin', kolom ban, audit log admin, dan guard anti-eskalasi role.
-- Aman dijalankan ulang (idempotent). Jalankan di Supabase project wormhole-mini-app.

BEGIN;

-- 1) Izinkan role super_admin
ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_role_check;
ALTER TABLE public.profiles
  ADD CONSTRAINT profiles_role_check CHECK (role = ANY (ARRAY['user'::text, 'admin'::text, 'super_admin'::text]));

-- 2) Kolom ban (dipakai panel admin; super_admin tidak pernah bisa di-ban oleh logika backend)
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS is_banned boolean NOT NULL DEFAULT false;
ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS banned_at timestamptz;

-- 3) Audit log aksi admin
CREATE TABLE IF NOT EXISTS public.admin_audit_log (
  id          bigserial PRIMARY KEY,
  actor_id    uuid NOT NULL,
  actor_email text,
  action      text NOT NULL,
  target      text,
  detail      text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.admin_audit_log ENABLE ROW LEVEL SECURITY;
-- tanpa policy = tidak bisa diakses lewat PostgREST/anon key; backend (koneksi DATABASE_URL) tetap bisa.

-- 4) Guard: kolom role hanya boleh berubah lewat koneksi database langsung (postgres / SQL editor / service_role),
--    BUKAN lewat anon/authenticated (PostgREST). Mencegah user biasa menaikkan role dirinya sendiri.
CREATE OR REPLACE FUNCTION public.guard_profile_role_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.role IS DISTINCT FROM OLD.role
     AND current_user IN ('anon', 'authenticated') THEN
    RAISE EXCEPTION 'Perubahan role tidak diizinkan.';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_guard_profile_role_change ON public.profiles;
CREATE TRIGGER trg_guard_profile_role_change
  BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.guard_profile_role_change();

-- 5) Jadikan akun ini super_admin (satu-satunya cara memberi role ini: SQL langsung)
UPDATE public.profiles
   SET role = 'super_admin', is_banned = false, banned_at = NULL
 WHERE email = 'febymarapandebu01@gmail.com';

COMMIT;

-- Verifikasi:
-- SELECT id, email, role, is_banned FROM public.profiles WHERE role <> 'user';
