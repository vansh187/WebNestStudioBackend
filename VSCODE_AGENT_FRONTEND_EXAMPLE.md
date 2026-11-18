# VS Code Agent Mode: Frontend API Example

This document shows the frontend request and response flow for Agent mode on `POST /api/extension/chat`.

## Initial request

The endpoint returns Server-Sent Events (SSE), not a single JSON response.

```http
POST /api/extension/chat
Authorization: Bearer <access_token>
Content-Type: application/json
Accept: text/event-stream
```

```json
{
  "conversation_id": "conv_123",
  "client_request_id": "req_456",
  "mode": "agent",
  "messages": [
    {
      "role": "user",
      "content": "Find the bug in src/cart.ts and fix it."
    }
  ],
  "context": {
    "os": "win32",
    "shell": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
    "files": ["src/cart.ts"],
    "active_file": {
      "path": "src/cart.ts",
      "language": "typescript",
      "content": "export function total(items) { return items.reduce((sum, item) => sum + item.cost, 0); }"
    }
  }
}
```

`mode` is case and whitespace insensitive. Missing or unrecognized values are treated as Ask mode.

## Example tool-call response

Each SSE event consists of an `event:` line and a JSON `data:` line. In Agent mode, tool calls are sent together before the `done` event.

```text
event: start
data: {"request_id":"req_456","message_id":"msg_a1b2c3d4"}

event: tool_call
data: {"id":"call_789","name":"read_file","arguments":{"path":"src/cart.ts"}}

event: usage
data: {"input_tokens":812,"output_tokens":42,"calls_used_today":3,"calls_limit":30}

event: done
data: {"finish_reason":"tool_calls"}
```

The extension runs requested tools locally, subject to its path and protected-file checks, Workspace Trust, and any required user approval.

## Send tool results in the next request

Keep the prior messages and append the assistant tool call and its result. A tool result can include `"is_error": true` if the extension skipped, refused, or failed the tool.

```json
{
  "conversation_id": "conv_123",
  "client_request_id": "req_457",
  "mode": "agent",
  "messages": [
    {
      "role": "user",
      "content": "Find the bug in src/cart.ts and fix it."
    },
    {
      "role": "assistant",
      "tool_calls": [
        {
          "id": "call_789",
          "name": "read_file",
          "arguments": {"path": "src/cart.ts"}
        }
      ]
    },
    {
      "role": "tool",
      "tool_call_id": "call_789",
      "content": "export function total(items: Item[]) { return items.reduce((sum, item) => sum + item.cost, 0); }"
    }
  ],
  "context": {
    "os": "win32",
    "shell": "powershell.exe"
  }
}
```

The next response may request additional tools or return text and finish with `done` using `{"finish_reason":"stop"}`.

## Available tools

Agent mode offers these tools:

- `list_dir`
- `read_file`
- `search_text`
- `get_diagnostics`
- `create_file`
- `edit_file`
- `delete_file`
- `run_command`

Ask mode offers no tools. Each tool call is one assistant round; after 25 rounds since the last non-empty user message, the backend responds in Ask mode without tools.
