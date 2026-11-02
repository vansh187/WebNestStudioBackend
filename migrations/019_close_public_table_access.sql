-- Closes the public schema to Supabase's anon and authenticated roles. All data access goes
-- through the API, which connects as the tables' owner, so neither these grants nor Row Level
-- Security apply to it. service_role is left alone.
--
-- Revoking matters as well as RLS: RLS doesn't stop TRUNCATE or calling a function, and the
-- default privileges would hand every table that create_all adds later straight back to anon.
-- Postgres also lets PUBLIC execute any new function, and only a default without IN SCHEMA can
-- change that, so that one covers all of a role's new functions, not just those in public.
--
-- This is one DO statement because it commits per table, so that no table's lock is held while
-- waiting for the next. Run it on its own, outside a transaction. Safe to re-run. Each 3-second
-- lock limit is local to its own transaction, so the session's setting never changes.
--
-- Rollback, leaving RLS on for the four assistant tables that had it before this:
--   grant all on all tables in schema public to anon, authenticated;
--   grant all on all sequences in schema public to anon, authenticated;
--   grant execute on all functions in schema public to public, anon, authenticated;
--   alter default privileges in schema public grant all on tables to anon, authenticated;
--   alter default privileges in schema public grant all on sequences to anon, authenticated;
--   alter default privileges in schema public grant all on functions to anon, authenticated;
--   alter default privileges grant execute on functions to public;
--   then, for every other table: alter table <name> disable row level security;

DO $$
DECLARE
    target record;
    locked text[] := '{}';
    not_owned text[] := '{}';
    other_owners text[] := '{}';
BEGIN
    PERFORM set_config('lock_timeout', '3s', true);

    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;

    -- Written out one by one, because extensions' own functions must stay as they are.
    FOR target IN
        SELECT p.oid::regprocedure AS name
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
        WHERE n.nspname = 'public' AND d.objid IS NULL
    LOOP
        EXECUTE format('REVOKE ALL ON ROUTINE %s FROM PUBLIC, anon, authenticated', target.name);
    END LOOP;

    -- Every role that sets defaults here or owns a table, not just the one running this.
    FOR target IN
        SELECT pg_get_userbyid(d.defaclrole) AS owner
        FROM pg_default_acl d
        JOIN pg_namespace n ON n.oid = d.defaclnamespace
        WHERE n.nspname = 'public'
        UNION
        SELECT pg_get_userbyid(c.relowner)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
        UNION
        SELECT current_user
    LOOP
        BEGIN
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated', target.owner);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated', target.owner);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM anon, authenticated', target.owner);
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC', target.owner);
        EXCEPTION WHEN insufficient_privilege THEN
            other_owners := other_owners || target.owner::text;
        END;
    END LOOP;
    COMMIT;

    FOR target IN
        SELECT n.nspname AS schema_name, c.relname AS table_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity
    LOOP
        PERFORM set_config('lock_timeout', '3s', true);
        BEGIN
            EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', target.schema_name, target.table_name);
        EXCEPTION
            WHEN lock_not_available THEN
                locked := locked || target.table_name::text;
            WHEN insufficient_privilege THEN
                not_owned := not_owned || target.table_name::text;
            WHEN undefined_table THEN
                NULL; -- Dropped since the list was read, so there is nothing left to protect.
        END;
        COMMIT;
    END LOOP;

    IF cardinality(locked) > 0 THEN
        RAISE WARNING 'Row Level Security not enabled, the table was locked: %. Run this again.', locked;
    END IF;
    IF cardinality(not_owned) > 0 THEN
        RAISE WARNING 'Row Level Security not enabled, another role owns the table: %. Running this again will not help; run it as the owner.', not_owned;
    END IF;
    IF cardinality(other_owners) > 0 THEN
        RAISE NOTICE 'Default privileges left as they are for roles this one cannot act for: %. Anything those roles create in public can still be open to anon.', other_owners;
    END IF;
END $$;
