# Local Grok Tool Server

This local MCP server exposes one stateless tool:

- `ask_grok(messages, model?)` sends a list of system, user, and assistant
  messages to Grok and returns its response.

Example input:

```json
{
  "messages": [
    {"role": "system", "content": "You are a coding assistant."},
    {"role": "user", "content": "Build a todo API."}
  ]
}
```

Grok responses are cached on disk under `.cache/grok`. The SHA-256 cache key
includes the model and complete message list. The cache maps that request to the
corresponding Grok response and is excluded from Git.

## Start

The local `.env` file must contain:

```text
XAI_API_KEY=your-secret-key
```

It is excluded from Git.

Start the server before opening or using it from Cursor:

```bash
./start.sh
```

The MCP endpoint is:

```text
http://127.0.0.1:8765/mcp
```

## Cursor configuration

Add this project-level configuration to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "local-grok": {
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

The server does not retain conversation state between calls.
