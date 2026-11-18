# VS Code Agent Mode API

This document is the integration contract for Agent mode on `POST /api/extension/chat`. The server streams model output and tool requests; the VS Code extension executes tools locally and submits their results in the next chat request.

## Endpoint

```http
POST /api/extension/chat
Authorization: Bearer <extension_access_token>
Content-Type: application/json
Accept: text/event-stream
X-WebNest-Client: vscode/0.1.0 (Visual Studio Code)
X-Request-Id: <request_id>
```

The bearer token is an extension access token, not a website access token. A successful response is `200 OK` with `Content-Type: text/event-stream`. Errors before streaming use the API's normal JSON error response. Errors after streaming starts are sent as SSE `error` events.

## Request

```json
{
  "conversation_id": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
  "client_request_id": "9a8b7c6d-5e4f-4a3b-2c1d-0e9f8a7b6c5d",
  "mode": "agent",
  "approval": "manual",
  "phase": "execute",
  "messages": [
    {
      "role": "user",
      "content": "Find the bug in src/cart.ts and fix it."
    }
  ],
  "context": {
    "workspace_name": "shop-app",
    "os": "win32",
    "shell": "powershell.exe",
    "ui_language": "en",
    "files": ["src/cart.ts"],
    "files_truncated": false,
    "active_file": {
      "path": "src/cart.ts",
      "language": "typescript",
      "content": "export function total(items) { return items.reduce((sum, item) => sum + item.cost, 0); }",
      "truncated": false
    },
    "diagnostics": [],
    "attachments": []
  }
}
```

`conversation_id`, `client_request_id`, `mode`, and `messages` are sent by the extension. `context` fields may be empty, null, or omitted depending on user sharing choices; the server must not require workspace contents. Protected or secret-looking content is withheld by the extension.

`mode` is trimmed and compared case-insensitively. Only `agent` enables Agent behavior; missing or unrecognized values use Ask behavior and offer no tools. The extension currently sends `approval: "manual"` and `phase: "execute"`; these fields are informational and do not enforce approval. Approval, Workspace Trust, protected-path checks, and tool execution belong to the extension.

The extension retries once with the same `client_request_id` only when the first attempt fails before receiving any SSE event (connection failure or `503 provider_busy`).

## SSE response

Each application event has an `event:` line followed by a JSON `data:` line. SSE comment lines such as `: ping` are keepalives, not application events. The server may emit text, usage, and tool-call events; finish the response with `done` or `error`.

Example tool request:

```text
event: start
data: {"request_id":"5f0c2b1e-9d7a-4c1e-8f3b-2a6d9e0c1b4f","message_id":"msg_4f1a9c2e7b3d5a8f"}

event: text
data: {"delta":"I will inspect the file first."}

event: tool_call
data: {"id":"call_789","name":"read_file","arguments":{"path":"src/cart.ts"}}

event: usage
data: {"input_tokens":812,"output_tokens":42}

event: done
data: {"finish_reason":"tool_calls"}
```

Supported application events:

| Event | Data | Meaning |
|---|---|---|
| `start` | `{ "request_id": string, "message_id": string }` | Stream started. |
| `text` | `{ "delta": string }` | Append text to the assistant response. |
| `tool_call` | `{ "id": string, "name": string, "arguments": object \| string }` | Request a local tool call. Emit all calls for this assistant turn before `done`. |
| `usage` | Token counts and optional usage/access fields | Usage and account-meter updates. |
| `done` | `{ "finish_reason": "stop" \| "length" \| "tool_calls" }` | End of a successful stream. Use `tool_calls` when calls were emitted. |
| `error` | `{ "code": string, "message"?: string, "retryable"?: boolean, "retry_after"?: number }` | End the stream with an application error. |
| `: ping` | SSE comment, no JSON data | Keepalive; sent periodically during long waits. |

In Ask mode, offer no tools and do not emit `tool_call` events. Recommended Agent flow: emit `start`, optional text/usage, one or more `tool_call` events, then `done` with `finish_reason: "tool_calls"`.

## Tools offered in Agent mode

Tool names, argument names, required fields, limits, and `additionalProperties: false` must match the extension. The server asks for tools; it does not execute commands or access workspace files.

| Tool | Arguments | Constraints/defaults |
|---|---|---|
| `list_dir` | `path?`, `depth?` | `path` is relative to a workspace folder, at most 1,024 characters; omitted means workspace root. `depth`: integer 1–3, default 1. |
| `read_file` | `path`, `start_line?`, `end_line?` | `path` required, at most 1,024 characters; line values are positive 1-based integers up to 10,000,000. |
| `search_text` | `query`, `is_regex?`, `include?`, `max_results?` | `query` required, 1–500 characters. `include` is optional, 1–500 characters. `is_regex: true` is refused by the extension to keep VS Code responsive; offer literal search. `max_results`: integer 1–100, default 30. |
| `get_diagnostics` | `path?` | Omitted `path` means the whole workspace; a supplied path is at most 1,024 characters. |
| `create_file` | `path`, `content` | Both required; path at most 1,024 characters, content at most 200,000 characters. |
| `edit_file` | `path`, `old_text`, `new_text` | All required; path at most 1,024 characters, text fields at most 200,000 characters. The extension requires `old_text` to match exactly once. |
| `delete_file` | `path` | Required, at most 1,024 characters; files only, not folders. |
| `run_command` | `command`, `cwd?` | `command` required, 1–4,000 characters; `cwd` is optional and at most 1,024 characters. Omitted `cwd` means workspace root. |

All paths use workspace-relative `/` separators. In multi-root workspaces, include the workspace folder name. The extension applies path and protected-file checks and may require user approval. In particular, the extension controls command approval and runs commands on the user's machine.

The function definitions offered to the model use this JSON Schema shape (descriptions omitted here; argument names, types, required lists, bounds, and `additionalProperties` are normative):

```json
[
  { "type": "function", "function": { "name": "list_dir", "parameters": { "type": "object", "properties": { "path": { "type": "string" }, "depth": { "type": "integer", "minimum": 1, "maximum": 3 } }, "required": [], "additionalProperties": false } } },
  { "type": "function", "function": { "name": "read_file", "parameters": { "type": "object", "properties": { "path": { "type": "string" }, "start_line": { "type": "integer", "minimum": 1 }, "end_line": { "type": "integer", "minimum": 1 } }, "required": ["path"], "additionalProperties": false } } },
  { "type": "function", "function": { "name": "search_text", "parameters": { "type": "object", "properties": { "query": { "type": "string" }, "is_regex": { "type": "boolean" }, "include": { "type": "string" }, "max_results": { "type": "integer", "minimum": 1, "maximum": 100 } }, "required": ["query"], "additionalProperties": false } } },
  { "type": "function", "function": { "name": "get_diagnostics", "parameters": { "type": "object", "properties": { "path": { "type": "string" } }, "required": [], "additionalProperties": false } } },
  { "type": "function", "function": { "name": "create_file", "parameters": { "type": "object", "properties": { "path": { "type": "string" }, "content": { "type": "string" } }, "required": ["path", "content"], "additionalProperties": false } } },
  { "type": "function", "function": { "name": "edit_file", "parameters": { "type": "object", "properties": { "path": { "type": "string" }, "old_text": { "type": "string" }, "new_text": { "type": "string" } }, "required": ["path", "old_text", "new_text"], "additionalProperties": false } } },
  { "type": "function", "function": { "name": "delete_file", "parameters": { "type": "object", "properties": { "path": { "type": "string" } }, "required": ["path"], "additionalProperties": false } } },
  { "type": "function", "function": { "name": "run_command", "parameters": { "type": "object", "properties": { "command": { "type": "string" }, "cwd": { "type": "string" } }, "required": ["command"], "additionalProperties": false } } }
]
```

Use the table above for descriptions and runtime length limits: the provider-facing JSON Schema intentionally does not encode every limit enforced by the extension.

## Tool result round trip

After receiving `done` with `finish_reason: "tool_calls"`, the extension runs, refuses, or skips each requested tool locally. It then sends another request with the prior conversation plus:

1. An assistant message containing `content` and the exact `tool_calls` previously returned.
2. One tool message for each call, with the matching `tool_call_id`, tool `name`, result `content`, and optional `is_error`.

Example next-request `messages`:

```json
[
  {
    "role": "user",
    "content": "Find the bug in src/cart.ts and fix it."
  },
  {
    "role": "assistant",
    "content": "I will inspect the file first.",
    "tool_calls": [
      {
        "id": "call_789",
        "name": "read_file",
        "arguments": { "path": "src/cart.ts" }
      }
    ]
  },
  {
    "role": "tool",
    "tool_call_id": "call_789",
    "name": "read_file",
    "content": "export function total(items: Item[]) { return items.reduce((sum, item) => sum + item.cost, 0); }",
    "is_error": false
  }
]
```

The next response may request more tools or finish with text and `done` using `{"finish_reason":"stop"}`. Preserve each assistant tool-call message and provide exactly one matching tool result per call before the next user message. Tool results are limited by the active account tier's `max_tool_result_chars`.

If an incoming history names a tool that is not offered, reject the request before calling the provider with `422 validation_error`. If the model asks for an unoffered tool, reject the entire tool-call response rather than returning a partial set: emit an SSE `error` event with code `invalid_tool_call`. The extension treats errors in the stream as terminal for that request.

## Round limit

`max_agent_steps` from `/api/extension/me` is the maximum assistant tool-call rounds since the last non-empty user message (default 25). At the limit, answer without tools so the assistant can summarize and stop. Each model round is a separate chat request and counts toward existing rate and usage limits.

