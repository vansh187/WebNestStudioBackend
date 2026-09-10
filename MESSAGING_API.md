# In-App Project Chat API (`/api/messaging`, `/api/users`)

User-to-user **group chats + 1:1 DMs** with image / PDF / file attachments,
reply-quote, emoji reactions and unread counts. Freshness is **client polling**
(no websockets).

> This is separate from `/api/chat` (the AI chatbot). Different tables, different
> prefix.

- **Base URL:** `https://webneststudiobackend-n00h.onrender.com`
- **Every endpoint requires** `Authorization: Bearer <access_token>`
- Interactive docs: `<base>/docs`
- All timestamps are ISO-8601 UTC. All ids are UUID strings.
- Errors are always `{"detail": "<message>"}` (422 validation errors are
  `{"detail": "Request validation failed", "errors": [...]}`).

---

## Shared object shapes

### UserSummary
```json
{ "id": "b3f1c8e2-…", "full_name": "Vansh Duggal", "email": "vansh@webneststudio.co.in" }
```
In **`GET /api/users/search`** results the `email` is **masked** (`va******@gmail.com`).
Everywhere else (participants, message senders) it is the real address.

### Attachment (inside a Message)
```json
{
  "url_path": "b3f1c8e2-…/2026/09/2b1c…-brief.pdf",
  "url": "https://<ref>.supabase.co/storage/v1/object/sign/chat_attachments/…?token=…",
  "name": "project-brief.pdf",
  "mime_type": "application/pdf",
  "size_bytes": 482113,
  "kind": "pdf",
  "width": null,
  "height": null
}
```
- `url` is a **fresh signed GET URL, ~1 h TTL**, added on every read. Don't cache it long-term; re-fetch the message to get a new one.
- `kind` is server-derived: `image/*` → `"image"`, `application/pdf` → `"pdf"`, else `"file"`.
- `url` is `null` if signing failed transiently — retry the read.

### ReactionGroup
```json
{ "emoji": "👍", "count": 3, "reacted_by_me": true }
```

### Message
```json
{
  "id": "9a2c…",
  "conversation_id": "51de…",
  "sender": { "id": "b3f1…", "full_name": "Vansh Duggal", "email": "vansh@webneststudio.co.in" },
  "body": "Updated the homepage hero, please review 👇",
  "attachments": [],
  "reply_to": {
    "id": "77b0…",
    "sender": { "id": "c4d2…", "full_name": "Aditya Rao", "email": "aditya@example.com" },
    "body_preview": "Can you change the CTA colour?",
    "is_deleted": false
  },
  "reactions": [ { "emoji": "👍", "count": 2, "reacted_by_me": false } ],
  "is_deleted": false,
  "created_at": "2026-09-09T09:32:11.482Z",
  "edited_at": null
}
```
- `body` is `null` for an attachment-only message, and `null` once `is_deleted` is true.
- `reply_to` is `null` when the message isn't a reply (or the quoted message was hard-removed).
- A deleted message comes back as `is_deleted: true`, `body: null`, `attachments: []`, `reactions: []`.

### Participant
```json
{
  "user": { "id": "b3f1…", "full_name": "Vansh Duggal", "email": "vansh@webneststudio.co.in" },
  "role": "owner",
  "joined_at": "2026-09-01T07:00:00Z",
  "last_read_at": "2026-09-09T09:33:00Z"
}
```
`role` ∈ `owner` | `admin` | `member`. Only **active** members are listed (people who left are omitted).

### Conversation
```json
{
  "id": "51de…",
  "type": "group",
  "title": "V Stitch — Website Revamp",
  "project_id": null,
  "created_by": "b3f1…",
  "participants": [ /* Participant… */ ],
  "last_message": {
    "id": "9a2c…",
    "sender_name": "Vansh Duggal",
    "preview": "Updated the homepage hero, please review",
    "created_at": "2026-09-09T09:32:11.482Z",
    "has_attachment": false
  },
  "unread_count": 4,
  "created_at": "2026-09-01T07:00:00Z",
  "updated_at": "2026-09-09T09:32:11.482Z"
}
```
- `type: "direct"` → `title` is `null`; show the other participant's name.
- `project_id` is non-null only when the group is a project team room; use it to badge the room. `null` for every ordinary group and DM.
- `last_message` is `null` before the first message.
- `unread_count` = messages from other people newer than your `last_read_at`.

---

## Conversations

### 1. List my conversations
`GET /api/messaging/conversations`

No params. Returns every conversation you're an active member of, **newest activity first**.

**200**
```json
{ "conversations": [ /* Conversation objects */ ] }
```

---

### 2. Create a group
`POST /api/messaging/conversations/group`

**Request**
```json
{ "title": "V Stitch — Website Revamp", "participant_ids": ["c4d2…", "e5f3…"] }
```
- `title`: 1–120 chars.
- `participant_ids`: 1–50 existing active user ids (yourself is ignored if included).
- You become `owner`; the rest `member`.

**201** → `Conversation`

**Errors**
- `400 {"detail":"One or more users could not be found"}`
- `400 {"detail":"Add at least one other person to the group"}`
- `422` — validation (title length, >50 ids, …)

---

### 3. Create / open a direct chat
`POST /api/messaging/conversations/direct`

**Request**
```json
{ "user_id": "c4d2…" }
```
Find-or-create the 1:1 with that user. Both stored as `member`.

- **200** → `Conversation` (already existed)
- **201** → `Conversation` (just created)
- Response `type` is `"direct"`, `title` is `null`.

**Errors**
- `400 {"detail":"You cannot start a chat with yourself"}`
- `400 {"detail":"User not found"}`

---

### 4. Get one conversation
`GET /api/messaging/conversations/{conversation_id}`

**200** → `Conversation`

**Errors**
- `403 {"detail":"You are not a participant in this conversation"}`
- `404 {"detail":"Conversation not found"}`

---

### 5. Rename a group
`PATCH /api/messaging/conversations/{conversation_id}`

**Request** `{ "title": "V Stitch — Phase 2" }` (1–120 chars)

`owner` / `admin` only, groups only.

**200** → `Conversation`

**Errors**
- `403` — not owner/admin
- `409 {"detail":"Direct chats cannot be renamed"}`

---

### 6. Add members
`POST /api/messaging/conversations/{conversation_id}/participants`

**Request** `{ "user_ids": ["a1b2…", "f6a7…"] }` (1–50)

`owner` / `admin` only. Re-adding someone who left revives their membership. Already-active ids are silently skipped.

**200** → `Conversation` (updated participant list)

**Errors**
- `400 {"detail":"One or more users could not be found"}`
- `403` — not owner/admin
- `409 {"detail":"Cannot add members to a direct chat"}`

---

### 7. Remove a member / leave
`DELETE /api/messaging/conversations/{conversation_id}/participants/{user_id}`

- Pass **your own id** to leave.
- `owner` / `admin` can remove other members.
- The **owner cannot be removed by anyone else** — the owner has to leave themselves. When the owner leaves a group that still has members, ownership auto-passes to the earliest-joined remaining `admin`, else the earliest-joined `member`.

**204** — no body

**Errors**
- `403 {"detail":"Only group admins can remove other members"}`
- `403 {"detail":"The group owner cannot be removed. The owner has to leave the group themselves."}`
- `404 {"detail":"That person is not a member of this conversation"}`

---

## Messages

### 8. List / page messages
`GET /api/messaging/conversations/{conversation_id}/messages`

**Query params**
| param | default | notes |
|---|---|---|
| `limit` | `30` | max `100` |
| `before` | — | message id; return the page **older** than it (scroll-back) |
| `after` | — | message id; return messages **newer** than it (polling) |

`before` and `after` are mutually exclusive; omit both for the latest page.
Messages come back **ascending by `created_at`**.

**200**
```json
{ "messages": [ /* Message… */ ], "has_more": true }
```
- With `before` / no cursor: `has_more` = older messages exist before this page.
- With `after`: `has_more` = newer messages exist after this page.

**Errors**
- `400 {"detail":"Pass either 'before' or 'after', not both"}`
- `400 {"detail":"The 'before'/'after' message id does not belong to this conversation"}`
- `403` — not a participant

---

### 9. Send a message
`POST /api/messaging/conversations/{conversation_id}/messages`

**Request** — at least one of `body` / `attachments` required.
```json
{
  "body": "Here's the revised brief 🙂",
  "reply_to_message_id": "77b0…",
  "attachments": [
    {
      "url_path": "b3f1…/2026/09/2b1c…-brief.pdf",
      "name": "project-brief.pdf",
      "mime_type": "application/pdf",
      "size_bytes": 482113,
      "kind": "pdf",
      "width": null,
      "height": null
    }
  ]
}
```
- `body` ≤ 4000 chars.
- `attachments`: 1–10. Each `url_path` **must** be one you received from endpoint 14 for **your own** upload — re-using someone else's path is rejected.
- `reply_to_message_id` must be a non-deleted message in this same conversation.

**201** → `Message`

**Errors**
- `403 {"detail":"Attachment url_path was not issued to you. Upload the file first via /api/messaging/attachments/sign-upload."}`
- `400 {"detail":"reply_to_message_id does not belong to this conversation"}`
- `403` — not a participant
- `413 {"detail":"File exceeds the 25 MB limit"}`
- `415 {"detail":"File type not allowed"}`
- `422 {"detail":"Provide a message body or at least one attachment"}`
- `422 {"detail":"A message can have at most 10 attachments"}`

---

### 10. Mark read
`POST /api/messaging/conversations/{conversation_id}/read`

**Request** `{ "last_read_message_id": "9a2c…" }`

Sets your `last_read_at` to that message's timestamp; `unread_count` drops on the next list/get.

**204** — no body

**Errors**
- `403` — not a participant
- `404 {"detail":"That message is not part of this conversation"}`

---

### 11. Add a reaction
`POST /api/messaging/messages/{message_id}/reactions`

**Request** `{ "emoji": "👍" }` — idempotent (adding the same one twice is a no-op).

**200**
```json
{ "message_id": "9a2c…", "reactions": [ { "emoji": "👍", "count": 3, "reacted_by_me": true } ] }
```

**Errors**
- `400 {"detail":"Cannot react to a deleted message"}`
- `403` — not a participant of that message's conversation
- `404 {"detail":"Message not found"}`

---

### 12. Remove a reaction
`DELETE /api/messaging/messages/{message_id}/reactions/{emoji}`

`emoji` is URL-encoded — `👍` → `%F0%9F%91%8D`.

**200** — same shape as #11.

**Errors** — `403`, `404`.

---

### 13. Delete a message
`DELETE /api/messaging/messages/{message_id}`

Soft delete. Allowed for the **sender**, or the conversation `owner` / `admin`.

**200** → `Message` with `is_deleted: true`, `body: null`, `attachments: []`, `reactions: []`.

**Errors**
- `403 {"detail":"You can only delete your own messages"}`
- `404 {"detail":"Message not found"}`

---

## Attachments

### 14. Sign an upload URL
`POST /api/messaging/attachments/sign-upload`

Call **once per file before #9**, then PUT the bytes straight to Supabase, then
reference `url_path` in the message.

**Request**
```json
{ "filename": "project-brief.pdf", "mime_type": "application/pdf", "size_bytes": 482113 }
```

**200**
```json
{
  "url_path": "b3f1…/2026/09/2b1c…-brief.pdf",
  "storage_path": "chat_attachments/b3f1…/2026/09/2b1c…-brief.pdf",
  "upload_url": "https://<ref>.supabase.co/storage/v1/object/upload/sign/chat_attachments/…?token=…",
  "method": "PUT",
  "headers": { "Content-Type": "application/pdf", "x-upsert": "true" },
  "expires_in": 120
}
```

**Then, client-side:**
```
PUT {upload_url}
headers: {headers}          // exactly as returned
body: <raw file bytes>
```
A `2xx` means the bytes are stored. Now send the message (#9) with the `url_path`.

**Allowed MIME types:** `image/jpeg`, `image/png`, `image/webp`, `image/gif`,
`image/heic`, `application/pdf`, `text/plain`, `application/msword`,
`application/vnd.openxmlformats-officedocument.wordprocessingml.document`,
`application/vnd.ms-excel`,
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
`application/zip`. Max **25 MB** per file.

**Errors**
- `413 {"detail":"File exceeds the 25 MB limit"}`
- `415 {"detail":"File type not allowed"}`
- `503` — attachment storage not configured on the server

---

## Users

### 15. Search users (people picker)
`GET /api/users/search?q=<term>&limit=<n>`

| param | default | notes |
|---|---|---|
| `q` | — | required, **≥ 2 chars** after trim |
| `limit` | `20` | max `50` |

Case-insensitive match on `full_name` **or** `email`, active accounts only,
**excludes you**. `email` in results is **masked**.

**200**
```json
{ "results": [ { "id": "c4d2…", "full_name": "Aditya Rao", "email": "ad****@example.com" } ] }
```

**Errors**
- `400 {"detail":"Search needs at least 2 characters"}`

---

## Typical client flows

**Send a photo + caption**
1. `POST /api/messaging/attachments/sign-upload` → get `upload_url`, `url_path`.
2. `PUT {upload_url}` with the file bytes.
3. `POST /api/messaging/conversations/{id}/messages` with `body` + `attachments:[{url_path, …}]`.

**Poll a thread**
- First open: `GET .../messages` (latest page), remember the last message id.
- Every N seconds: `GET .../messages?after={lastId}` → append; keep the newest id.
- Scroll up: `GET .../messages?before={oldestLoadedId}` until `has_more` is false.
- After the user views: `POST .../read` with the newest visible message id.

**Conversation list badge**
- `GET /api/messaging/conversations` on an interval; sum / show `unread_count` per row.
