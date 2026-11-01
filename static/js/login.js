const statusEl = document.getElementById('status');
function setStatus(msg, isError) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? '#ff6b6b' : '#4dff88';
}

document.getElementById('btn-login').onclick = async () => {
  const email = document.getElementById('login-email').value.trim();
  const password = document.getElementById('login-password').value;
  if (!email || !password) { setStatus('Isi email dan password dulu.', true); return; }

  const btn = document.getElementById('btn-login');
  btn.disabled = true;
  setStatus('Memproses...', false);

  try {
    const res = await fetch('/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Login gagal');

    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('refresh_token', data.refresh_token);
    localStorage.setItem('user_id', data.user_id);
    localStorage.setItem('user_email', email);

    setStatus('Berhasil login! Mengalihkan...', false);
    setTimeout(() => { window.location.href = '/'; }, 1000);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
};

document.querySelectorAll('.pw-toggle').forEach(btn => {
  btn.onclick = () => {
    const input = document.getElementById(btn.dataset.target);
    input.type = input.type === 'password' ? 'text' : 'password';
    btn.style.opacity = input.type === 'password' ? '1' : '0.6';
  };
});
