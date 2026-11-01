const stepRequest = document.getElementById('step-request');
const stepReset = document.getElementById('step-reset');
const resendBtn = document.getElementById('btn-resend');
let pendingEmail = '';

const RESEND_LABEL = 'Kirim ulang kode';
const RESEND_COOLDOWN = 60;

function showStep(step) {
  stepRequest.classList.toggle('active', step === 'request');
  stepReset.classList.toggle('active', step === 'reset');
  document.getElementById(step === 'request' ? 'fp-email' : 'fp-code').focus();
}

document.getElementById('step-request').addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = document.getElementById('fp-email').value.trim();
  if (!email) { setStatus('Isi email dulu.', true); return; }

  const btn = document.getElementById('btn-request');
  btn.disabled = true;
  setStatus('Memproses...', false);

  try {
    const data = await postJSON('/auth/forgot-password', {email}, 'Gagal mengirim kode. Coba lagi.');
    pendingEmail = email;
    document.getElementById('reset-email').textContent = email;
    setStatus(data.message || 'Kode reset sudah dikirim.', false);
    showStep('reset');
    startCooldown(resendBtn, RESEND_COOLDOWN, RESEND_LABEL);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('step-reset').addEventListener('submit', async (e) => {
  e.preventDefault();
  const code = document.getElementById('fp-code').value.trim();
  const newPassword = document.getElementById('fp-new-password').value;
  if (!code || !newPassword) { setStatus('Isi kode dan password baru dulu.', true); return; }
  if (newPassword.length < 6) { setStatus('Password minimal 6 karakter.', true); return; }

  const btn = document.getElementById('btn-reset');
  btn.disabled = true;
  setStatus('Memproses...', false);

  try {
    await postJSON('/auth/reset-password', {email: pendingEmail, code, new_password: newPassword}, 'Gagal mengganti password. Coba lagi.');
    setStatus('Password berhasil diubah! Mengalihkan ke halaman masuk...', false);
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
    await postJSON('/auth/forgot-password', {email: pendingEmail}, 'Gagal mengirim ulang kode. Coba lagi.');
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
  document.getElementById('fp-code').value = '';
  document.getElementById('fp-new-password').value = '';
  setStatus('', false);
  showStep('request');
});
