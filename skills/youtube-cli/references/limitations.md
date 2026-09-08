# YouTube Data API — what it cannot do

Read this before promising a capability. These are platform limits, not gaps
in `ytx`.

## Watch history and Watch Later: gone since 2016

`channels.list` still returns `contentDetails.relatedPlaylists.watchHistory`
and `watchLater` with the placeholder values `HL` and `WL`, but querying them
returns empty lists. Deprecated 2016-08-11 and never restored.

There is no scope, no permission, and no quota tier that brings it back.

- **Official alternative:** Google Takeout exports full watch and search
  history as a batch download.
- **Unofficial:** `ytmusicapi` exposes `get_history()`, but that is *YouTube
  Music play history*, not YouTube watch history.

`activities.list` sounds like a substitute and is not — it returns public
channel activity, not private viewing.

## YouTube Music surfaces

YouTube and YouTube Music share one backend and one playlist ID space. A
playlist created here is the same object YouTube Music shows, and vice versa.
YouTube Music is a filtered view over the shared library, not a separate one.

But these are YouTube-Music-only and unreachable via the Data API:

- **Liked Music** — a distinct playlist from YouTube's Liked videos
- Personal uploads
- Albums and artists saved to library
- Play history
- Lyrics, charts, moods/genres, radio and mixes
- Podcasts and episodes

Reaching those requires `ytmusicapi`, an unofficial reverse-engineered library
that breaks when Google reshuffles internal endpoints. Not wired into `ytx`.

## Quota cannot be bought

10,000 units/day. There is no paid tier and no way to pay for more. The only
path is the YouTube API Services Audit and Quota Extension Form — a manual
compliance review requiring a project URL, public privacy policy, terms of
service, and a use-case description. Reported turnaround runs weeks to months
and personal-tooling use cases are routinely rejected.

Assume 10,000/day is permanent.

## Other ceilings worth knowing

- `search.list` costs 100 units, so ~100 searches/day exhausts everything.
  Sync and query locally instead.
- `playlistItems.list` returns 50 per page; `ytx` paginates to 50 pages by
  default.
- Playlist item *position* is writable via `playlistItems.update`, but there
  is no bulk reorder — each move is its own 50-unit call.
- Deleted or private videos still occupy playlist positions and appear with
  the title `Deleted video` / `Private video` and an empty channel.
