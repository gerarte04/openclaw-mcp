# Telegram news and Spotify discovery MCP

The project exposes independent integrations through one REST service and a
Streamable HTTP MCP server:

- `getTopNews` ranks recent posts from Telegram channels.
- `createSpotifyDiscoveryPlaylist` builds a Spotify playlist from the user's
  top tracks and similar tracks returned by Last.fm.
- `sendTelegramBotMessage` sends text and optional media through a Telegram
  bot.

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

`mcp-server` is a persistent Streamable HTTP server. By default the REST
service (port 8000) and the MCP server (port 8001) are published on
`127.0.0.1` only, so they are reachable on the same host but not exposed
publicly. `REST_BIND_ADDRESS`, `REST_PORT`, `MCP_BIND_ADDRESS`, and `MCP_PORT`
in `.env` change this; `0.0.0.0` listens on every network interface.

Setting `MCP_PUBLIC_URL` (the server's public HTTPS address) turns on OAuth
for the MCP server, so it can be added to Claude as a custom connector: Claude
registers itself, you approve access once with `MCP_OAUTH_PASSWORD`, and its
tokens refresh automatically. To deploy this on a server with HTTPS (Caddy)
through Ansible, and to authorize Telegram and Spotify there from your
terminal, see [deploy/ansible/README.md](deploy/ansible/README.md).

## OpenClaw MCP configuration

Configure an HTTP MCP server with this local URL:

```bash
http://127.0.0.1:8001/mcp
```

Install `openclaw-skill/telegram-top-news`,
`openclaw-skill/spotify-discovery-playlist`, and
`openclaw-skill/telegram-bot-message` as OpenClaw skills. They document
their tools and tell agents to report required manual authorization without
requesting credentials.

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

## Spotify discovery playlists

Create a Spotify app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
Add this exact Redirect URI to the app:

```text
http://127.0.0.1:8888/callback
```

Put its Client ID (never a client secret) in `.env` as `SPOTIFY_CLIENT_ID`.
In Development Mode, also add the Spotify account as a test user. The app
requests `user-top-read`, `user-library-read`, `playlist-read-private`,
`playlist-read-collaborative`, `playlist-modify-private`, and
`playlist-modify-public`. If Spotify was authorized before the library-read
scopes were added, repeat the manual authorization flow to issue a refresh
token with the new grants.

Create a Last.fm API key at <https://www.last.fm/api/account/create> and set
`LASTFM_API_KEY`. It is used only to obtain similar tracks; Spotify remains the
catalogue lookup and playlist destination. Spotify's Recommendations endpoint
is deliberately not called: it returns 404 for new Development Mode apps
without Extended access.

Authorize manually in a local browser:

```bash
cp .env.example .env
# Set SPOTIFY_CLIENT_ID and LASTFM_API_KEY in .env.
docker compose --profile spotify-auth run --rm --service-ports spotify-auth
```

The PKCE callback verifies OAuth state and saves only the refresh token in
`data/spotify/refresh-token.json`, with owner-only permissions. `data/` and
`.env` are git-ignored. Do not copy the token into chat, an MCP request, or
source control. Afterwards start the normal services:

```bash
docker compose up -d --build
```

### REST request

`POST /spotify/discovery-playlist` creates a private playlist by default:

```json
{
  "top_n": 35,
  "playlist_name": "Fresh discoveries",
  "public": false
}
```

`top_n` is limited to 1–35. The service reads short-, medium-, and long-term
top tracks (at most Spotify's 50-track response limit), randomly selects
diverse seeds, queries Last.fm with bounded concurrency and a TTL cache, then
matches the best candidates back to Spotify by both title and artist. Before
asking Last.fm, it reads the current user's saved tracks and all readable
playlist items, then excludes those tracks from new recommendations. The
intentional source portion—up to 25% known tracks (at most 10)—is not filtered.
The response includes the playlist URL, actual counts, temporal seed quotas,
the number of familiar recommendations excluded, unreadable-playlist count, and
warnings if a catalogue segment ran short.

If authorization is missing or its refresh token lacks library-read scopes,
REST and MCP return `SPOTIFY_AUTH_REQUIRED`; rerun the manual authorization
command. They never ask for a Spotify password or authorization code. See
[SPOTIFY_ALGO.md](SPOTIFY_ALGO.md) for the complete selection algorithm,
rate limits, and filtering behavior.

## Telegram bot messages

Create a bot with [@BotFather](https://t.me/BotFather) and put its token in
`.env` as `TELEGRAM_BOT_TOKEN`. Optionally set `TELEGRAM_BOT_DEFAULT_CHAT_ID`
to the chat used when a request has no `chat_id` (for example your own user
ID; write `/start` to the bot first, because bots cannot start a private
chat). The bot is independent from the Telethon user session above. Restart
the services after changing `.env`.

`POST /telegram-bot/messages`:

```json
{
  "text": "Weekly digest",
  "media": [
    {"type": "photo", "url": "https://example.com/cover.jpg"},
    {"type": "video", "url": "https://example.com/clip.mp4"}
  ],
  "chat_id": "@my_channel",
  "parse_mode": "HTML"
}
```

- `text`: up to 4096 characters. `media`: up to 10 items, each with `type`
  (`photo`, `video`, `animation`, `audio`, `document`) and a public HTTP(S)
  `url`; Telegram downloads it itself (photos up to 5 MB, other files up to
  20 MB). At least one of `text` and `media` is required.
- Consecutive photos and videos are sent as one album; consecutive audio
  files and documents form albums of their own kind; animations are sent
  individually. The text becomes the caption of the first media item when it
  fits in 1024 characters, otherwise it is sent as a separate message first.
- `chat_id` (numeric ID or `@channel_username`) and `parse_mode` (`HTML` or
  `MarkdownV2`) are optional.
- With `TELEGRAM_BOT_ALLOW_ANY_CHAT=false`, the bot sends only to
  `TELEGRAM_BOT_DEFAULT_CHAT_ID` and the comma-separated
  `TELEGRAM_BOT_ALLOWED_CHAT_IDS`. This keeps text in Telegram posts or web
  pages that an agent reads from redirecting messages elsewhere. The Ansible
  deployment turns this on.

The response contains `chat_id` and the sent `message_ids`. Errors are
`TELEGRAM_BOT_NOT_CONFIGURED` (503), `TELEGRAM_BOT_CHAT_NOT_ALLOWED` (403),
`TELEGRAM_BOT_AUTH_ERROR` (502, invalid token), `TELEGRAM_BOT_REJECTED` (422, Telegram's own description such as an
unknown chat or unreadable media URL), `UPSTREAM_RATE_LIMIT` (429), and
`TELEGRAM_BOT_TEMPORARY_ERROR` (503). If a failure happens after some messages
of a multi-part send were delivered, the error message says how many, so a
retry does not duplicate them unknowingly. The bot token is never returned in
responses or error messages.
