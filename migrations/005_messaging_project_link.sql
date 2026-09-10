-- Project-linked conversations (spec section 11).
--
-- ADDITIVE ALTER on the existing conversations table. Base.metadata.create_all
-- does NOT add columns to a table that already exists, so this file must be run
-- explicitly in the Supabase SQL editor. Every statement is idempotent - safe
-- to re-run.
--
-- The `projects` table is created by project-progress-backend-spec.md section 2
-- and is NOT part of this service yet. This patch does not require it to exist:
--   * the project_id column and its one-conversation-per-project guarantee are
--     created now;
--   * the projects(id) foreign key is attached automatically the moment that
--     table exists - the guarded DO block below is a harmless no-op until then.
-- Re-run this file after applying the projects migration to pick the FK up.
--
-- Rollback:
--   alter table conversations drop constraint if exists fk_conversations_project;
--   drop index if exists uq_conversations_project;
--   alter table conversations drop column if exists project_id;

alter table conversations add column if not exists project_id uuid;

-- One conversation per project. Partial: ordinary groups and DMs keep
-- project_id NULL and are unaffected.
create unique index if not exists uq_conversations_project
    on conversations (project_id)
    where project_id is not null;

-- Attach the FK to projects(id) only once that table is present.
do $$
begin
    if exists (
            select 1 from information_schema.tables
            where table_schema = 'public' and table_name = 'projects'
        )
        and not exists (
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
