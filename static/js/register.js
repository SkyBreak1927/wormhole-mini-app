const stepRegister = document.getElementById('step-register');
const stepVerify = document.getElementById('step-verify');
const resendBtn = document.getElementById('btn-resend');
let pendingEmail = '';

const RESEND_LABEL = 'Kirim ulang kode';
const RESEND_COOLDOWN = 60;

function showStep(step) {
  stepRegister.classList.toggle('active', step === 'register');
  stepVerify.classList.toggle('active', step === 'verify');
  document.getElementById(step === 'register' ? 'reg-email' : 'verify-code').focus();
}

document.getElementById('step-register').addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = document.getElementById('reg-email').value.trim();
  const password = document.getElementById('reg-password').value;
  if (!email || !password) { setStatus('Isi email dan password dulu.', true); return; }
  if (password.length < 6) { setStatus('Password minimal 6 karakter.', true); return; }

  const btn = document.getElementById('btn-register');
  btn.disabled = true;
  setStatus('Memproses...', false);

  try {
    await postJSON('/auth/register', {email, password}, 'Gagal mendaftar. Coba lagi.');
    pendingEmail = email;
    document.getElementById('verify-email').textContent = email;
    setStatus('Kode verifikasi sudah dikirim.', false);
    showStep('verify');
    startCooldown(resendBtn, RESEND_COOLDOWN, RESEND_LABEL);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('step-verify').addEventListener('submit', async (e) => {
  e.preventDefault();
  const code = document.getElementById('verify-code').value.trim();
  if (!code) { setStatus('Isi kode verifikasi dulu.', true); return; }

  const btn = document.getElementById('btn-verify');
  btn.disabled = true;
  setStatus('Memverifikasi...', false);

  try {
    await postJSON('/auth/verify', {email: pendingEmail, code}, 'Kode salah atau sudah kedaluwarsa.');
    setStatus('Berhasil! Mengalihkan ke halaman masuk...', false);
    setTimeout(() => { window.location.href = '/login'; }, 1500);
  } catch (err) {
    setStatus(err.message, true);
    btn.disabled = false;
  }
});

resendBtn.addEventListener('click', async () => {
  setStatus('Mengirim ulang kode...', false);
  startCooldown(resendBtn, RESEND_COOLDOWN, RESEND_LABEL);
  try {
    await postJSON('/auth/resend-verification', {email: pendingEmail}, 'Gagal mengirim ulang kode. Coba lagi.');
    setStatus('Kode baru sudah dikirim ke ' + pendingEmail + '. Cek folder spam kalau belum sampai.', false);
  } catch (err) {
    clearInterval(resendBtn._cooldown);
    resendBtn.disabled = false;
    resendBtn.textContent = RESEND_LABEL;
    setStatus(err.message, true);
  }
});

document.getElementById('btn-change-email').addEventListener('click', () => {
  clearInterval(resendBtn._cooldown);
  document.getElementById('verify-code').value = '';
  setStatus('', false);
  showStep('register');
});
