-- In-app project chat (user-to-user messaging).
--
-- app.py's lifespan runs Base.metadata.create_all, so these four tables are
-- created automatically on deploy - this file is for review / explicit manual
-- apply against Supabase only. Nothing here ALTERs an existing table.
--
-- Rollback:
--   drop table if exists message_reactions, messages,
--     conversation_participants, conversations cascade;

CREATE TABLE IF NOT EXISTS conversations (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type                 text NOT NULL CHECK (type IN ('group', 'direct')),
    title                text,
    created_by           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_message_at      timestamptz,
    last_message_preview text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_conversations_last_message_at ON conversations (last_message_at DESC);

CREATE TABLE IF NOT EXISTS conversation_participants (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            text NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
    last_read_at    timestamptz,
    joined_at       timestamptz NOT NULL DEFAULT now(),
    left_at         timestamptz,
    CONSTRAINT uq_participant UNIQUE (conversation_id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_participants_conversation ON conversation_participants (conversation_id);
CREATE INDEX IF NOT EXISTS ix_participants_user ON conversation_participants (user_id);

CREATE TABLE IF NOT EXISTS messages (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body                text,
    reply_to_message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
    attachments         jsonb,
    is_deleted          boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    edited_at           timestamptz
);
CREATE INDEX IF NOT EXISTS ix_messages_conv_created ON messages (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS message_reactions (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_reaction UNIQUE (message_id, user_id, emoji)
);
CREATE INDEX IF NOT EXISTS ix_reactions_message ON message_reactions (message_id);
