# YouTube Music (Free) Provider

An independent, community-built provider that adds free YouTube Music streaming to the open-source [Music Assistant](https://github.com/music-assistant/server) media server, using the same technique as open-source players like [SimpMusic](https://github.com/maxrave-dev/SimpMusic).

> [!IMPORTANT]
> This project is not affiliated with, endorsed by, or supported by the Music Assistant project or the Open Home Foundation. See [Support and bug reports](#support-and-bug-reports) before asking anyone for help.

## How it works

| Component | Role |
|-----------|------|
| `ytmusicapi` | Search, metadata, and library sync (optional auth) |
| `yt-dlp` | Extract direct audio stream URLs and playlist tracks |

Audio stream URLs are resolved without a login. Which of YouTube's internal clients does that resolving is left to yt-dlp's own defaults rather than pinned here, because the client that works anonymously keeps moving: the Android Music client this provider originally named has since been removed from yt-dlp entirely, and the Android and iOS clients now require a PO token, which an anonymous session cannot supply. yt-dlp tracks that target for us. This is the same general approach used by NewPipe and SimpMusic on Android.

For playlists, `yt-dlp` is used as a fallback when `ytmusicapi` cannot parse the unauthenticated playlist response from YouTube, ensuring playlists work without a login.

> **Note:** This uses YouTube's internal (unofficial) API. It may break if Google changes their API. Premium-exclusive content (offline, high-res audio) is not accessible.

---

## Support and bug reports

This is an unofficial, independent provider. It is not affiliated with or supported by the Music Assistant project.

The Music Assistant maintainers have stated more than once that they do not want this provider and will not support it. Please respect that:

- Do not open issues, discussions, or support requests about this provider on the Music Assistant repositories, Discord, or forum.
- Do not mention this provider when reporting an unrelated Music Assistant bug. If you hit a problem in Music Assistant itself, reproduce it with this provider removed before reporting it upstream.
- Report anything about this provider here, on this repository's [issue tracker](https://github.com/sproft/ytmusic-free-provider/issues).

Keeping these reports here respects the Music Assistant team's wishes and keeps them out of a project they have asked not to be involved with.

**What gets installed.** Both install scripts fetch the newest published [release](https://github.com/sproft/ytmusic-free-provider/releases) by default, so an install is reproducible and a bug report can name a version. Pass `--ref main` to track branch head instead, or `--ref v1.2.3` to pin an older release. If no release has been published yet, or GitHub is unreachable, the scripts fall back to branch head and say so in their output.

**Please include the version.** The provider logs it on every start, as the first line it writes:

```
YouTube Music (Free) provider version 1.0.0
```

Search the server log for `provider version` to find it. Without that line a report can only be answered with guesses about which build you have.

---

## Installation: Standalone Docker Compose

If you run the server via standalone Docker Compose instead of the Home Assistant OS add-on, you can use our pre-built image, which comes with the `ytmusic_free` provider pre-installed.

Replace the default upstream image (`ghcr.io/music-assistant/server:latest`) with this image in your `docker-compose.yml`:

```yaml
services:
  music-assistant:
    image: ghcr.io/sproft/ytmusic-free-provider:latest
    container_name: music-assistant
    restart: unless-stopped
    # ... keep your existing volumes, network, devices, and ports settings here
```

### Available Docker tags

Official releases and automatic builds use **strictly separated tags**. This separation exists because the plugin is built on top of a moving upstream base image (`ghcr.io/music-assistant/server`): a change in that base image can break the plugin at any time. Only official releases, created by pushing a git tag in this repository, are tested - automatic builds are rebuilt whenever the base image changes and are therefore **untested**.

> [!NOTE]
> **Tag semantics**
> - `:latest` points at the most recent tested **stable** release and is set only by release builds. Prereleases (`v1.2.3-rc.1`) publish `:1.2.3-rc.1` but never move `:latest`.
> - The rolling, untested channel builds are `:edge` (MA latest channel), `:beta` (MA beta channel) and `:nightly` (MA nightly channel), rebuilt daily.
> - Release images are tagged `:<version>` (e.g. `:1.2.3`) rather than `:<version>-latest` / `:<version>-beta`; per-build pins are the immutable companion tags `:<tag>-YYYYMMDD-<shortsha>-<run_id>`.

#### Official releases (tested)

Built **only** by the release workflow, triggered by pushing a git tag `v*` to this repository. Base image: `ghcr.io/music-assistant/server:latest`.

| Tag | Description |
| --- | --- |
| `:<version>` (e.g. `:2.1.0`) | The tested release image, pinned to the git tag (without the `v` prefix). |
| `:latest` | Moving tag pointing at the most recent tested **stable** release; not set by prerelease versions. **Recommended for production.** |

#### Automatic builds (untested)

Rebuilt automatically (daily schedule and on pushes to `main`) whenever the upstream base image changes. A base-image update can break the plugin at any time - use at your own risk.

| Tag | Base image | Description |
| --- | --- | --- |
| `:edge` | `ghcr.io/music-assistant/server:latest` | Rolling build for stable Music Assistant releases. **Untested.** |
| `:beta` | `ghcr.io/music-assistant/server:beta` | Rolling build for MA beta pre-releases (`2.X.0b*`/rc). **Untested.** |
| `:nightly` | `ghcr.io/music-assistant/server:nightly` | Rolling build for MA nightly dev builds (`2.X.0.dev*`). **Untested.** |
| `:<tag>-YYYYMMDD-<shortsha>-<run_id>` (e.g. `:edge-20260831-a1b2c3d-1234567890`) | — | Immutable companion tags for rollback, one per build (build date + commit short SHA + workflow run id). |

> [!NOTE]
> The `:latest` tag is reserved for official, tested releases and is never touched by automatic builds. If you want to live dangerously and track automatic builds, use `:edge` instead.
>
> The image was previously published as `ghcr.io/sproft/music-assistant-ytmusic`. That name is retired: existing tags stay pullable so running deployments keep working, but new builds are published only under the name above, so switch your compose file when convenient.
>
> For reproducible deployments, pin to an immutable tag such as `:<version>`, `:edge-YYYYMMDD-<shortsha>-<run_id>`, or a specific `@sha256:` digest.
>
> The daily build checks the base image digest and the stamped commit revision; variants are only rebuilt when the base image changed or a previous build was missed. The revision records the last commit that touched the build inputs (`ytmusic_free`, `Dockerfile`, `.dockerignore`), so docs-only commits do not force a rebuild.

## Installation: Home Assistant OS

The server runs as a Docker container (an HA add-on). The provider files must be copied **inside the container**. Placing them in `/config/` is not sufficient.

### Quick install (recommended)

One-line install from a shell with **host Docker access**. On Home Assistant OS that means the **Advanced SSH & Web Terminal** community add-on with **Protection mode off** (the official Terminal & SSH add-on is sandboxed and cannot reach Docker, so the script aborts with a `the 'docker' command was not found` error explaining how to proceed). On a Supervised install, a normal root SSH session works:

```bash
curl -fsSL https://raw.githubusercontent.com/sproft/ytmusic-free-provider/main/scripts/install_provider.sh | sh
```

The script auto-detects your MA container ID, Python version, and `/config` path, downloads the latest provider, stages it under `/config/custom_components/mass/providers/`, copies it into the MA container, and restarts MA.

To upgrade later, re-run with `--force` so the already-staged provider is overwritten without stalling on the interactive prompt (a `curl | sh` pipe has no terminal to answer it):

```bash
curl -fsSL https://raw.githubusercontent.com/sproft/ytmusic-free-provider/main/scripts/install_provider.sh | sh -s -- --force
```

Note the `sh -s --` separator, as with the watcher installer below; `curl ... | sh --force` makes the shell parse `--force` as its own option and fail.

> **Installing from a fork?** Both install scripts accept `--repo-owner OWNER` to download from your own fork instead of the default `sproft`. For example: `curl -fsSL .../install_provider.sh | sh -s -- --repo-owner youruser`.

> **No Docker in your shell?** On Home Assistant OS the watcher add-on route does not need Docker in your terminal: run `install_watcher_addon.sh` (see below), then install and start the **Provider Watcher** local add-on with Protection mode off. It injects the provider for you and keeps it installed across restarts.

Then jump to step 4 below to add the provider in the MA UI, and see [WATCHER_ADDON.md](WATCHER_ADDON.md) (or the quick installer further down) to make the install survive HA restarts.

### Manual install

### 1. Find your MA container name

In an HAOS / Supervised setup the container is named `app_d5369777_music_assistant` on current Supervisor versions, and `addon_d5369777_music_assistant` on older ones. Supervisor renamed the prefix, so which one you have depends on your version. Find yours:

```bash
docker ps --format '{{.Names}}' | grep music_assistant
```

The commands below use a `$MA` variable so you can paste them unchanged whichever prefix you have:

```bash
MA=$(docker ps --format '{{.Names}}' | grep -E '^(addon|app)_[0-9a-f]+_music_assistant(_beta|_nightly|_dev)?$' | head -n1)
echo "$MA"
```

If that prints nothing, the server is not running; start it and try again.

### 2. Copy the provider into the container

The provider lives in MA's Python `site-packages`, and the Python version moves over time (recent server builds use `python3.14`, older ones `python3.13`). Detect it instead of hard-coding the path:

```bash
PYVER=$(docker exec "$MA" sh -c 'ls /app/venv/lib' | grep -m1 '^python3')
docker cp /path/to/ytmusic_free \
  "$MA:/app/venv/lib/$PYVER/site-packages/music_assistant/providers/"
```

Replace `/path/to/ytmusic_free` with wherever you placed the folder (e.g. `/config/custom_components/mass/providers/ytmusic_free`). The one-line `install_provider.sh` runs this detection for you, so prefer it unless you are debugging.

### 3. Restart the server

```bash
docker restart "$MA"
```

> **Important:** Restarting MA from the Home Assistant UI recreates the container from its image, wiping any files you copied in. Always use `docker restart` to preserve the provider files.

### 4. Add the provider in MA

Go to **Settings → Apps → Add** in the MA UI. You should see **"YouTube Music (Free)"** listed. No credentials are required for basic playback.

### Adding more than one account

The provider is multi-instance, so you can add it several times and give each entry its own cookie, brand account ID, and audio quality setting. Repeat the step above once per account. The server labels the entries automatically once there is more than one, using the brand account ID where you set one, and you can rename any of them in its settings.

Typical reasons to run more than one:

| Setup | What each instance holds |
|-------|--------------------------|
| Personal plus brand account | The same cookie in both, with the **Brand account ID** field set on one of them |
| Two people in one household | A separate cookie per Google account, captured in separate incognito windows |
| Authenticated plus anonymous | One instance with your library, one without auth |

Each instance authenticates on its own and syncs only the account its own cookie resolves to. The server then merges what they sync into its single library, tagging every item with the instance it came from.

> **Capturing two cookies?** Use a separate incognito window per account. A browser signed in to several Google accounts at once sends one identical cookie for all of them, and the **Account index** field (the `X-Goog-AuthUser` header value) is then the only thing that tells them apart. Two separate incognito sessions avoid the problem entirely, since each has a single account at index 0.

> **Upgrading from an earlier version?** Your existing entry keeps working and needs no changes. On first start after the upgrade, the provider deletes the old `/data/ytmusic_browser_auth.json` file that previous releases used to store your cookie in plaintext.

### Keeping the provider across HA restarts

If you restart HA (not just MA), the container is recreated and the provider files are lost. The recommended fix is the **Provider Watcher** local add-on, which re-copies the provider whenever the MA container is recreated. One-line install from a host shell:

```bash
curl -fsSL https://raw.githubusercontent.com/sproft/ytmusic-free-provider/main/scripts/install_watcher_addon.sh | sh
```

To re-install or upgrade an existing watcher add-on without the overwrite prompt, pass `--force` through to the script with `sh -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/sproft/ytmusic-free-provider/main/scripts/install_watcher_addon.sh | sh -s -- --force
```

> Note the `sh -s --` separator. Writing `... | sh --force` makes the shell parse `--force` as one of its own options and fail with `sh: bad option '--force'`.

The installer auto-detects the local add-ons folder across the common layouts: the SSH/Samba add-on mapping (`/addons`), Home Assistant OS (`/mnt/data/supervisor/apps/local`, or legacy `.../addons/local`), and Supervised hosts. On **HAOS 18+** the Supervisor renamed the `addons` tree to `apps`, so the folder is `apps/local`. Older guides pointing at `addons/local` are out of date.

> **`could not find local add-ons directory`?** Pass the folder explicitly. From the HAOS host console:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/sproft/ytmusic-free-provider/main/scripts/install_watcher_addon.sh | sh -s -- --force --addons-dir /mnt/data/supervisor/apps/local
> ```
> Inside the SSH/Samba add-on use `--addons-dir /addons`. After re-running, **Rebuild** the add-on (three-dot menu) so the new files are baked into its image, then **Start** it.

The watcher can also **keep the provider up to date automatically**. Enable the `auto_update` option in the add-on's Configuration tab (opt-in, off by default) and it periodically checks GitHub and reinstalls the provider only when the code actually changed, so you don't have to re-run the installer on every upstream change. Turning it back off pins to the version baked into the add-on image. See [Auto-update](WATCHER_ADDON.md#auto-update) for the options and details.

See **[WATCHER_ADDON.md](WATCHER_ADDON.md)** for the manual procedure, troubleshooting, and the available installer flags.

> **If the automatic installer doesn't work on your system,** follow the manual procedure in **[WATCHER_ADDON.md](WATCHER_ADDON.md)**, which covers HAOS and Supervised installs, and please [open an issue](https://github.com/sproft/ytmusic-free-provider/issues/new) so the installer can be fixed. Work from the [latest release](https://github.com/sproft/ytmusic-free-provider/releases/latest) rather than an older tag: `v0.1.0-beta.1` is still published for anyone already pinned to it, but it is from May 2026 and predates podcast support, the AI-music filter, the fix for `403 Forbidden` playback failures, and the guard that stops an expired cookie from emptying your library.

---

## Authentication (optional)

Authentication is **not required** for search, browse, and playback. However, adding a browser cookie unlocks:

- Library sync (liked songs, saved albums, playlists, subscribed artists)
- Personalized recommendations (home feed)
- Library editing (add/remove items)

### Setup

1. In the MA UI, go to **Settings → Music sources** and open the **YouTube Music (Free)** entry you want to authenticate (if you added several, each one holds its own cookie)
2. Set **Authentication** to **Browser cookie**
3. Get your cookie (do this in a fresh **incognito / private window**, see the tip below):
   - Open a new incognito/private window and log in to `music.youtube.com`
   - Open DevTools (F12) → **Network** tab → reload the page
   - Click the first document request → under **Request Headers** find the `Cookie:` header → copy the full value
   - **Do not log out.** Just close the incognito window when you are done. Logging out invalidates the cookie.

> **Tip: use a dedicated incognito session.** Logging in through a new incognito/private window is the easiest way to grab a clean cookie. The session is isolated, so the `Cookie:` header carries only what YouTube Music needs and is shorter and easier to copy. More importantly, that session stays valid for as long as you never click **log out**: closing the window keeps the cookie alive (good for ~2 years). In your everyday browser, an accidental sign-out or Google rotating the session can invalidate the cookie later and silently break library sync.

4. Paste the cookie into the **Cookie header** field
5. **Brand accounts:** If your YouTube Music library is on a brand account, enter your brand account ID in the **Brand account ID** field. Find it at [myaccount.google.com/brandaccounts](https://myaccount.google.com/brandaccounts) or check the `X-Goog-PageId` header in DevTools. After logging into your Google account and selecting the correct Brand account you will find it here: ```https://myaccount.google.com/brandaccounts/THISISYOURIDRIGHTHERE/view```.
6. Click **Save**

The cookie must contain `__Secure-3PAPISID`, `SID`, `HSID`, and `SSID`. Cookies are valid for approximately 2 years unless you log out.

Your cookie is stored by the server itself, in the encrypted provider config it keeps for every provider, so it survives restarts and provider updates. You never need to re-enter it after an upgrade. The provider keeps the auth headers in memory for as long as the instance runs and writes no copy of its own; earlier releases also dropped a plaintext copy at `/data/ytmusic_browser_auth.json`, which is now removed automatically.

---

## Supported features

| Feature | Without auth | With auth |
|---------|:---:|:---:|
| Search (tracks, albums, artists, playlists, podcasts) | ✅ | ✅ |
| Add by pasting a YouTube / YTM link | ✅ | ✅ |
| Trim a video with `@start-end` timestamps | ✅ | ✅ |
| Stream audio | ✅ | ✅ |
| Artist top tracks / albums | ✅ | ✅ |
| Similar tracks (song radio) | ✅ | ✅ |
| Album / playlist tracks | ✅ | ✅ |
| Library sync (songs, albums, playlists) | ❌ | ✅ |
| Library artists (subscriptions + liked) | ❌ | ✅ |
| Personalized recommendations | ❌ | ✅ |
| Library editing (add/remove) | ❌ | ✅ |
| Multiple accounts side by side | ✅ | ✅ |
| Podcasts: search, browse a show, play episodes | ✅ | ✅ |
| Podcast library sync (subscribed shows) | ❌ | ✅ |

> **Podcasts.** Search for a show, open it to see its most recent episodes, and play them. This works without an account, since YouTube answers all three anonymously. Episode length comes from YouTube; playback position is tracked by the server itself, because YouTube does not report one.
>
> With a cookie, the shows you subscribe to sync into your library alongside your music. The two auto-generated entries YouTube returns there, "New Episodes" and "Saved episodes", are skipped: they are not shows, and syncing them would leave two pseudo-subscriptions in your library that you cannot remove. Saved individual episodes are not synced, because the server's library has no place to put an episode outside its show.
>
> The subscribed-shows sync is the one part of this provider that has not been checked against a live account, because doing so needs credentials. If your subscriptions do not appear, or appear wrongly, please say so in [#52](https://github.com/sproft/ytmusic-free-provider/issues/52) with what you see.

### Finding plain YouTube videos

Track search covers two YouTube Music tabs: the **Songs** tab (the official
catalog) and the **Videos** tab, which also indexes regular `youtube.com`
uploads such as live recordings and concert films ([#77]). Catalog songs are
listed first, videos below them, so keyword search finds a live set that never
made it into the catalog. Podcast episodes that YouTube mixes into the Videos
tab are left out; to reach an episode, search for its show under Podcasts and
open it.

[#77]: https://github.com/sproft/ytmusic-free-provider/issues/77

### Adding an arbitrary YouTube link

Keyword search only finds what YouTube Music's search index chooses to return.
To add one specific YouTube or YouTube Music item, **paste its URL directly
into the search box**. The provider detects the link and resolves it to the exact
item, placed first in the results, that you can then play or add to your library.

Recognized link formats:

- Songs / videos: `https://music.youtube.com/watch?v=…`, `https://www.youtube.com/watch?v=…`, `https://youtu.be/…`
- Playlists: `https://music.youtube.com/playlist?list=…`, `https://www.youtube.com/playlist?list=…`

Notes:

- A watch link that also carries a `list=` parameter resolves to the **song**, not the surrounding playlist.
- A pasted link bypasses any media-type filter, so a deliberate paste always resolves.
- Plain (non-Music) videos still play; their metadata (title, uploader) may be sparse.
- Albums are intentionally left to normal search, since YouTube albums already carry the metadata needed to surface there.

#### Related results use the video's name

When you paste a **track** link, the remaining results aren't matches on the raw
URL string. The provider looks up the video's title and runs a normal search on
that name, so the other results are related songs, albums and artists, while the
pasted video itself stays at the top.

#### Trimming a video (start / end timestamps)

Some great finds have an unrelated intro or an end-card with extra audio. Append a
`@start-end` trim spec to the link to play only part of the video:

```
https://youtu.be/VIDEOID @0:15-3:42      # play from 0:15 to 3:42
https://youtu.be/VIDEOID @15-222          # same, in plain seconds
https://youtu.be/VIDEOID @1m30s-          # from 1:30 to the natural end
https://youtu.be/VIDEOID @-3:42           # from the start to 3:42
```

Timestamps accept plain seconds (`15`), clock form (`3:42`, `1:02:03`) or unit
form (`1m30s`, `2h`, `90s`). The trim is encoded into the track, so it **persists**
when you save the song to your library or a playlist and replays trimmed every
time. The trimmed length is reflected in the duration/progress bar, and a `[start–end]`
label is shown so trimmed entries are easy to spot. (The spec is ignored for
playlist links.)

---

## Troubleshooting

**Provider doesn't appear in MA**
- Confirm the folder is named exactly `ytmusic_free` and contains both `__init__.py` and `manifest.json`.
- Verify the files are inside the container, not just in `/config/`.
- Check MA logs for import errors during startup.

**Track fails to play / `UnplayableMediaError`**
- yt-dlp may need updating: run `pip install -U yt-dlp` inside the MA container.
- Some tracks are region-locked or removed and cannot be streamed.

**Tracks skipped with `ytmusic is not available`**
- Some third-party tools (for example [Beatify](https://github.com/mholzi/beatify)) hand the server links in the form `ytmusic://track/<id>`. The server routes a media link by its scheme prefix, and `ytmusic://` belongs to the official premium YouTube Music provider. When that provider is not installed, the queue drops the item and logs `Skipping ytmusic://track/<id>: ytmusic is not available`.
- Rewrite the prefix to this provider's scheme, `ytmusic_free://track/<id>`, and the link resolves here. A track id is the raw YouTube video id and is identical on both providers, so the swap points at the same song. This is safe for `track/` links only. Album, artist, and playlist ids live in separate namespaces and will not map across.
- A provider cannot claim another provider's scheme, since the server core owns that routing, so the lasting fix belongs in the tool that emits the link. Background and discussion: [#31](https://github.com/sproft/ytmusic-free-provider/issues/31).

**Playlist shows "No playable items found"**
- Ensure you are on the latest version of this provider (playlist support uses a yt-dlp fallback added after the initial release).
- Very large playlists may take a few seconds to load as yt-dlp fetches the track list.

**Album release years, and how often this provider calls YouTube**
- Catalogue lookups (albums, artists, tracks, playlists) are cached: albums and tracks for 30 days, artists for 7, song radio for 1 day, playlists for 3 hours. Nothing about a released album changes, and the previous behaviour of re-fetching on every render was both slow and a good way to get rate-limited.
- Tracks now carry their album's release year. A track from a playlist or a search only tells us its album's id and name, so the year comes from an album lookup, which is affordable only because of the cache above: in steady state it costs nothing, and a cold cache costs one request per album rather than one per track. Bounded to 40 albums per call, 4 at a time.
- **Radio and mix tracks have no album at all** in YouTube's response, so they get no year. Nothing can be cached or looked up to change that.
- The year is the year of the *album entry the track was surfaced under*, not of the recording. A 1998 song on a 2025 compilation reads as 2025. Requested in [#53](https://github.com/sproft/ytmusic-free-provider/issues/53).

**Mixes look different every time I open them**
- Fixed in current versions. Auto-generated playlists (My Supermix, Discover Mix, song radio) come from YouTube's watch endpoint, which builds a new list on every request: two consecutive calls for the same mix returned 147 tracks each with nothing in common. Nothing was cached, so the playlist you were looking at was regenerated on each render, and every render cost a request to YouTube.
- The track list is now reused for three hours, so a mix stays put while you browse it. Playback is unaffected: The server asks for fresh tracks when it fills a queue, which bypasses the cache, so a dynamic playlist still gives you new music when you play it rather than when you look at it. Background: [#56](https://github.com/sproft/ytmusic-free-provider/issues/56).

**Audio quality is low**
- Enable "Prefer highest audio quality" in the provider settings (on by default).
- With it enabled you normally get Opus in a WebM container at roughly 130 to 160 kbps. Disabling it restricts playback to AAC, usually around 128 kbps, for players that cannot handle Opus. Some accounts and regions are offered nothing better than a 48 kbps AAC stream.

**Filtering out AI-generated music**
- Turn on **Filter AI-generated music** in the provider settings, then give it something to work with. The toggle on its own filters nothing.
- **Blocked artists** takes one entry per line, or several separated by semicolons. An entry is either an artist name or a YouTube channel id (`UC...`). Semicolons rather than commas on purpose: commas are common inside real artist names, and splitting on them would turn `Earth, Wind & Fire` into a rule that blocks everyone called Earth. Names match loosely, ignoring case and extra spacing; channel ids match exactly and are the better choice when two artists share a name. Lines starting with `#` are ignored.
- **Blocklist URL** is optional and points at a list somebody else maintains, merged with your own entries. It accepts a JSON array of names or channel ids, a JSON object with an `artists` key, or plain text one entry per line. It refreshes in the background roughly twice a day, and if the URL is unreachable the previous list stays in effect rather than the filter quietly switching itself off.
- **Scope:** this applies to auto-generated lists only, which is where unrequested music arrives: radio, mixes, similar tracks and the home feed. Search results, your library and playlists you picked yourself are never filtered, so looking up a blocked artist on purpose still finds them.
- A track is dropped when any of its artists matches. Tracks whose artist could not be read are always kept, so a parsing gap never silently removes music.
- Detection itself is out of scope here: the provider filters against a list, it does not judge whether a track is AI-generated. For a provider-agnostic approach that analyses the queue, see the discussion in [#53](https://github.com/sproft/ytmusic-free-provider/issues/53).

**Playback fails with 403 on some tracks**
- Fixed in current versions. YouTube puts a pre-roll ad in front of some tracks, and the stream URL it returns is not valid until that ad window has passed. The provider now waits it out before handing the URL to the server. Before the fix these tracks resolved fine and then failed at playback, logging `Skipping unplayable item`.
- This needs yt-dlp 2025.12.8 or newer, which the manifest now requires. `pip` will not upgrade a requirement that is already satisfied, so an installation that first resolved before that floor keeps its old yt-dlp. Check with `python -c "import yt_dlp; print(yt_dlp.version.__version__)"` inside the server container and upgrade with `pip install --upgrade "yt-dlp[default]"` if it is older, then restart the server so the running process picks up the new module.
- Releases between 2025.08.20 and 2025.11.12 report the ad window on every track whether or not there is an ad. The provider detects those and skips the wait, so it does not add a delay to every track. Background: [#51](https://github.com/sproft/ytmusic-free-provider/issues/51).

**Podcasts never sync, and the log says the installed ytmusicapi has no `get_library_podcasts()`**
- Upgrade ytmusicapi inside the server container with `pip install --upgrade ytmusicapi`, then **restart the server**. The restart is load-bearing: the old module is already imported in the running process, and neither the upgrade nor a provider reload replaces it. You have it right when the `get_library_podcasts failed` warning stops appearing and the podcast sync completes.
- Check the installed version with `python -c "import ytmusicapi; print(ytmusicapi.__version__)"`. That command starts a fresh process, so it reports the new version the moment pip finishes while the provider is still running the old one. Treat it as confirmation that pip worked, not that the provider picked it up.
- The cause is the same trap as the yt-dlp floor above. The provider asks pip for `ytmusicapi` without a version, and pip leaves a requirement alone once it is satisfied, so an installation that already had an older copy never reaches the 1.7.0 floor the manifest asks for.
- Until it is upgraded that category fails its sync rather than reporting no subscriptions. That is deliberate: reporting an empty list would make the server unsubscribe you from every show. Background: [#64](https://github.com/sproft/ytmusic-free-provider/issues/64).

**Cookie authentication failed**
- Make sure you copied the **entire** cookie string from the Network tab (2000+ characters).
- The cookie must contain `__Secure-3PAPISID`, `SID`, `HSID`, and `SSID`.
- If you use a brand account, enter the brand account ID (21-digit number from [myaccount.google.com/brandaccounts](https://myaccount.google.com/brandaccounts)).

**My library keeps emptying, and re-applying the cookie fixes it**
- Fixed in current versions. YouTube does not answer a lapsed session with an auth error: it answers HTTP 200 with a logged-out payload, which reads as an empty library. The server treats a completed sync as authoritative, so anything it held that the provider did not return this round is dropped from the library and unfavourited. An expired cookie therefore did not just stop refreshing your library, it deleted it.
- Two things made this silent. The provider announced "library sync enabled" after a validation call that a dead cookie passes, and its guard against empty syncs only armed once a category had been seen populated *in the current process*, so any restart disarmed it.
- Now: the cookie is verified against the account at startup, an empty sync is checked against the live session every time rather than only sometimes, and a sync that cannot be authenticated fails loudly instead of reporting an empty library. A failed sync leaves your library untouched.
- The same rule now covers a library call that fails outright. A rate limit, a timeout, or a response whose shape YouTube changed used to be written to the log and then answered with an empty list, which the server read exactly the way it read a lapsed cookie. Any failure in a library call now fails that sync and leaves your library where it was. This includes a failure that only affects half a category: artists are read as two separate requests, and one of them failing used to delete everything the other did not cover.
- What that looks like: the affected sync shows as failed in the server UI with the reason attached, nothing is removed in the meantime, and the next scheduled run picks it up. **If a sync keeps failing, do not remove and re-add the provider.** That deletes the library outright, which is the thing this protects you from. A sync failing every time is telling you something real, usually an expired cookie or an ytmusicapi that needs upgrading. Background: [#64](https://github.com/sproft/ytmusic-free-provider/issues/64).
- This does not extend how long a cookie lasts. That is Google's decision and nothing here can change it. It does mean an expired cookie costs you a warning in the log rather than your favourites. Background: [#55](https://github.com/sproft/ytmusic-free-provider/issues/55).

**Library is empty after auth**
- Your YouTube Music library only shows content you've explicitly liked, saved, or subscribed to.
- If your library is on a brand account, make sure the brand account ID is set.
- Running several instances? Check that you authenticated the one you are looking at. Each entry holds its own cookie, and an entry left on **Authentication: None** syncs nothing.

**Two instances show the same library**
- The usual cause is capturing both cookies from one browser that has several Google accounts signed in. Google sends the **same** cookie for every account in that session, and only the `X-Goog-AuthUser` index says which account a request means, so recapturing the cookie "for the other account" changes nothing. Either capture each cookie in a separate incognito window signed in to one account only, or set the **Account index** field on the second instance to that account's `X-Goog-AuthUser` value (visible on any `youtubei/v1/...` request in DevTools).
- If both entries point at the same personal account on purpose, set the **Brand account ID** on one of them to split them apart.

**Install script fails with "MA container not found"**
- Supervisor renamed add-on containers from `addon_*` to `app_*`. Current versions of the installer detect both, so update the script first by re-running the one-line install. If you are on an older copy, pass the name explicitly: `--ma-id app_d5369777_music_assistant`.
- Find the real name with `docker ps --format '{{.Names}}' | grep music_assistant`.
- If you installed the watcher add-on before this fix, it baked the old `addon_*` name into its `run.sh` and has been silently doing nothing since the rename: no error, because the name was right when it was written. Current versions re-detect at runtime and log that they have adapted, so re-running the watcher installer once is enough. Background: [#54](https://github.com/sproft/ytmusic-free-provider/issues/54).

**Files disappear after restarting Home Assistant**
- Only use `docker restart "$MA"` to restart MA (see "Find your MA container name" above).
- Restarting HA from the UI recreates the container from scratch. See [WATCHER_ADDON.md](WATCHER_ADDON.md) to set up automatic re-copying.

---

## Dependencies

These are installed automatically by the provider on first run via the server's `install_package` utility:

- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
- [`ytmusicapi`](https://github.com/sigma67/ytmusicapi)

> [!NOTE]
> The first run requires outbound network access and a writable virtual environment (`/app/venv`) because the dependencies above are `pip`-installed at setup time. Subsequent starts use the cached packages.

---

## Credits

Built and maintained by [@sproft](https://github.com/sproft), with features and fixes contributed by the community:

- **[@mawoka-myblock](https://github.com/mawoka-myblock):** noticing that every track was streaming at 48 kbps and fixing the format selector to rank audio by bitrate rather than by container, which also restored audio-only extraction on current yt-dlp ([#44](https://github.com/sproft/ytmusic-free-provider/pull/44)).
- **[@jojo141185](https://github.com/jojo141185):** automated Docker image builds published to GHCR, so standalone Docker and Compose users can run the server with the provider baked in ([#33](https://github.com/sproft/ytmusic-free-provider/pull/33)).
- **[@bygadd](https://github.com/bygadd):** opt-in auto-update for the Provider Watcher add-on, keeping the provider current from GitHub without a manual reinstall ([#32](https://github.com/sproft/ytmusic-free-provider/pull/32)).
- **[@gusjengis](https://github.com/gusjengis):** resolving a pasted YouTube or YTM link directly from the search box, plus the `@start-end` video trimming feature ([#29](https://github.com/sproft/ytmusic-free-provider/pull/29)).
- **[@bsny](https://github.com/bsny):** correct parsing of artists from search results, plus the `--repo-owner` option for the install scripts so forks can install from their own copy ([#26](https://github.com/sproft/ytmusic-free-provider/pull/26)).

Contributions are welcome. Please [open an issue](https://github.com/sproft/ytmusic-free-provider/issues) or a pull request.

---

## Legal Disclaimer & Terms of Use

### 1. 100% Free, Open-Source & Strictly Non-Commercial

This project is fully open-source (FOSS), created purely for educational purposes and personal use. **It is not sold, monetized, or distributed commercially in any way.** There are no advertisements, no premium tiers, no subscriptions, and no financial intent behind it whatsoever. Any form of commercial use is explicitly prohibited.

### 2. A Thin Client, Not a Piracy Tool

This provider acts strictly as a thin client that queries publicly accessible YouTube and YouTube Music APIs and passes the resulting stream URLs to the media server for local playback, the same way a web browser with an ad-blocking extension would render the same content. It does not circumvent DRM, does not download or cache media to disk, and does not redistribute any audio or video content.

### 3. No Hosting of Copyrighted Material

This project does not host, upload, store, or redistribute any audio, video, or copyrighted media. All content accessed through this provider remains stored exclusively on Google's / YouTube's servers and is the property of the respective copyright holders. This project merely resolves publicly accessible stream URLs for personal, local playback.

### 4. Support the Artists You Listen To

We strongly encourage all users to subscribe to [YouTube Premium](https://www.youtube.com/premium). A Premium subscription is the most direct way to financially support the musicians and creators whose work you enjoy, and to support the platform that hosts it. This project exists as a technical proof-of-concept for developers and home automation enthusiasts. It is not intended to deprive creators of revenue.

### 5. YouTube Terms of Service

This provider interacts with YouTube's internal (unofficial) APIs without a premium account. **This is against YouTube's Terms of Service.** By using this software you acknowledge that:

- You use it entirely at your own risk.
- The developers accept no liability for account suspensions, legal action, or any other consequences arising from its use.
- This project is not affiliated with, endorsed by, or connected to Google LLC or YouTube in any way.
- Google may change their APIs at any time, which may break functionality.

### 6. User Responsibility

The software is provided **"AS IS"**, without warranty of any kind. Users are solely responsible for ensuring their use of this project complies with their local laws and the Terms of Service of any platforms they access through it. Because no media files are hosted by this project, DMCA takedown requests for audio or video content cannot be processed here. Such requests should be directed to Google / YouTube directly.

### 7. Independent Project

This is an independent community project. It is not affiliated with, endorsed by, sponsored by, or supported by the Music Assistant project, the Open Home Foundation, Home Assistant, Google LLC, or YouTube. All product names are used solely to describe compatibility and belong to their respective owners. The upstream server maintainers have asked not to be involved with this project; direct every question about it to this repository only.

---

## License

[MIT](LICENSE)
