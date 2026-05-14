# MCP — Model Context Protocol

**Model Context Protocol** is the open protocol for connecting an LLM to external tool servers. castor is a full MCP client — point it at a server (filesystem, GitHub, Slack, Postgres, your own), and the server's tools join castor's tool catalog. The agent uses them like any built-in.

Why this matters: instead of castor shipping every integration directly (and going stale), it speaks the same protocol the rest of the ecosystem uses. New MCP server today → new agent capability today, with zero castor code change.

## Two transports

| Transport | When | Setup |
|---|---|---|
| **stdio** | The MCP server is a local subprocess. Most reference servers ship this way. | castor spawns the process and pipes JSON-RPC over stdin/stdout. |
| **HTTP** | The MCP server runs as a network service. Useful for shared servers, container deployments. | castor POSTs JSON-RPC to the configured URL. |

stdio is the common path. HTTP works for hosted MCP servers (Cloudflare's MCP, Anthropic's hosted servers, your own).

## Quick start

### From the Web UI

Settings → **MCP** tab → **Add server**:

| Field | What |
|---|---|
| **Name** | Short id (`filesystem`, `github`, `slack`, …). Tools appear as `mcp__<name>__<tool>`. |
| **Transport** | `stdio` or `http` |
| **stdio command** | The shell command to start the server. E.g. `npx -y @modelcontextprotocol/server-filesystem /home/me/projects` |
| **HTTP URL** | If transport is HTTP, the JSON-RPC endpoint |
| **Env** | Key=value pairs passed to the subprocess (API keys, etc.) |

Hit Save. castor spawns the server, reads its tool catalog, and the new tools show up next time you `tool_search`.

### From chat

The `mcp_manager` skill lets the agent set up MCP servers on request:

```
You:    Подключи MCP-сервер для работы с GitHub. Токен возьми из хранилища
        secrets под именем github_pat.
Agent:  [tool_search("mcp")]
        [secret_get name="github_pat"]
        [mcp_add_server name="github" command="npx -y @modelcontextprotocol/server-github"
          env={"GITHUB_TOKEN": "<from secret>"}]
        Added 'github' (12 tools: github_create_issue, github_list_repos, ...)
        Готово. Попробуй "перечисли мои репозитории".
```

The `mcp_manager` tools (after `tool_search("mcp")`):

| Tool | What it does |
|---|---|
| `mcp_add_server(name, transport, command_or_url, env?)` | Register a new server. Auto-starts it. |
| `mcp_remove_server(name)` | Stop + unregister. |
| `mcp_restart_server(name)` | Restart subprocess (after env change or crash). |
| `mcp_list_servers()` | Show registered servers + status + tool counts. |

### CLI

```
/mcp                                    # list
/mcp add filesystem stdio "npx -y @modelcontextprotocol/server-filesystem /path"
/mcp remove filesystem
/mcp restart filesystem
```

## Tool naming

MCP tools are namespaced `mcp__<server>__<tool>`:

```
mcp__filesystem__read_file
mcp__filesystem__list_directory
mcp__github__create_issue
mcp__slack__send_message
```

This means an MCP server's `read_file` doesn't collide with castor's built-in `read_file` — they're separate tools, the model picks the right one based on context.

`tool_search` finds MCP tools by keyword too — `tool_search("github")` will surface all `mcp__github__*` tools alongside any built-ins. The model doesn't need to know whether a tool is built-in or MCP.

## Useful MCP servers

The MCP ecosystem is growing fast. Anthropic's [reference servers](https://github.com/modelcontextprotocol/servers) cover the most common needs:

| Server | What it does | Setup |
|---|---|---|
| **filesystem** | Read / list / search files in a sandboxed root | `npx -y @modelcontextprotocol/server-filesystem /path/to/root` |
| **github** | Repos, issues, PRs, commits, file contents | `npx -y @modelcontextprotocol/server-github` + `GITHUB_TOKEN` env |
| **gitlab** | GitLab equivalent | `npx -y @modelcontextprotocol/server-gitlab` + `GITLAB_TOKEN` |
| **postgres** | Read-only SQL against a Postgres DB | `npx -y @modelcontextprotocol/server-postgres postgresql://...` |
| **sqlite** | Read-only SQL against a SQLite file | `npx -y @modelcontextprotocol/server-sqlite /path/to/db.sqlite` |
| **brave-search** | Web search via Brave Search API | `npx -y @modelcontextprotocol/server-brave-search` + `BRAVE_API_KEY` |
| **fetch** | HTTP GET arbitrary URLs (less restrictive than browser) | `npx -y @modelcontextprotocol/server-fetch` |
| **time** | Current time, timezone conversions | `npx -y @modelcontextprotocol/server-time` |
| **memory** | Anthropic's reference memory server (alternative pattern) | `npx -y @modelcontextprotocol/server-memory` |
| **puppeteer** | Browser automation, alternative to castor's built-in | `npx -y @modelcontextprotocol/server-puppeteer` |
| **slack** | Slack integration | `npx -y @modelcontextprotocol/server-slack` + tokens |
| **everart** | EverArt image generation | `npx -y @modelcontextprotocol/server-everart` + API key |
| **google-maps** | Maps + places + directions | `npx -y @modelcontextprotocol/server-google-maps` + API key |

Most servers ship as **npm packages with `-y` auto-install** — no manual `npm i`, `npx` pulls them on first run. Cache lives in `~/.npm/`.

Python and Rust servers also exist — same JSON-RPC, castor doesn't care what the server is written in.

### Home Assistant — the hardware swiss army knife

For ANY non-serial smart-home device (Zigbee, Z-Wave, Wi-Fi, MQTT, BLE), point castor at your existing Home Assistant via an HA MCP server:

```
[mcp_add_server name="ha" command="npx -y @modelcontextprotocol/server-home-assistant"
  env={"HA_URL": "http://homeassistant.local:8123", "HA_TOKEN": "..."}]
```

Now castor can control 2000+ HA integrations: lights, locks, thermostats, vacuum cleaners, climate sensors, Sonos, garage doors. Combined with [hardware support](HARDWARE.md) (USB-serial scales + scanners + PLCs direct), castor covers essentially every device class with one config.

## Writing your own MCP server

If the integration you need doesn't exist, write a server. Minimal Python example using the official SDK:

```python
# myserver.py
from mcp.server import Server
from mcp.types import Tool, TextContent

app = Server("my-thing")

@app.list_tools()
async def list_tools():
    return [Tool(name="greet", description="Say hello",
                 inputSchema={"type": "object",
                              "properties": {"name": {"type": "string"}}})]

@app.call_tool()
async def call_tool(name, arguments):
    if name == "greet":
        return [TextContent(type="text", text=f"Hello, {arguments['name']}!")]

if __name__ == "__main__":
    import asyncio
    from mcp.server.stdio import stdio_server
    async def main():
        async with stdio_server() as (r, w):
            await app.run(r, w, app.create_initialization_options())
    asyncio.run(main())
```

Register in castor:

```
[mcp_add_server name="my-thing" transport="stdio"
  command="python /path/to/myserver.py"]
```

The `mcp_manager` skill + castor's built-in MCP client handle the rest.

For TypeScript / Rust / etc., see [modelcontextprotocol.io](https://modelcontextprotocol.io) for SDK examples.

## Configuration

Servers are persisted in `~/.castor/castor.db` (table `mcp_servers`). The schema:

| Column | Notes |
|---|---|
| `name` | PK |
| `transport` | `stdio` / `http` |
| `command` | shell command (stdio) |
| `url` | endpoint (http) |
| `env` | JSON of env vars |
| `enabled` | `1` / `0` — pause without removing |
| `auto_restart` | `1` by default — relaunch on crash |

Settings → MCP exposes all fields as forms.

### Env vars for MCP-specific secrets

Don't embed API keys in the `command` string — put them in `env`. The vault tools (`secret_save` / `secret_get`) work nicely here:

```
[secret_save name="GITHUB_TOKEN" value="ghp_..."]
[mcp_add_server name="github" command="..." env={"GITHUB_TOKEN": "<from vault>"}]
```

The vault stores encrypted; the server sees the decrypted value as a normal env var.

## Telemetry

MCP tool calls bucket into the `turn_complete` event's `tool_calls_count` — same as built-in tools. The server NAMES are not reported (no list of which integrations you use); only the total tool-call count and error count.

If you opt into the `tool_error` event, you'll see error kinds like `mcp_unavailable`, `mcp_timeout`, `mcp_protocol` — useful for debugging recurring server issues without revealing what server it is.

See [PRIVACY.md](PRIVACY.md) for the full contract.

## Troubleshooting

**Server fails to start** — castor's UI shows the spawn error. Common: `npx` missing (install Node), the package name has a typo, an env var the server needs isn't set.

**Server crashes mid-turn** — `auto_restart=1` (default) relaunches on next use. If it crashes immediately on relaunch, the server has a real bug; check its logs via `mcp_logs(name)`.

**MCP tools missing from `tool_search`** — server tools refresh on server start. If you added the server after castor started, restart it via `mcp_restart_server` to re-pull the catalog.

**`mcp_call_tool` returns "transport closed"** — stdio pipe closed (server crashed). Auto-restart will fix on next call if `auto_restart=1`.

**Tool result is "Internal error"** — something in the server itself. Check `mcp_logs(name)` for the server-side stack trace. castor's MCP client doesn't swallow errors; it surfaces them to the agent.

## Cross-links

- [SKILLS.md](SKILLS.md) — `mcp_manager` is one of the built-in skills
- [HARDWARE.md](HARDWARE.md) — Home Assistant via MCP bridges Zigbee / Z-Wave / WiFi devices
- [BROWSER.md](BROWSER.md) — alternative to puppeteer MCP if you want castor's built-in
- [modelcontextprotocol.io](https://modelcontextprotocol.io) — protocol spec + reference servers
