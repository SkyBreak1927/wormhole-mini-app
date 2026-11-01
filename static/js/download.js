// Feedback setelah klik Unduh: tiap link hanya berlaku sekali, jadi tombol langsung dikunci
// dan pengguna diberi tahu unduhan sudah dimulai (dibacakan lewat aria-live).
const dlStatus = document.getElementById('dl-status');
document.querySelectorAll('a[data-dl]').forEach(a => {
  a.addEventListener('click', (e) => {
    if (a.getAttribute('aria-disabled') === 'true') { e.preventDefault(); return; }
    // tunda sedikit supaya navigasi unduhan sempat dimulai sebelum tombol dikunci
    setTimeout(() => {
      a.setAttribute('aria-disabled', 'true');
      a.setAttribute('tabindex', '-1');
      a.textContent = '✓ Diunduh';
      const row = a.closest('.row');
      if (row) row.classList.add('done');
      dlStatus.textContent = '"' + a.dataset.name + '" mulai diunduh. Cek folder Unduhan di perangkatmu. Link ini tidak bisa dipakai lagi.';
    }, 150);
  });
});
