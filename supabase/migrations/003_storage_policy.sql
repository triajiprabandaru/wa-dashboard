-- 003_storage_policy.sql | izinkan pengguna login membaca media dari grup yang boleh mereka baca.
-- Path media = <group_id>/<hash>.<ext>, jadi folder pertama dipakai sebagai kunci akses.
create policy "wa_media_read" on storage.objects for select to authenticated
using (bucket_id = 'wa-media' and public.can_read_group(((storage.foldername(name))[1])::uuid));
