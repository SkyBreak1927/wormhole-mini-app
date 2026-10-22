-- 004_grant_admin.sql
-- Memberi role 'admin' (pengamat: lihat User/Aktivitas/File aktif; tanpa ban, hapus file, audit log).
-- Role hanya bisa diubah lewat SQL langsung (dijaga trigger dari migration 001). Aman diulang.
-- Ganti email sesuai kebutuhan. Hanya mengubah akun yang masih 'user' (tidak menyentuh super_admin).

UPDATE public.profiles
   SET role = 'admin'
 WHERE email = 'ygpna7@gmail.com'
   AND role = 'user';

-- Verifikasi:
-- SELECT email, role FROM public.profiles WHERE role <> 'user';
-- Mencabut role admin:
-- UPDATE public.profiles SET role = 'user' WHERE email = 'ygpna7@gmail.com' AND role = 'admin';
