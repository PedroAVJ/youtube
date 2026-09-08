---
name: youtube-cli
description: Read and edit YouTube playlists, liked videos, and subscriptions through the ytx CLI. Use for playlist discovery, listing playlist contents, adding/removing/reordering videos, creating or deleting playlists, subscription and liked-video reads, video metadata, and YouTube Data API quota questions.
---

# YouTube (CLI)

Use this plugin when the user wants to inspect or manage YouTube playlists.

`ytx` is a purpose-built CLI over the YouTube Data API v3. Google ships no
first-party CLI for YouTube, so unlike the Workspace plugins there is no `gws`
underneath — `ytx` *is* the tool. Source lives at `scripts/youtube_cli.py` in
this plugin; `~/.local/bin/ytx` is a shim onto it.

## Start

```bash
command -v ytx
ytx auth status
ytx playlists list
```

`auth status` reports the channel, the granted scopes, and whether writes are
available. If it says `authenticated: false`, run `ytx auth login --write`
(read-only: omit `--write`). Login prints a URL and waits — it deliberately
does not open a browser. Add `--open` only if the user asks.

## Quota Is The Constraint

10,000 units/day per project, resetting midnight Pacific. It cannot be raised
by paying — only by a manual compliance audit. Budget accordingly:

| Operation | Units | Per day |
| --- | --- | --- |
| any `list` (`playlists`, `playlistItems`, `videos`, `channels`, `subs`) | 1 | ~10,000 |
| any write (`create`, `delete`, `items add/remove/move`, `subs remove`) | 50 | ~200 |
| `search` | 100 | ~100 |

```bash
ytx quota
```

Check it before a large batch, and read it after anything unexpected. Failed
calls still bill.

**Prefer the cache over `search`.** `search` is the single most expensive call
and is gated behind `--yes` for that reason. Most "find the video" work can be
done against synced data for zero units.

## Safe Reads

```bash
ytx playlists list
ytx playlists show "workout"          # resolves by title fragment once synced
ytx playlists show PLxxxxxxxx
ytx liked --pages 2
ytx subs list
ytx video dQw4w9WgXcQ https://youtu.be/QHM-ixZrs9M
```

Video arguments accept bare IDs, `watch?v=` URLs, `youtu.be` links, Shorts,
and `music.youtube.com` URLs interchangeably.

Output is a table on a TTY and JSON when piped. Force JSON with `--json` after
the subcommand.

## The Cache (zero quota)

```bash
ytx sync                              # mirrors playlists + contents into SQLite
ytx playlists list --cached
ytx playlists show "fantasy" --cached
ytx db query "SELECT channel_title, COUNT(*) n FROM playlist_items GROUP BY 1 ORDER BY n DESC LIMIT 10"
```

`sync` costs 1 unit per playlist plus 1 per 50 items — a full mirror of a
few hundred videos is under 10 units. Everything after it is free.

Tables: `playlists(id, title, description, privacy, item_count, published_at,
synced_at)` and `playlist_items(item_id, playlist_id, video_id, title,
channel_title, position, published_at, synced_at)`. `db query` accepts
`SELECT`/`WITH` only.

Re-run `sync` before trusting cached data for a mutation — the cache is a
snapshot, not a live view.

## Writes

Ask before creating, deleting, adding, removing, reordering, or unsubscribing
unless the user already made that action explicit. Deleting a playlist additionally requires
`--yes`; do not pass it on the user's behalf without a clear instruction.

```bash
ytx playlists create "Focus" --privacy private --description "..."
ytx playlists rename PLxxxx "New title"
ytx items add "Focus" https://www.youtube.com/watch?v=VIDEO_ID
ytx items remove "Focus" VIDEO_ID
ytx items move "Focus" VIDEO_ID --position 0
ytx playlists delete PLxxxx --yes
ytx subs remove "Channel Name"        # or a channel ID
```

New playlists default to `private`. After an approved mutation, read the
playlist back and report the confirmed state.

## Rules

- Prefer playlist IDs for mutations. Title resolution is a convenience for
  interactive use; it errors on ambiguity rather than guessing, but a stale
  cache can still resolve to the wrong playlist.
- `ytx playlists show` against a live playlist is 1 unit — cheap enough to
  verify after every write. Do it.
- YouTube and YouTube Music share one backend and one playlist ID space, so
  playlists here are the same objects YouTube Music shows. Music-only surfaces
  are not reachable — see `references/limitations.md`.
- Watch history is **not** available through this or any API. Do not offer it.
- Tokens live in the macOS Keychain (`ytx-oauth`), not on disk. `ytx auth
  logout` revokes upstream and clears the entry.
