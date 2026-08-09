# Telegram news MCP

The project ranks posts from Telegram channels available to a user account and
exposes them through one REST endpoint and the MCP tool `getTopNews`.

## Setup

1. Create a Telegram application at <https://my.telegram.org>, then configure
   its credentials:

   ```bash
   cp .env.example .env
   # Edit .env and set TELEGRAM_API_ID and TELEGRAM_API_HASH.
   ```

2. Authorize manually. This command is interactive and may ask for the phone
   number, Telegram code, and the account's 2FA password. It creates
   `data/telegram.session`; neither MCP nor OpenClaw takes part in this flow.

   ```bash
   docker compose run --rm telegram-auth
   ```

3. Start both services:

   ```bash
   docker compose up -d --build
   ```

The REST endpoint is internal to Docker Compose. `mcp-server` is a persistent
Streamable HTTP server. Its port is bound to `127.0.0.1` only, so it is
reachable by OpenClaw on the same host but not exposed publicly.

## OpenClaw MCP configuration

Configure an HTTP MCP server with this local URL:

```bash
http://127.0.0.1:8001/mcp
```

Install `openclaw-skill/telegram-top-news` as an OpenClaw skill. It documents
the tool's arguments and tells agents to report `TELEGRAM_AUTH_REQUIRED` to the
user. The user must then manually repeat the authorization command above.

## REST request

```json
{
  "period": "9h32m",
  "exclude_mode": true,
  "excluded_channels": ["@noisy_channel"],
  "included_channels": []
}
```

`period` accepts non-zero compact durations from weeks, days, hours, and
minutes (`1w`, `1d`, `9h32m`). With `exclude_mode: false`, specify at least one
channel in `included_channels`. A selector can be a title, username,
`@username`, ID, or `-100`-prefixed ID.
