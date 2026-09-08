# YouTube (CLI)

Read and edit YouTube playlists through `ytx`, a quota-aware CLI over the
YouTube Data API v3.

Unlike the other Google plugins in this repo, there is no vendor CLI
underneath. Google ships no first-party tool for YouTube, so `ytx` is the
tool — a single stdlib-only Python file at
[`scripts/youtube_cli.py`](./scripts/youtube_cli.py), no dependencies.

## Install

```bash
codex plugin add youtube@package-manager
mkdir -p ~/.local/bin
ln -s ~/.codex/plugins/cache/package-manager/youtube/0.2.7/bin/ytx ~/.local/bin/ytx
```

The plugin-owned wrapper is the stable front door into the released plugin
cache. Update the versioned symlink only after the replacement release is
installed and verified.

Then authorize. `ytx` reuses the Desktop OAuth client already configured for
`gws` (`~/.config/gws/client_secret.json`) but keeps its own token, so YouTube
scopes never disturb the Workspace credentials.

```bash
ytx auth login --write     # omit --write for read-only
ytx auth status
```

Login prints a URL and waits rather than launching a browser, so it never
steals focus mid-task. Pass `--open` if you want the browser opened.

Requires `youtube.googleapis.com` enabled on the project backing that OAuth
client.

## Use

```bash
ytx playlists list
ytx playlists show "workout"                     # resolves by title fragment
ytx items add "workout" https://youtu.be/VIDEO_ID
ytx items move "workout" VIDEO_ID --position 0
ytx items remove "workout" VIDEO_ID
ytx playlists create "Focus" --privacy private
ytx playlists delete PLxxxx --yes

ytx liked --pages 2
ytx subs list
ytx subs remove "Channel Name"
ytx video dQw4w9WgXcQ
ytx search "query" --yes                         # 100 units, gated on purpose
```

Video arguments accept bare IDs, `watch?v=` URLs, `youtu.be` links, Shorts,
and `music.youtube.com` URLs. Output is a table on a TTY, JSON when piped;
`--json` forces JSON.

## Quota

10,000 units/day per project, resetting midnight Pacific. It cannot be
purchased — the only path above it is a manual compliance audit.

| Operation | Units |
| --- | --- |
| any `list` | 1 |
| any write | 50 |
| `search` | 100 |

```bash
ytx quota      # today's spend, broken down by method
```

## Zero-quota reads

`sync` mirrors playlists and their contents into SQLite at
`~/.config/ytx/cache.db`. Everything after that is free.

```bash
ytx sync
ytx playlists list --cached
ytx db query "SELECT channel_title, COUNT(*) n FROM playlist_items GROUP BY 1 ORDER BY n DESC LIMIT 10"
```

A full mirror of a few hundred videos costs well under 10 units.

## Notes

- Tokens live in the macOS Keychain (`ytx-oauth`), not on disk. `ytx auth
  logout` revokes upstream and clears the entry.
- YouTube and YouTube Music share one backend and one playlist ID space —
  playlists here are the same objects YouTube Music shows.
- Watch history is not available through any YouTube API and hasn't been since
  2016. See [`limitations.md`](./skills/youtube-cli/references/limitations.md).
