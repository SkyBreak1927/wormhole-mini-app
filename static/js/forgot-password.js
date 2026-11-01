const stepRequest = document.getElementById('step-request');
const stepReset = document.getElementById('step-reset');
const statusEl = document.getElementById('status');
let pendingEmail = '';

function setStatus(msg, isError) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? '#ff6b6b' : '#4dff88';
}

document.getElementById('btn-request').onclick = async () => {
  const email = document.getElementById('fp-email').value.trim();
  if (!email) { setStatus('Isi email dulu.', true); return; }

  const btn = document.getElementById('btn-request');
  btn.disabled = true;
  setStatus('Memproses...', false);

  try {
    const res = await fetch('/auth/forgot-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Gagal mengirim kode');

    pendingEmail = email;
    setStatus(data.message, false);
    stepRequest.classList.remove('active');
    stepReset.classList.add('active');
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
};

document.getElementById('btn-reset').onclick = async () => {
  const code = document.getElementById('fp-code').value.trim();
  const newPassword = document.getElementById('fp-new-password').value;
  if (!code || !newPassword) { setStatus('Isi kode dan password baru dulu.', true); return; }

  const btn = document.getElementById('btn-reset');
  btn.disabled = true;
  setStatus('Memproses...', false);

  try {
    const res = await fetch('/auth/reset-password', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: pendingEmail, code, new_password: newPassword})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Gagal reset password');

    setStatus('Password berhasil diubah! Mengalihkan ke login...', false);
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
    btn.setAttribute('aria-pressed', input.type === 'password' ? 'false' : 'true');
  };
});
