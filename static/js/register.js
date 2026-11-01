const stepRegister = document.getElementById('step-register');
const stepVerify = document.getElementById('step-verify');
const statusEl = document.getElementById('status');
let pendingEmail = '';

function setStatus(msg, isError) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? '#ff6b6b' : '#4dff88';
}

document.getElementById('btn-register').onclick = async () => {
  const email = document.getElementById('reg-email').value.trim();
  const password = document.getElementById('reg-password').value;
  if (!email || !password) { setStatus('Isi email dan password dulu.', true); return; }

  const btn = document.getElementById('btn-register');
  btn.disabled = true;
  setStatus('Memproses...', false);

  try {
    const res = await fetch('/auth/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Gagal daftar');

    pendingEmail = email;
    setStatus('Kode verifikasi sudah dikirim ke email kamu.', false);
    stepRegister.classList.remove('active');
    stepVerify.classList.add('active');
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
};

document.getElementById('btn-verify').onclick = async () => {
  const code = document.getElementById('verify-code').value.trim();
  if (!code) { setStatus('Isi kode verifikasi dulu.', true); return; }

  const btn = document.getElementById('btn-verify');
  btn.disabled = true;
  setStatus('Memverifikasi...', false);

  try {
    const res = await fetch('/auth/verify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: pendingEmail, code})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Kode salah');

    setStatus('Berhasil! Mengalihkan ke halaman login...', false);
    setTimeout(() => { window.location.href = '/login'; }, 1500);
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
