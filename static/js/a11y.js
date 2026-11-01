// Elemen non-<button> yang berperan sebagai tombol (role="button"/"tab" + tabindex="0")
// ikut menjawab Enter dan Spasi seperti tombol asli. Delegasi, jadi berlaku juga untuk
// elemen yang dibuat belakangan oleh JS.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const el = e.target;
  if (!el.matches || !el.matches('[role="button"][tabindex],[role="tab"][tabindex]')) return;
  if (el.tagName === 'BUTTON' || el.tagName === 'A') return;
  e.preventDefault();
  el.click();
});
