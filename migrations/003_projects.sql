-- Client Project Progress (project-progress-backend-spec.md).
--
-- Base.metadata.create_all auto-creates `projects` / `project_stages` on
-- deploy (they're brand-new tables) — no manual step needed for those two.
-- This file is for review, and to attach the projects(id) FK onto the
-- existing `conversations.project_id` column, which create_all cannot do
-- (it only creates missing tables, never ALTERs an existing one). Mirrors
-- the same guarded-DO-block pattern as migrations/005_messaging_project_link.sql,
-- which already added that column + its partial unique index. Idempotent —
-- safe to re-run.
--
-- Rollback:
--   alter table conversations drop constraint if exists fk_conversations_project;
--   drop table if exists project_stages;
--   drop table if exists projects;

create table if not exists projects (
  id               uuid primary key default gen_random_uuid(),
  client_user_id   uuid not null references users(id) on delete cascade,
  name             text not null,
  summary          text,
  current_stage    text not null default 'requirements'
                     check (current_stage in ('requirements','design','development','testing','deployment','maintenance')),
  progress_percent integer check (progress_percent is null or (progress_percent between 0 and 100)),
  status           text not null default 'active' check (status in ('active','on_hold','completed','archived')),
  created_by       uuid references users(id) on delete set null,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index if not exists ix_projects_client on projects (client_user_id, updated_at desc);

create table if not exists project_stages (
  id           uuid primary key default gen_random_uuid(),
  project_id   uuid not null references projects(id) on delete cascade,
  key          text not null,
  label        text not null,
  order_index  integer not null,
  state        text not null default 'pending' check (state in ('pending','in_progress','done')),
  note         text,
  started_at   timestamptz,
  completed_at timestamptz,
  updated_at   timestamptz not null default now(),
  unique (project_id, key)
);
create index if not exists ix_project_stages_project on project_stages (project_id, order_index);

-- Attach the FK now that `projects` exists (migration 005 already added the
-- bare column + partial unique index; this is the missing FK constraint).
do $$
begin
    if not exists (
            select 1 from information_schema.table_constraints
            where constraint_name = 'fk_conversations_project'
              and table_name = 'conversations'
        )
    then
        alter table conversations
            add constraint fk_conversations_project
            foreign key (project_id) references projects(id) on delete set null;
    end if;
end $$;

-- One-time back-fill: give every existing project_status row a real project
-- so the new endpoints have data on day one. `phase` free text maps to
-- current_stage='requirements' (admins re-point it after); the six stage
-- rows are seeded pending. No group conversations are back-filled here —
-- an admin can create one later via PATCH .../projects/{id} with
-- create_conversation: true.
insert into projects (id, client_user_id, name, current_stage, progress_percent, status, created_at, updated_at)
select gen_random_uuid(), ps.client_user_id,
       coalesce(nullif(ps.project_name, ''), 'WebNest project'),
       'requirements', ps.percent_complete, 'active', now(), ps.updated_at
from project_status ps
where not exists (select 1 from projects p where p.client_user_id = ps.client_user_id);

insert into project_stages (id, project_id, key, label, order_index, state)
select gen_random_uuid(), p.id, s.key, s.label, s.ord, 'pending'
from projects p
cross join (values
  ('requirements','Requirements',0), ('design','Design',1), ('development','Development',2),
  ('testing','Testing',3), ('deployment','Deployment',4), ('maintenance','Maintenance',5)
) as s(key, label, ord)
where not exists (select 1 from project_stages existing where existing.project_id = p.id);
