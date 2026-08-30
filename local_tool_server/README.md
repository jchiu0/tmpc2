# Local Grok Tool Server

This local MCP server exposes one tool:

- `ask_grok(prompt)` sends a message to Grok and returns its response.

The server retains conversation context in memory until it restarts.

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

Restart the server to clear its in-memory Grok conversation.
