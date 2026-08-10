# Spotify discovery algorithm

`createSpotifyDiscoveryPlaylist` builds a short playlist that retains a small,
deliberately familiar source portion while making the recommendation portion
new to the user.

## Inputs and temporary state

For each request, the service reads the user's short-, medium-, and long-term
Spotify top tracks. It also pages through all saved tracks (`/me/tracks`) and
all playlists visible to the user (`/me/playlists`), then pages through each
readable playlist's items. Removed playlist positions (`track: null`) are
ignored.

These library results form a request-scoped in-memory exclusion set containing
Spotify track IDs and normalised `(artist, title)` pairs. It is not stored on
disk or reused by later requests.

## Selection

1. Distinct source tracks are sampled across short, medium, and long listening
   windows to preserve temporal diversity.
2. Up to 25% of the requested source size (at most 10 tracks) is selected as
   the intentionally familiar source portion. It is always retained, even when
   it is saved or appears in one of the user's playlists.
3. Last.fm `track.getSimilar` produces candidates for each seed. Candidates
   matching a source track or the library exclusion set are discarded before
   Spotify catalogue search.
4. Remaining candidates are scored within their time segment and matched in
   Spotify by normalised title and artist. The matched Spotify track is checked
   again against both its ID and title/artist in the exclusion set.
5. The service aims for three new recommendations per familiar source track,
   apportioned across segments by seed count. If a segment runs out, its quota
   is redistributed to segments with candidates.

The response reports `known_tracks_excluded` for familiar recommendation
candidates filtered during this request, plus `unavailable_playlists`.

## Rate limits and unavailable playlists

Playlist-item requests are bounded by `SPOTIFY_PLAYLIST_ITEM_CONCURRENCY`
(default `4`). A Spotify `429` honors `Retry-After` and retries the request.
Last.fm similarity requests have a separate concurrency limit and are spaced by
`LASTFM_REQUESTS_PER_SECOND` (default `4`), staying below the public
five-requests-per-second IP guideline.

Some followed playlists cannot be read unless the user is the owner or a
collaborator. A `403` while reading those items increments
`unavailable_playlists`, adds a warning, and does not stop playlist creation;
all accessible library tracks still participate in filtering.

The required OAuth scopes are `user-top-read`, `user-library-read`,
`playlist-read-private`, `playlist-read-collaborative`,
`playlist-modify-private`, and `playlist-modify-public`. Re-run manual OAuth
after upgrading from a token issued without the library-read scopes.
