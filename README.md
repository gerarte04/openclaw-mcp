# Telegram news and Spotify discovery MCP

The project exposes two independent user-account integrations through one REST
service and a Streamable HTTP MCP server:

- `getTopNews` ranks recent posts from Telegram channels.
- `createSpotifyDiscoveryPlaylist` builds a Spotify playlist from the user's
  top tracks and similar tracks returned by Last.fm.

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

Install `openclaw-skill/telegram-top-news` and
`openclaw-skill/spotify-discovery-playlist` as OpenClaw skills. They document
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
In Development Mode, also add the Spotify account as a test user. The app must
request `user-top-read`, `playlist-modify-private`, and, if public playlists
are needed, `playlist-modify-public`.

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
matches the best candidates back to Spotify by both title and artist. It
combines up to 25% known tracks (at most 10) with three times as many new
recommendations. The response includes the playlist URL, actual counts,
temporal seed quotas, and warnings if a catalogue segment ran short.

If authorization is missing, REST and MCP return `SPOTIFY_AUTH_REQUIRED`; rerun
the manual authorization command. They never ask for a Spotify password or
authorization code.
