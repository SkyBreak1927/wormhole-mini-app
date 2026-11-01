document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = document.getElementById('login-email').value.trim();
  const password = document.getElementById('login-password').value;
  if (!email || !password) { setStatus('Isi email dan password dulu.', true); return; }

  const btn = document.getElementById('btn-login');
  btn.disabled = true;
  setStatus('Memproses...', false);

  try {
    const data = await postJSON('/auth/login', {email, password}, 'Gagal masuk. Coba lagi.');

    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    localStorage.setItem('user_id', data.user_id);
    localStorage.setItem('user_email', email);

    setStatus('Berhasil masuk! Mengalihkan...', false);
    setTimeout(() => { window.location.href = '/'; }, 1000);
  } catch (err) {
    setStatus(err.message, true);
    btn.disabled = false;
  }
});
