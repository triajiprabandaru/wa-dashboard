/* CARA PAKAI EXPLORER
   1. Jalankan 003_storage_policy.sql di Supabase SQL Editor (sekali).
   2. Isi dua nilai di bawah dari Supabase > tombol Connect > tab Framework.
      Kunci "publishable" memang aman ada di browser; data dilindungi RLS.
   3. Supabase > Authentication > URL Configuration: tambahkan http://localhost:8000 ke Redirect URLs.
   4. Di folder ini jalankan:  python -m http.server 8000   lalu buka http://localhost:8000
   5. Masuk dengan email yang sama dengan owner (tautan login dikirim ke email). */
window.WA_CONFIG = {
  url: "https://gaisxicxnfscduqgffvh.supabase.co",
  key: "sb_publishable_fsFQc4y0e0x2Vkw9G4Z94A_SlvaaBsK",
};
