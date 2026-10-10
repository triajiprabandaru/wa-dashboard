-- 001_init.sql  |  Postgres 15+ / Supabase
-- Fase 1: workspace multi-user, impor idempotent, pencarian.  Fase 2: tabel items (disiapkan, belum dipakai UI).
-- BELUM diuji pada database sungguhan: jalankan di proyek Supabase kosong dan laporkan error bila ada.

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- ------------------------------------------------------------------ akses --
create table workspaces (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  created_at  timestamptz not null default now()
);

create table workspace_members (
  workspace_id uuid not null references workspaces on delete cascade,
  user_id      uuid not null references auth.users on delete cascade,
  role         text not null check (role in ('owner','editor','viewer')),
  primary key (workspace_id, user_id)
);

create table projects (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces on delete cascade,
  name         text not null,
  kind         text,                       -- bebas: 'konstruksi', 'software', ...
  created_at   timestamptz not null default now()
);

create table project_members (
  project_id uuid not null references projects on delete cascade,
  user_id    uuid not null references auth.users on delete cascade,
  role       text not null check (role in ('editor','viewer')),
  primary key (project_id, user_id)
);

-- ------------------------------------------------------------------ chat ---
create table groups (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces on delete cascade,
  name         text not null,
  source_key   text not null,              -- kunci stabil antar-export (group_key dari parser)
  created_at   timestamptz not null default now(),
  unique (workspace_id, source_key)
);

-- satu project bisa berisi banyak grup, satu grup bisa masuk banyak project
create table project_groups (
  project_id uuid not null references projects on delete cascade,
  group_id   uuid not null references groups   on delete cascade,
  primary key (project_id, group_id)
);

create table imports (
  id          uuid primary key default gen_random_uuid(),
  group_id    uuid not null references groups on delete cascade,
  imported_by uuid references auth.users,
  filename    text,
  date_order  text check (date_order in ('MDY','DMY')),
  tz          text not null default 'Asia/Jakarta',
  msg_total   int  not null default 0,
  msg_new     int  not null default 0,
  first_ts    timestamptz,
  last_ts     timestamptz,
  warnings    jsonb not null default '[]',
  created_at  timestamptz not null default now()
);

-- orang (identity resolution): nama tampilan berbeda antar-grup digabung lewat alias
create table participants (
  id           uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces on delete cascade,
  display_name text not null,
  phone        text,                       -- hanya terisi jika export menampilkan nomor
  created_at   timestamptz not null default now()
);
create table participant_aliases (
  workspace_id   uuid not null references workspaces on delete cascade,
  alias          text not null,            -- persis seperti di export
  participant_id uuid not null references participants on delete cascade,
  primary key (workspace_id, alias)
);

create table messages (
  id             uuid primary key default gen_random_uuid(),
  group_id       uuid not null references groups on delete cascade,
  import_id      uuid references imports on delete set null,
  msg_hash       text not null,            -- dari parser; kunci dedup
  ts             timestamptz not null,
  sender_alias   text,                     -- nama/nomor persis seperti di export
  participant_id uuid references participants on delete set null,
  kind           text not null check (kind in ('text','media','system','deleted')),
  system_event   text,
  body           text not null default '',
  edited         boolean not null default false,
  mentions       text[] not null default '{}',
  created_at     timestamptz not null default now(),
  tsv            tsvector generated always as (to_tsvector('simple', coalesce(body,''))) stored,
  unique (group_id, msg_hash)
);
create index messages_group_ts   on messages (group_id, ts desc);
create index messages_sender     on messages (participant_id);
create index messages_tsv        on messages using gin (tsv);
create index messages_body_trgm  on messages using gin (body gin_trgm_ops);

create table attachments (
  id           uuid primary key default gen_random_uuid(),
  message_id   uuid not null references messages on delete cascade,
  filename     text,
  media_kind   text not null check (media_kind in ('image','video','audio','document','sticker','other')),
  available    boolean not null default false,   -- file ada di export / sudah diunggah
  storage_path text,                              -- path di object storage
  size_bytes   bigint,
  sha256       text
);
create index attachments_message on attachments (message_id);
create index attachments_sha256  on attachments (sha256) where sha256 is not null;

create table links (
  id         uuid primary key default gen_random_uuid(),
  message_id uuid not null references messages on delete cascade,
  url        text not null,
  domain     text
);
create index links_message on links (message_id);
create index links_domain  on links (domain);

-- ------------------------------------------- fase 2: item terstruktur ------
create table items (
  id                  uuid primary key default gen_random_uuid(),
  project_id          uuid not null references projects on delete cascade,
  group_id            uuid references groups on delete set null,
  source_message_id   uuid references messages on delete set null,
  kind                text not null check (kind in
                        ('task','open_question','approval','decision','deadline','meeting','issue','info')),
  title               text not null,
  status              text not null default 'open' check (status in ('open','waiting','done','dismissed')),
  urgency             smallint not null default 0 check (urgency between 0 and 3),
  assignee_id         uuid references participants on delete set null,
  due_date            date,
  confidence          real,
  created_by          text not null default 'ai' check (created_by in ('ai','user')),
  opened_at           timestamptz not null default now(),
  closed_at           timestamptz
);
create index items_project_status on items (project_id, status, urgency desc);

create table item_events (
  id           uuid primary key default gen_random_uuid(),
  item_id      uuid not null references items on delete cascade,
  at           timestamptz not null default now(),
  actor_user   uuid references auth.users,
  actor_kind   text not null default 'user' check (actor_kind in ('user','ai')),
  from_status  text,
  to_status    text,
  note         text,
  message_id   uuid references messages on delete set null
);

-- ------------------------------------------------------------------ RLS ----
-- Impor/pemrosesan malam memakai service role (melewati RLS). Aplikasi web memakai
-- akun pengguna, sehingga semua baca/tulis di bawah ini berlaku.

create function public.is_workspace_member(ws uuid, min_role text default 'viewer')
returns boolean language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from workspace_members
    where workspace_id = ws and user_id = auth.uid()
      and (min_role = 'viewer'
           or (min_role = 'editor' and role in ('owner','editor'))
           or (min_role = 'owner'  and role = 'owner'))
  )
$$;

create function public.can_read_project(pid uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from project_members where project_id = pid and user_id = auth.uid())
      or exists (select 1 from projects p where p.id = pid and public.is_workspace_member(p.workspace_id, 'owner'))
$$;

create function public.can_edit_project(pid uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from project_members where project_id = pid and user_id = auth.uid() and role = 'editor')
      or exists (select 1 from projects p where p.id = pid and public.is_workspace_member(p.workspace_id, 'owner'))
$$;

-- grup terbaca jika masuk project yang boleh dibaca user, atau user owner workspace
create function public.can_read_group(gid uuid)
returns boolean language sql stable security definer set search_path = public as $$
  select exists (select 1 from project_groups pg where pg.group_id = gid and public.can_read_project(pg.project_id))
      or exists (select 1 from groups g where g.id = gid and public.is_workspace_member(g.workspace_id, 'owner'))
$$;

alter table workspaces           enable row level security;
alter table workspace_members    enable row level security;
alter table projects             enable row level security;
alter table project_members      enable row level security;
alter table groups               enable row level security;
alter table project_groups       enable row level security;
alter table imports              enable row level security;
alter table participants         enable row level security;
alter table participant_aliases  enable row level security;
alter table messages             enable row level security;
alter table attachments          enable row level security;
alter table links                enable row level security;
alter table items                enable row level security;
alter table item_events          enable row level security;

create policy ws_read      on workspaces        for select using (public.is_workspace_member(id));
create policy wsm_read     on workspace_members for select using (user_id = auth.uid() or public.is_workspace_member(workspace_id, 'owner'));
create policy wsm_manage   on workspace_members for all    using (public.is_workspace_member(workspace_id, 'owner'))
                                                           with check (public.is_workspace_member(workspace_id, 'owner'));

create policy proj_read    on projects          for select using (public.can_read_project(id));
create policy proj_manage  on projects          for all    using (public.is_workspace_member(workspace_id, 'owner'))
                                                           with check (public.is_workspace_member(workspace_id, 'owner'));
create policy pm_read      on project_members   for select using (user_id = auth.uid() or public.can_edit_project(project_id));
create policy pm_manage    on project_members   for all    using (exists (select 1 from projects p where p.id = project_id and public.is_workspace_member(p.workspace_id, 'owner')))
                                                           with check (exists (select 1 from projects p where p.id = project_id and public.is_workspace_member(p.workspace_id, 'owner')));

create policy grp_read     on groups            for select using (public.can_read_group(id));
create policy grp_manage   on groups            for all    using (public.is_workspace_member(workspace_id, 'owner'))
                                                           with check (public.is_workspace_member(workspace_id, 'owner'));
create policy pg_read      on project_groups    for select using (public.can_read_project(project_id));
create policy pg_manage    on project_groups    for all    using (exists (select 1 from projects p where p.id = project_id and public.is_workspace_member(p.workspace_id, 'owner')))
                                                           with check (exists (select 1 from projects p where p.id = project_id and public.is_workspace_member(p.workspace_id, 'owner')));

create policy imp_read     on imports           for select using (public.can_read_group(group_id));
create policy msg_read     on messages          for select using (public.can_read_group(group_id));
create policy att_read     on attachments       for select using (exists (select 1 from messages m where m.id = message_id and public.can_read_group(m.group_id)));
create policy lnk_read     on links             for select using (exists (select 1 from messages m where m.id = message_id and public.can_read_group(m.group_id)));

create policy part_read    on participants      for select using (public.is_workspace_member(workspace_id));
create policy part_edit    on participants      for all    using (public.is_workspace_member(workspace_id, 'editor'))
                                                           with check (public.is_workspace_member(workspace_id, 'editor'));
create policy alias_read   on participant_aliases for select using (public.is_workspace_member(workspace_id));
create policy alias_edit   on participant_aliases for all    using (public.is_workspace_member(workspace_id, 'editor'))
                                                             with check (public.is_workspace_member(workspace_id, 'editor'));

create policy items_read   on items             for select using (public.can_read_project(project_id));
create policy items_edit   on items             for update using (public.can_edit_project(project_id)) with check (public.can_edit_project(project_id));
create policy evt_read     on item_events       for select using (exists (select 1 from items i where i.id = item_id and public.can_read_project(i.project_id)));
create policy evt_insert   on item_events       for insert with check (exists (select 1 from items i where i.id = item_id and public.can_edit_project(i.project_id)));

-- Catatan operasional
--  * Matikan pendaftaran publik di Supabase Auth (invite-only); undang teman lewat dashboard Auth.
--  * Workspace pertama + baris workspace_members (owner) dibuat sekali lewat SQL editor / service role.
--  * Pesan tidak pernah ditulis dari browser: impor berjalan di worker dengan service role.
