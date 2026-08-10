---
name: spotify-discovery-playlist
description: Create a Spotify discovery playlist from the user's listening history.
user-invocable: true
---

# Spotify discovery playlist

Use the MCP tool `createSpotifyDiscoveryPlaylist` when the user asks for a
playlist with new music related to their Spotify listening history. It uses
Spotify top tracks as seeds, Last.fm only for similar-track discovery, and
Spotify catalog search to verify every result. It does not use Spotify's
Recommendations API because that endpoint is unavailable (404) to new
Development Mode applications without Extended access.

## Preconditions

The user must configure a Spotify Dashboard app, add
`http://127.0.0.1:8888/callback` as its Redirect URI, set `SPOTIFY_CLIENT_ID`
and `LASTFM_API_KEY` in `.env`, and authorize manually:

```bash
docker compose --profile spotify-auth run --rm --service-ports spotify-auth
docker compose up -d --build
```

The authorization app requests `user-top-read`, `playlist-modify-private`, and
`playlist-modify-public` when a public playlist is requested. Do not ask for,
receive, or handle a Spotify password, authorization code, refresh token, or
other credential. The local PKCE flow handles these directly.

If the tool returns `SPOTIFY_AUTH_REQUIRED`, tell the user to manually repeat
the authorization command. Do not try to authorize Spotify through OpenClaw.

## Tool arguments

Call `createSpotifyDiscoveryPlaylist` with:

- `top_n`: source-pool size from 1 to 35. Default to `35`.
- `playlist_name`: a concise user-facing playlist name that always includes
  the current date in ISO format (`YYYY-MM-DD`) to keep repeated playlists
  distinct. Default to `Discovery playlist — YYYY-MM-DD`. If the user gives a
  title, retain it and append ` — YYYY-MM-DD`.
- `public`: defaults to `false`. Only set true when the user explicitly wants
  a public playlist.

The tool returns the Spotify playlist URL, actual known/recommended-track
counts, seed counts and recommendation quotas per time segment, plus warnings
when a segment has too few available tracks.

## Response template

After success, reply in the user's language. Offer the Spotify URL as a
clickable descriptive phrase rather than a raw URL. Mention any returned
warnings compactly and truthfully.

```markdown
Готово — я собрал [плейлист «Fresh discoveries»](https://open.spotify.com/playlist/example)
с 8 знакомыми треками и 24 новыми рекомендациями.
```

Do not claim a target count was reached when the tool reports fewer actual
tracks. Never expose `.env` values or refresh-token storage paths in a user
response.
