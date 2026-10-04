-- 002_seq_and_bucket.sql  |  jalankan SEBELUM memakai wa_loader.py

-- Export WhatsApp hanya mencatat waktu sampai menit. seq = nomor baris di file export,
-- dipakai untuk mengurutkan pesan yang berada di menit yang sama.
alter table messages add column if not exists seq int;

drop index if exists messages_group_ts;
create index if not exists messages_group_ts_seq on messages (group_id, ts desc, seq desc);

-- Bucket privat untuk foto/dokumen/video. Akses dari aplikasi nanti lewat signed URL
-- yang dibuat di sisi server, jadi tidak perlu policy storage untuk pengguna.
insert into storage.buckets (id, name, public)
values ('wa-media', 'wa-media', false)
on conflict (id) do nothing;
