"""YouTube Music (Free) provider for Music Assistant.

Streams YouTube Music without a premium subscription by:
- Using ytmusicapi for search/metadata (optionally with browser cookie auth)
- Using yt-dlp with the iOS client to extract stream URLs (no PO token needed)

Authentication is optional. Without it, search/browse/playback work fine.
With browser cookie authentication, library sync and recommendations unlock.

Note: This uses YouTube's internal APIs in an unofficial manner, similar to how
apps like SimpMusic work. This may break if YouTube changes their API.
"""

from __future__ import annotations

# The one place this provider's version is recorded. A release is cut by pushing
# a tag, and .github/workflows/release.yml refuses to publish when the tag and
# this string disagree, so a release and the code inside it can never claim
# different numbers.
#
# It lives here rather than in manifest.json for two reasons. Only the
# ytmusic_free/ directory ships (.dockerignore is "*" then "!ytmusic_free", and
# both installers copy just this directory), so a repo-root VERSION file would
# never reach an install. And Music Assistant would throw it away anyway:
# ProviderManifest has no version field and mashumaro drops unknown keys, so the
# manifest object handed to the provider never carries one. See issue #68.
__version__ = "1.2.0"

import asyncio
import importlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    AlbumType,
    ConfigEntryType,
    ContentType,
    EventType,
    ImageType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    SetupFailedError,
    UnplayableMediaError,
)

try:
    # The structured provider error behind the auth notice. Music Assistant 2.9
    # stored last_error as a plain string and has neither this class nor
    # update_provider_last_error, so there the notice is skipped and the log
    # warning is all the user gets.
    from music_assistant_models.config_entries import ProviderError
except ImportError:  # pragma: no cover - Music Assistant 2.9.x
    ProviderError = None
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    MediaType,
    Playlist,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.util import infer_album_type, install_package, parse_title_and_version
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType


YTM_DOMAIN = "https://music.youtube.com"
VARIOUS_ARTISTS_YTM_ID = "UCUTXlgdcKU5vfzFqHOWIvkA"
DEFAULT_STREAM_URL_EXPIRATION = 3600  # 1 hour

# googlevideo answers 403 to any request whose Range header is missing or
# open-ended ("bytes=0-"), while a bounded "bytes=0-1023" against the very same
# URL answers 206 (the rqh=1 URL parameter; enforcement observed 2026-08-19,
# breaking every install at once with extraction still green). ffmpeg only ever
# opens URLs the unbounded way, so the URL cannot be handed to Music Assistant
# as StreamType.HTTP; the provider fetches the bytes itself in bounded chunks
# instead.
#
# Bounded is not enough: the range's *size* is capped too. Measured on
# 2026-08-19 against a real ANDROID_VR URL: 1 MiB answers 206 and 2 MiB
# answers 403 (the earlier 10 MiB, chosen to mirror yt-dlp's http_chunk_size,
# 403d in production while every diagnostic that probed with small ranges
# passed). 1 MiB is the largest measured-good size; get_audio_stream also
# halves the chunk on a 403 down to STREAM_CHUNK_FLOOR before failing, so a
# future tightening of the cap degrades to smaller requests instead of
# breaking playback outright.
STREAM_CHUNK_SIZE = 1024 * 1024

# Give-up point for that halving. Below this, request overhead dominates and a
# 403 is clearly not about the range size anymore (1 KiB probes pass even when
# 10 MiB fails), so keep shrinking would only mask a different failure.
STREAM_CHUNK_FLOOR = 64 * 1024

# How much of a chunk's body to hand over per yield. Purely a buffering knob
# for the pipe into ffmpeg; it has no effect on the HTTP requests.
STREAM_YIELD_SIZE = 64 * 1024

# Longest pre-roll window we will hold a track start for. A single skippable
# ad is around 5 seconds (issue #51 measured 4 to 5) but yt-dlp sums a whole
# pod, and two non-skippable ads back to back reach the mid-thirties, so this
# has to clear that or it would refuse legitimate waits.
#
# Past it we hand the URL over *immediately* rather than sleeping the cap and
# handing it over anyway. Waiting 45 seconds and then serving a URL we already
# know is still gated is the worst of both outcomes: the user gets the silence
# and the failure. Failing at once at least skips to the next track promptly.
MAX_PREROLL_WAIT = 45.0

# The oldest yt-dlp that can still stream at all. YouTube killed the
# android_vr client on 2026-08-17: its URLs serve at most ~1 MiB in total and
# then answer 403 to every request, bounded or not, paced or not (all of it
# measured 2026-08-19), so no fetch strategy on this side can play a full
# track from them. yt-dlp dropped android_vr from its default clients in
# nightly 2026.08.18 (yt-dlp/yt-dlp#17461); those defaults extract via
# visionos, verified the same day to serve whole files with no PO token.
#
# The ``.dev0`` spelling is load-bearing twice over: it is the version PyPI
# actually carries for the nightly, and PEP 440 only lets pip consider
# pre-releases when the specifier itself names one, so this floor installs
# nightlies without any --pre flag. Replace with the next stable release once
# one exists above it.
MIN_YTDLP_REQUIREMENT = "yt-dlp[default]>=2026.8.18.122307.dev0"

# The same floor as a comparable date tuple, for warning at stream time when
# the *imported* module is still older: install_package upgrades the package
# on disk, but a Python process that already imported yt_dlp keeps the old
# module until Music Assistant restarts.
MIN_YTDLP_VERSION_FOR_PLAYBACK = (2026, 8, 18)

# ``available_at`` first appeared in yt-dlp 2025.08.20, but it only became
# ad-derived in 2025.12.08. On the releases in between it is a flat six seconds
# in the future on *every* non-live format, ad or not, so honouring it there
# would put six seconds of silence in front of every single track. Older
# releases omit the field entirely and need no guard.
#
# The manifest floor now excludes that range, but this check still has to
# exist: pip does not upgrade an already-satisfied requirement, so an install
# that first resolved under the old floor keeps whatever yt-dlp it landed on
# and never sees the new one.
MIN_YTDLP_VERSION_FOR_PREROLL = (2025, 12, 8)

# Song radio and the personal mixes are effectively endless, so a full fetch has
# no natural stopping point. Ask for a queue's worth and stop there.
#
# This is a floor, not a ceiling: ytmusicapi documents the argument as the
# minimum to return, and stops requesting continuations once it holds that
# many, so a real result overshoots by whatever the last batch contained. 100
# here measured 147 tracks for song radio.
#
# It is also the whole fetch rather than a first page. get_watch_playlist takes
# a limit but exposes no offset, so a second page could only be had by
# refetching from the start and slicing off what we already returned, and radio
# does not hand back a stable sequence across calls, so those slices would
# duplicate some tracks and skip others. get_playlist_tracks accordingly
# returns nothing for page > 0.
RADIO_PLAYLIST_LIMIT = 100

# How long a playlist's track list is reused before it is fetched again.
# Short, because the whole point is that auto-generated mixes change: long
# enough that browsing one twice shows the same thing, short enough that it
# still turns over during a day. Music Assistant bypasses this entirely for
# playback and refill, so it only ever affects what you are looking at, never
# what you are hearing. Same figure the official ytmusic provider uses.
PLAYLIST_TRACKS_CACHE_TTL = 3 * 3600

# Catalogue metadata: an album's title, artwork and release year do not change
# after it is published, and an artist's discography changes a few times a year
# at most. Long lifetimes here are what make the album-year lookup below
# affordable, and they cut the request volume that raises the bot-detection risk
# raised in issue #53. Same figures the official ytmusic provider uses.
CATALOG_CACHE_TTL = 30 * 24 * 3600
ARTIST_CACHE_TTL = 7 * 24 * 3600

# Stored as the cache checksum on the long-lived lookups that return tracks
# (get_track, get_album_tracks, get_artist_toptracks). Music Assistant treats an
# entry whose checksum differs as a miss, so bumping this when a change to
# _parse_track alters what tracks contain retires each old entry on its next
# lookup, well before its 30 days run out. Albums and artists are left alone so
# the album-year lookups keep their warm cache; the short-lived caches turn
# over on their own.
# 2: THUMB fallback for video uploads (issue #90).
TRACK_CACHE_VERSION = "2"

# Song radio is meant to differ each time it is asked for, so it gets a short
# window: long enough that paging through a queue is stable, short enough that
# asking again tomorrow gives you different music.
SIMILAR_TRACKS_CACHE_TTL = 24 * 3600

# Album lookups run to fill in release years on tracks that only carry an album
# id. Bounded on both axes: at most this many at once, so a cold cache does not
# arrive as a burst of parallel requests, and never more than this many distinct
# albums per call, so one pathological playlist cannot turn into hundreds.
ALBUM_YEAR_LOOKUP_CONCURRENCY = 4
ALBUM_YEAR_LOOKUP_LIMIT = 40

# Features that work without a YTM account
BASE_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
    ProviderFeature.BROWSE,
}

# Additional features unlocked by browser cookie authentication
AUTHENTICATED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LIBRARY_ARTISTS_EDIT,
    ProviderFeature.LIBRARY_ALBUMS_EDIT,
    ProviderFeature.LIBRARY_PLAYLISTS_EDIT,
    # A YTM "library track" is a liked song, so add/remove map to rating the
    # song. Without this a delete only touched Music Assistant's database and
    # the next sync re-imported the like (issue #82).
    ProviderFeature.LIBRARY_TRACKS_EDIT,
    # Subscribed shows. Safe to declare unconditionally alongside the others
    # only because get_library_podcasts goes through the same guards as every
    # other library method: without auth it raises rather than reporting an
    # empty library, so Music Assistant never reads it as "unsubscribe from
    # everything". See issue #55.
    ProviderFeature.LIBRARY_PODCASTS,
}

# YTM search filter per media type. YTM has no multi-type search, so a search
# spanning several types runs one filtered call per type and merges the results.
SEARCH_FILTER_BY_TYPE = {
    MediaType.ARTIST: "artists",
    MediaType.ALBUM: "albums",
    MediaType.TRACK: "songs",
    MediaType.PLAYLIST: "playlists",
    MediaType.PODCAST: "podcasts",
}

# Second search pass for tracks (issue #77). The "songs" filter only sees the
# YTM catalog, so live recordings and other plain-YouTube uploads never
# surface through it; the "videos" tab does index them. Kept out of
# SEARCH_FILTER_BY_TYPE because it is an addition to the TRACK search, not a
# type of its own.
SEARCH_FILTER_VIDEOS = "videos"

# The "videos" filter also returns podcast episodes (their resultType is
# forced to "video" by the filter); those belong to the podcast domain, not in
# track results.
VIDEO_TYPE_PODCAST_EPISODE = "MUSIC_VIDEO_TYPE_PODCAST_EPISODE"

# How much better a video has to match the query than the best catalog song
# before it is ranked ahead of the songs, as a fraction of the query's tokens.
# Videos are ranked below songs by default, but Music Assistant shares one
# capped result window between every enabled provider, so a tail-ranked video
# is invisible as soon as the user has a second provider (issue #77). A video
# that covers substantially more of the query than any song does is what the
# user was searching for, so it has to survive that cut.
VIDEO_PROMOTION_MARGIN = 0.25

# Podcast episode ids carry their show with them: "<podcastId>|<videoId>". Music
# Assistant looks an episode up on its own, without the show for context, and
# PodcastEpisode requires a podcast, so the id has to be enough to rebuild both.
# Matches the official ytmusic provider's separator, and "|" cannot appear in a
# YouTube id or playlist id. See issue #52.
PODCAST_EPISODE_SPLITTER = "|"

# Search returns a show as "MPSP" + its playlist id, while get_podcast expects
# the bare playlist id. Strip on the way in so one id shape flows through.
PODCAST_BROWSE_PREFIX = "MPSP"

# Episodes per show. get_podcast pages beyond this, but a queue's worth of the
# most recent episodes is what a browse is for, and every extra page is another
# request against a service that rate-limits.
PODCAST_EPISODE_LIMIT = 100

# Shows in the library. Higher than the episode limit because this is a flat
# list of subscriptions rather than a per-show fetch, and a truncated library
# is the one thing Music Assistant reads as "you unsubscribed".
LIBRARY_PODCAST_LIMIT = 9999

# YouTube returns two auto-generated playlists alongside real subscriptions:
# "New Episodes" (RDPN) and "Saved episodes" (SE). They are not shows, they do
# not answer to get_podcast, and syncing them into the library would put two
# permanent pseudo-subscriptions there that the user cannot remove. The official
# ytmusic provider skips them for the same reason.
PERSONAL_PODCAST_PLAYLIST_IDS = frozenset({"RDPN", "SE"})

# Playlists that exist only inside a signed-in account: liked songs ("LM") and
# the auto-generated podcast lists above. The yt-dlp fallback runs without
# cookies, so for these it can never return anything; YouTube answers "The
# playlist does not exist", which reads like data loss in the log (issue #82).
PRIVATE_PLAYLIST_IDS = frozenset({"LM"}) | PERSONAL_PODCAST_PLAYLIST_IDS

# Shows and episodes change far more slowly than a mix does: a new episode
# appears weekly at best, and the description and artwork essentially never
# change. Same stale-while-revalidate treatment as playlist tracks.
PODCAST_CACHE_TTL = 6 * 3600

# "3 hr 46 min", "31 min", "1 hr". YouTube spells episode lengths in words
# rather than the clock format used for tracks, so _parse_timestamp cannot read
# them.
DURATION_WORDS_RE = re.compile(
    r"(?:(?P<hours>\d+)\s*(?:hours?|hrs?|h)\b)?\s*"
    r"(?:(?P<minutes>\d+)\s*(?:minutes?|mins?|m)\b)?\s*"
    r"(?:(?P<seconds>\d+)\s*(?:seconds?|secs?|s)\b)?",
    re.IGNORECASE,
)

CONF_AUTH_TYPE = "auth_type"
CONF_COOKIE = "cookie_header"
CONF_BRAND_ACCOUNT = "brand_account"
CONF_AUTH_USER = "auth_user"
CONF_PREFER_AUDIO_QUALITY = "prefer_audio_quality"
CONF_FILTER_AI_MUSIC = "filter_ai_music"
CONF_AI_BLOCKLIST = "ai_blocklist"
CONF_AI_BLOCKLIST_URL = "ai_blocklist_url"

# How long a fetched remote blocklist is trusted before a refresh is scheduled.
# These lists change on the order of days, and the refresh happens in the
# background off a stale read, so nothing waits on it. See issue #53.
AI_BLOCKLIST_TTL = 12 * 3600

# Seconds to wait on the remote list before giving up. Short on purpose: a
# slow or dead host must not delay the radio call that noticed the staleness.
AI_BLOCKLIST_TIMEOUT = 15

# How long to leave a failed fetch alone before trying again. Without this the
# staleness check re-fires on every filtered call, because a failure never
# advances the fetched-at stamp, producing a request per queue build and a
# warning line to match.
AI_BLOCKLIST_RETRY_AFTER = 600

# A YouTube channel id. Used to tell "block this exact channel" apart from
# "block anything by this artist name", because names collide and ids do not.
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

AUTH_TYPE_NONE = "none"
AUTH_TYPE_COOKIE = "cookie"

# Releases before multi-instance support wrote the browser auth headers to this
# fixed path inside the MA container and handed ytmusicapi the filename. Auth is
# now kept in memory, so the file is dead weight holding a plaintext cookie.
# Deleted on setup so an upgrade cleans up after the old code. See issue #40.
LEGACY_AUTH_FILE = "/data/ytmusic_browser_auth.json"

# Cookie names that __Secure-3PAPISID alone cannot replace. A cookie capture
# that passes the hard check but is missing these often validates at init
# (single trivial call) and then fails for broader library queries minutes
# or hours later, with no obvious error to the user. See issue #6.
RECOMMENDED_AUTH_COOKIES = ("__Secure-1PSID", "__Secure-3PSID", "SAPISID")

# Patterns in exception messages that suggest an auth lapse, as opposed to a
# transient network or parsing error. ytmusicapi surfaces httpx.HTTPStatusError
# with the response status in the message.
#
# A bare "401" substring was too loose: it matched any message that happened to
# contain those digits anywhere, and the strings we pass through here routinely
# carry video ids and track titles. "Error 401 while fetching abc401xyz" and
# "no formats for video 4012ab" were indistinguishable, so an ordinary
# extraction failure could be reported to the user as an expired cookie. Anchor
# the status to how an HTTP error actually spells it instead.
# 403 is deliberately absent. It has its own meaning here (the owned-playlist
# no-op path) and reporting it as an expired cookie would send people to
# re-capture a cookie that is fine.
AUTH_LAPSE_ERROR_PATTERN = re.compile(
    r"(?:\b401\s+(?:Client\s+)?Error\b"
    r"|\bstatus[_ ]code[=: ]+401\b"
    r"|\bHTTP\s+401\b"
    r"|\bUnauthorized\b"
    r"|\bnot\s+authenticated\b"
    r"|\bauthentication\s+(?:failed|required)\b"
    # ytmusicapi's own wording when the client has no credentials at all
    # (YTMusic._check_auth raises YTMusicUserError with exactly this text).
    r"|\bprovide\s+authentication\b)",
    re.IGNORECASE,
)

# The notice shown in the Music Assistant UI while cookie authentication is
# configured but not working (issue #92). Before it, the only sign was a warning
# in the log: search and playback kept working, so nothing looked wrong while
# library sync had stopped. Music Assistant renders it as the provider's error,
# with an "Authentication required" badge in the provider list and the text as
# Markdown on the provider's settings page.
AUTH_NOTICE_COOKIE_FAILED = (
    "Cookie authentication failed, so this provider is running in anonymous "
    "mode. Search and playback still work, but library sync and recommendations "
    "are off. Copy a fresh Cookie header from a signed-in music.youtube.com tab "
    "into the Cookie header field of this provider's settings and save. "
    "Error: `{reason}`"
)
AUTH_NOTICE_COOKIE_MISSING = (
    "Cookie authentication is selected but the Cookie header field is empty, so "
    "this provider is running in anonymous mode. Search and playback still work, "
    "but library sync and recommendations are off. Paste the Cookie header from a "
    "signed-in music.youtube.com tab and save, or set Authentication to "
    "None (anonymous)."
)
AUTH_NOTICE_COOKIE_LAPSED = (
    "YouTube stopped accepting the cookie while this provider was running, so "
    "library sync is failing. Copy a fresh Cookie header from a signed-in "
    "music.youtube.com tab into the Cookie header field of this provider's "
    "settings and save. Error: `{reason}`"
)

# The notice quotes the error so a bug report can be made from a screenshot,
# but some of them (the 401 from Google) run to several sentences.
AUTH_NOTICE_REASON_MAX = 200

# A cookie pair as it appears inside an error message: name= then the value.
_COOKIE_VALUE_RE = re.compile(r"([\w.-]+=)[^;\s'\"]+")

# Music Assistant's core search controller sanitizes the query before handing it
# to a provider, replacing every "/" with a space and stripping "'"
# (controllers/music.py: search_query.replace("/", " ").replace("'", "")). This
# destroys the "://" and path separators of a pasted URL, so urlparse can no
# longer recognize it. These regexes recover the YouTube id from either the
# original URL or that mangled form. A YouTube video id is 11 chars of
# [A-Za-z0-9_-]; playlist ids are longer and use the same alphabet.
_YT_HOST_TOKEN = re.compile(r"(?:^|[^\w.])(?:music\.|m\.|www\.)?youtube\.com(?:[/\s?]|$)", re.I)
_YT_SHORT_TOKEN = re.compile(r"(?:^|[^\w.])youtu\.be[/\s]+([\w-]{11})", re.I)
_YT_VIDEO_ID = re.compile(r"[?&\s]v=([\w-]{11})", re.I)
_YT_LIST_ID = re.compile(r"[?&\s]list=([\w-]{10,})", re.I)

# "RD" covers two different things, and the difference decides which endpoint
# can answer for an id. Issue #47.
#
# Radio proper: song radio ("RD<videoId>", "RDAMVM<videoId>") and the
# auto-generated personal mixes ("My Supermix" RDTMAK5uy_...). Nothing that
# works for a normal playlist works on song radio: youtube.com/playlist?list=RD
# answers "This playlist type is unviewable", and ytmusicapi's get_playlist
# raises a KeyError parsing the response. Only a watch URL or
# get_watch_playlist will do.
#
# Editorial playlists: "RDCLAK5uy_..." is not radio at all. These are YouTube
# Music's own curated playlists ("'80s Pop", "Happy Pop Hits"), they answer
# get_playlist normally, and they are most of what the home feed hands out. The
# watch endpoint does answer for them, but it stops at a queue's length, so
# sending them there loses tracks: measured live, "'80s Pop" is 200 tracks
# through get_playlist and 101 through the watch endpoint.
_YT_RADIO_PREFIX = "RD"
_YT_EDITORIAL_PREFIX = "RDCLAK5uy_"
# Song radio embeds its seed video id ("RD<videoId>", "RDAMVM<videoId>"); the
# personal mixes do not, so they need a seed supplied from elsewhere. The
# trailing {11} anchor is what tells the two apart: RDTMAK5uy_kset8Dis... is far
# longer than a video id, so it correctly fails to match.
_YT_RADIO_SEED_RE = re.compile(r"^RD(?:AMVM)?([\w-]{11})$")

# Trim spec: an optional "@start-end" suffix a user appends to a pasted link to
# play only part of a video (e.g. to skip an intro or an unrelated end-card).
# Examples: "@15-222", "@0:15-3:42", "@1m30s-", "@-3:42". The "@", ":" and "-"
# characters all survive MA's query sanitization (which only strips "/" and
# "'"), so this works whether typed into the search box or used in a raw URL.
# A YouTube video id is [A-Za-z0-9_-]{11} and never contains "@", so the same
# "VIDEOID@start-end" encoding is reused as the persistent track item_id.
_YT_TRIM_RE = re.compile(r"@\s*([0-9hms:.]*)\s*-\s*([0-9hms:.]*)\s*$", re.I)
# A single timestamp token: bare seconds ("15", "15.5"), clock form ("3:42",
# "1:02:03") or unit form ("1m30s", "2h", "90s").
_TS_CLOCK_RE = re.compile(r"^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$")
_TS_UNIT_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.I)


def _parse_timestamp(value: str) -> int | None:
    """Parse a timestamp token into whole seconds, or ``None`` if invalid.

    Accepts bare seconds (``"15"``), clock form (``"3:42"`` / ``"1:02:03"``) and
    unit form (``"1m30s"`` / ``"2h"`` / ``"90s"``). An empty string means
    "unbounded" and returns ``None`` (callers treat that as no bound).
    """
    if not value:
        return None
    token = value.strip().lower()
    if not token:
        return None
    if _TS_CLOCK_RE.match(token):
        parts = token.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for num in nums:
            seconds = seconds * 60 + num
        return int(seconds)
    if (match := _TS_UNIT_RE.match(token)) and any(match.groups()):
        hours, minutes, secs = (int(g) if g else 0 for g in match.groups())
        return hours * 3600 + minutes * 60 + secs
    return None


def _split_trim_spec(query: str) -> tuple[str, int | None, int | None]:
    """Strip a trailing ``@start-end`` trim spec off ``query``.

    Returns ``(query_without_spec, start, end)``. ``start``/``end`` are whole
    seconds or ``None`` when unbounded/absent. An unparseable or empty spec is
    left untouched on the returned query so it can't accidentally hide text.
    """
    if not isinstance(query, str) or "@" not in query:
        return query, None, None
    match = _YT_TRIM_RE.search(query)
    if not match:
        return query, None, None
    start = _parse_timestamp(match.group(1))
    end = _parse_timestamp(match.group(2))
    if start is None and end is None:
        # Not a recognizable trim window (e.g. "@-"); leave the suffix on the
        # query so a genuine text search isn't silently altered.
        return query, None, None
    trimmed = query[: match.start()].rstrip()
    if start is not None and end is not None and start >= end:
        # Nonsensical window (start >= end): ignore the bounds, but still strip
        # the recognized "@start-end" suffix so it can't corrupt URL resolution.
        return trimmed, None, None
    return trimmed, start, end


def _encode_track_id(video_id: str, start: int | None, end: int | None) -> str:
    """Encode an optional trim window into a persistent track item_id."""
    if start is None and end is None:
        return video_id
    start_str = str(start) if start is not None else ""
    end_str = str(end) if end is not None else ""
    return f"{video_id}@{start_str}-{end_str}"


def _split_track_id(item_id: str) -> tuple[str, int | None, int | None]:
    """Split an item_id into ``(video_id, start, end)``.

    The inverse of :func:`_encode_track_id`. Ids without an ``@`` suffix (the
    common case for normal search results) pass through unchanged.
    """
    if not isinstance(item_id, str) or "@" not in item_id:
        return item_id, None, None
    video_id, _, spec = item_id.partition("@")
    start_str, _, end_str = spec.partition("-")
    start = int(start_str) if start_str.isdigit() else None
    end = int(end_str) if end_str.isdigit() else None
    return video_id, start, end


def _format_trim_label(start: int | None, end: int | None) -> str:
    """Human-readable label for a trim window, e.g. ``"0:15–3:42"``."""

    def _fmt(secs: int) -> str:
        minutes, sec = divmod(secs, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{sec:02d}"
        return f"{minutes}:{sec:02d}"

    start_str = _fmt(start) if start is not None else ""
    end_str = _fmt(end) if end is not None else ""
    return f"{start_str}\u2013{end_str}"


def _strip_browse_prefix(playlist_id: str) -> str:
    """Drop ytmusicapi's "VL" browse prefix (e.g. "VLPLxxx" -> "PLxxx")."""
    return playlist_id[2:] if playlist_id.startswith("VL") else playlist_id


def _is_private_playlist_id(playlist_id: str) -> bool:
    """Is this a per-account playlist that only an authenticated call can read?

    See ``PRIVATE_PLAYLIST_IDS``. For these the unauthenticated yt-dlp
    fallback is guaranteed to fail, so callers skip it rather than log a
    misleading "playlist does not exist" for a playlist that plainly exists.
    """
    return _strip_browse_prefix(playlist_id) in PRIVATE_PLAYLIST_IDS


def _is_radio_playlist_id(playlist_id: str) -> bool:
    """Does this id carry the "RD" prefix, whatever kind of RD it turns out to be?

    True for song radio, personal mixes and editorial playlists alike. Use
    ``_is_watch_only_playlist_id`` to decide where to fetch tracks from; this
    one answers the narrower question of whether a watch URL is even a
    possibility, which is what seeding one depends on.
    """
    return _strip_browse_prefix(playlist_id).startswith(_YT_RADIO_PREFIX)


def _is_watch_only_playlist_id(playlist_id: str) -> bool:
    """Is the watch endpoint the only one that will hand back this id's tracks?

    Song radio and the personal mixes, yes. Editorial "RDCLAK5uy_..."
    playlists, no: they read normally through ``get_playlist``, and that is the
    route that returns all of their tracks rather than a queue's worth.
    """
    bare_id = _strip_browse_prefix(playlist_id)
    return bare_id.startswith(_YT_RADIO_PREFIX) and not bare_id.startswith(
        _YT_EDITORIAL_PREFIX
    )


def _radio_seed_video_id(playlist_id: str) -> str | None:
    """Return the video id a song-radio id is built from, when it carries one.

    Personal mixes (``RDTMAK5uy_...``) and editorial playlists
    (``RDCLAK5uy_...``) carry no seed and return None; a caller that needs one
    has to take it from the first track instead.
    """
    match = _YT_RADIO_SEED_RE.match(_strip_browse_prefix(playlist_id))
    return match.group(1) if match else None


def _normalize_watch_track(track_obj: dict[str, Any]) -> dict[str, Any]:
    """Reshape a watch/radio track into the shape ``_parse_track`` expects.

    ``get_watch_playlist`` spells duration as a clock string under ``length``
    and puts artwork under ``thumbnail`` (singular). ``_parse_track`` only
    understands a numeric ``duration``/``duration_seconds`` and a
    ``thumbnails`` list, so without this every mix track renders with no
    duration and no artwork.
    """
    normalized = dict(track_obj)
    if "duration" not in normalized and "duration_seconds" not in normalized:
        seconds = _parse_timestamp(str(normalized.get("length") or ""))
        if seconds:
            normalized["duration"] = seconds
    if not normalized.get("thumbnails") and (thumb := normalized.get("thumbnail")):
        normalized["thumbnails"] = thumb if isinstance(thumb, list) else [thumb]
    return normalized


def _rank_audio_format(fmt: dict[str, Any], prefer_quality: bool) -> tuple[float, float]:
    """Sort key for audio-only yt-dlp formats. Higher is better.

    Mirrors the yt-dlp selector used in ``_get_stream_format`` so the manual
    fallback and the selector cannot disagree about what "best" means:

    * quality mode ranks purely on bitrate, matching ``bestaudio``
    * compatibility mode puts AAC ahead of everything else and only then
      ranks on bitrate, matching ``bestaudio[ext=m4a]/bestaudio``

    Bitrate comes from ``abr`` and falls back to ``tbr``; for an audio-only
    format the two are equivalent, and a missing value sorts last rather than
    raising. Anything unparseable is treated as 0 for the same reason.
    """
    raw_bitrate = fmt.get("abr")
    if raw_bitrate is None:
        raw_bitrate = fmt.get("tbr")
    try:
        bitrate = float(raw_bitrate)
    except (TypeError, ValueError):
        bitrate = 0.0

    if prefer_quality:
        return (0.0, bitrate)

    # ``ext`` alone is not enough: a format can be AAC in an mp4 container, so
    # check the codec too. yt-dlp spells AAC as "mp4a.40.x".
    acodec = str(fmt.get("acodec") or "").lower()
    is_aac = fmt.get("ext") == "m4a" or acodec.startswith(("mp4a", "aac"))
    return (1.0 if is_aac else 0.0, bitrate)


def _parse_duration_words(value: Any) -> int | None:
    """Seconds from a spelled-out duration like "3 hr 46 min".

    Returns None when nothing numeric is found, so a caller can leave the
    duration unset rather than claiming a track is zero seconds long.

    Tracks report a clock string ("3:46") that ``_parse_timestamp`` reads;
    podcast episodes report words instead, and the two are not interchangeable:
    "3 hr 46 min" read as a clock is nonsense, and "3:46" read as words finds no
    unit at all. Falls through to ``_parse_timestamp`` so either spelling works
    whichever endpoint it came from.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    match = DURATION_WORDS_RE.match(text)
    if match and any(match.groupdict().values()):
        hours = int(match["hours"] or 0)
        minutes = int(match["minutes"] or 0)
        seconds = int(match["seconds"] or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total > 0:
            return total

    return _parse_timestamp(text)


def _episode_item_id(podcast_id: str, video_id: str) -> str:
    """Build the composite id an episode is addressed by."""
    return f"{podcast_id}{PODCAST_EPISODE_SPLITTER}{video_id}"


def _split_episode_id(item_id: str) -> tuple[str, str]:
    """Split "<podcastId>|<videoId>" into its parts.

    A bare id with no separator is treated as the video id with no known show,
    because that is what a hand-written or legacy id looks like and refusing it
    outright would be less useful than resolving what we can.
    """
    podcast_id, sep, video_id = str(item_id).partition(PODCAST_EPISODE_SPLITTER)
    if not sep:
        return "", podcast_id
    return podcast_id, video_id


def _strip_podcast_browse_prefix(browse_id: str) -> str:
    """Turn a search result's "MPSP<playlistId>" into the bare playlist id."""
    return str(browse_id or "").removeprefix(PODCAST_BROWSE_PREFIX)


def _description_text(value: Any) -> str | None:
    """Read a description that may arrive as a string or a Description object.

    ``get_podcast`` returns plain strings; ``get_episode`` returns ytmusicapi's
    ``Description``, which stringifies to a repr rather than its text.
    """
    if value is None:
        return None
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text or None
    if isinstance(value, str):
        return value or None
    return None


def _normalize_artist_name(value: str) -> str:
    """Fold an artist name to a form two spellings of it can both match."""
    return " ".join(str(value).split()).casefold()


def _auth_notice_reason(err: Exception) -> str:
    """Condense an auth error into one line that is safe inside a code span.

    The notice is rendered as Markdown, and a raw cookie error such as "Cookie
    must contain __Secure-3PAPISID" would lose its underscores to emphasis. The
    caller wraps this in backticks, so backticks inside it are swapped out.
    """
    text = " ".join(str(err).split()) or type(err).__name__
    # requests quotes the whole header value when it rejects one (a cookie
    # pasted with a leading space, say), and the notice is stored in plain text
    # and invites screenshots. Blank every name=value pair.
    text = _COOKIE_VALUE_RE.sub(r"\1<redacted>", text)
    text = text.replace("`", "'")
    if len(text) > AUTH_NOTICE_REASON_MAX:
        text = text[: AUTH_NOTICE_REASON_MAX - 3].rstrip() + "..."
    return text


def _search_tokens(value: str) -> set[str]:
    """Split text into comparable tokens for query matching.

    Punctuation is a separator rather than part of a token, so the dashes and
    dots a YouTube upload title is full of ("7 SECONDS. Live in Madrid
    (19-3-25)") do not stop its words from matching a plain typed query.
    """
    return {token for token in re.split(r"\W+", value.casefold()) if token}


def _query_match_score(query_tokens: set[str], track: Track) -> float:
    """How much of the query a track's title, version and artists cover, 0..1.

    Deliberately crude: it only measures how many of the words the user typed
    appear somewhere in the track, which is enough to tell a recording the
    query names outright from a fuzzy catalog match on one or two of its words.
    It is never used to rank results that YouTube Music already ordered against
    each other, only to decide whether a video beats the songs (issue #77).
    """
    if not query_tokens:
        return 0.0
    parts = [track.name, track.version, *(artist.name for artist in track.artists)]
    track_tokens = _search_tokens(" ".join(part for part in parts if part))
    return len(query_tokens & track_tokens) / len(query_tokens)


def _parse_blocklist(raw: str) -> tuple[frozenset[str], frozenset[str]]:
    """Parse a blocklist document into (channel ids, normalized names).

    Deliberately lenient about the format, because the whole point of the URL
    field is to point at a list somebody else maintains, and we do not get to
    dictate what they serve. Three shapes are understood:

    * a JSON array of strings
    * a JSON object with an ``artists``, ``channels`` or ``items`` array
    * plain text, one entry per line, ``#`` starting a comment

    Anything that parses as neither yields an empty pair rather than raising,
    so a list that changes shape under us degrades to "filter nothing" instead
    of breaking playback. Entries are classified by ``CHANNEL_ID_RE``: channel
    ids match exactly, everything else matches on a folded name.
    """
    entries: list[str] = []
    text = (raw or "").strip()
    if not text:
        return frozenset(), frozenset()

    # Tracked separately from `entries`: valid JSON in a shape we do not
    # recognise must yield nothing, not fall through to the line parser, which
    # would otherwise turn every line of the document into an "artist name".
    parsed_as_json = False
    if text[0] in "[{":
        with suppress(ValueError, TypeError, AttributeError):
            loaded = json.loads(text)
            parsed_as_json = True
            if isinstance(loaded, dict):
                for key in ("artists", "channels", "items"):
                    if isinstance(loaded.get(key), list):
                        loaded = loaded[key]
                        break
                else:
                    loaded = []
            if isinstance(loaded, list):
                for item in loaded:
                    if isinstance(item, str):
                        entries.append(item)
                    elif isinstance(item, dict):
                        # Objects are common in community lists; take whichever
                        # identifying field is present.
                        for key in ("channel_id", "channelId", "id", "name", "artist"):
                            if isinstance(item.get(key), str):
                                entries.append(item[key])
                                break

    if not parsed_as_json:
        if text.lstrip().startswith("<"):
            # An HTML error page served with a 200, which some hosts do instead
            # of a 404. Without this every line of it becomes a blocked artist.
            return frozenset(), frozenset()
        for line in text.splitlines():
            # Only a leading "#" is a comment, matching what the config field
            # and the README promise. Splitting on any "#" would truncate a
            # name that legitimately contains one.
            entry = line.strip()
            if entry and not entry.startswith("#"):
                entries.append(entry)

    channel_ids = {e.strip() for e in entries if CHANNEL_ID_RE.match(e.strip())}
    names = {
        _normalize_artist_name(e)
        for e in entries
        if e.strip() and not CHANNEL_ID_RE.match(e.strip())
    }
    return frozenset(channel_ids), frozenset(names - {""})


def _ytdlp_honours_preroll(version: str | None) -> bool:
    """Whether this yt-dlp's ``available_at`` actually tracks pre-roll ads.

    See ``MIN_YTDLP_VERSION_FOR_PREROLL``. An unreadable or unexpected version
    string returns True, because the field is only acted on when it is present
    *and* in the future: guessing "honour" costs a wait that a modern yt-dlp
    only asks for when there really is an ad, while guessing "ignore" would
    silently reinstate issue #51 for anyone whose version we failed to parse.
    """
    if not version:
        return True
    parts = version.split(".")[:3]
    try:
        parsed = tuple(int(part) for part in parts)
    except (TypeError, ValueError):
        return True
    if len(parsed) < 3:
        return True
    return parsed >= MIN_YTDLP_VERSION_FOR_PREROLL


def _ytdlp_can_stream(version: str | None) -> bool:
    """Whether this yt-dlp's default clients survive the android_vr shutdown.

    See ``MIN_YTDLP_VERSION_FOR_PLAYBACK``. Below the floor the defaults still
    include android_vr, whose URLs stop serving after ~1 MiB, so every track
    longer than a jingle dies mid-first-chunk. Only used to *warn*, never to
    refuse: an unreadable version returns True because a wrong warning every
    track is worse than a missing one, and the 403 itself still surfaces.
    """
    if not version:
        return True
    parts = version.split(".")[:3]
    try:
        parsed = tuple(int(part) for part in parts)
    except (TypeError, ValueError):
        return True
    if len(parsed) < 3:
        return True
    return parsed >= MIN_YTDLP_VERSION_FOR_PLAYBACK


def _preroll_wait_seconds(fmt: dict[str, Any], now: float | None = None) -> float:
    """Seconds to wait before ``fmt``'s URL can actually be fetched.

    When YouTube puts a pre-roll ad in front of a track it serves a media URL
    that is not valid yet, and answers a fetch before the ad window with a 403.
    yt-dlp models this as ``available_at``, a unix timestamp, and its own
    downloader blocks on it ("Sleeping N seconds as required by the site")
    before touching the URL.

    We do not download; we hand the URL to Music Assistant, which fetches it at
    once. So the wait has to happen here or the fetch 403s and the track is
    skipped as unplayable. That was issue #51: extraction reported success,
    playback failed, and nothing in between logged a reason.

    Mirrors yt-dlp's own ``max(f.get('available_at') or 0 for f in
    requested_formats)``: a merged format drops the field from the top level and
    keeps it only on its parts, so taking the max means the wait covers whichever
    part is gated latest. Absent, zero and unparseable all mean "no wait", so an
    older yt-dlp that predates the field degrades to today's behaviour rather
    than raising.

    Returned uncapped. Whether a given wait is worth serving belongs to the
    caller, which can tell "wait it out" apart from "too long to be real, fail
    now" and act differently; clamping here would collapse the two.

    yt-dlp also offers ``use_ad_playback_context``, which suppresses the wait at
    the source. It is not usable here: it only takes effect on the ``mweb`` and
    ``web_music`` player clients, and pinning a client is exactly what PR #44
    removed, for reasons that still hold.
    """
    now = time.time() if now is None else now
    parts = fmt.get("requested_formats") or [fmt]
    available_at = 0.0
    for part in parts:
        if not isinstance(part, dict):
            continue
        # OverflowError too: it is an ArithmeticError, not a ValueError, so an
        # oversized int would otherwise escape and break the never-raise
        # contract this docstring promises.
        with suppress(TypeError, ValueError, OverflowError):
            available_at = max(available_at, float(part.get("available_at") or 0))

    wait = available_at - now
    return wait if wait > 0 else 0.0


def _content_range_total(value: str | None) -> int | None:
    """Total byte size out of a ``Content-Range: bytes 0-1023/16492447`` header.

    None for an absent, malformed or ``*``-sized header, so the caller falls
    back to reading until a chunk comes back short instead of trusting a
    number that was never there.
    """
    if not value or "/" not in value:
        return None
    total = value.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    # Declare all features upfront — library methods return empty when not authenticated
    return YoutubeMusicFreeProvider(mass, manifest, config, BASE_FEATURES | AUTHENTICATED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider.

    Only Music Assistant 2.9.x calls this module-level hook. 2.10 resolves
    config entries through the ``get_config_entries`` instance method on the
    provider class instead, with no fallback to this function (issue #78).
    Both delegate to :func:`_build_config_entries` so the two servers see the
    same entries.
    """
    return _build_config_entries()


def _build_config_entries() -> tuple[ConfigEntry, ...]:
    """The provider's config entries, shared by the 2.9 and 2.10 hooks.

    Every entry is optional with a usable default, deliberately: that is what
    lets Music Assistant 2.10 create an instance without a ``setup_flow``
    module, applying defaults and letting the cookie be added later from the
    options page, same as on 2.9.
    """
    return (
        ConfigEntry(
            key=CONF_AUTH_TYPE,
            type=ConfigEntryType.STRING,
            label="Authentication",
            default_value=AUTH_TYPE_NONE,
            required=False,
            options=(
                ConfigValueOption(title="None (anonymous)", value=AUTH_TYPE_NONE),
                ConfigValueOption(title="Browser cookie", value=AUTH_TYPE_COOKIE),
            ),
            description="Optional: authenticate with a browser cookie to unlock "
            "library sync and recommendations. Leave as 'None' for anonymous access.",
        ),
        ConfigEntry(
            key=CONF_COOKIE,
            type=ConfigEntryType.SECURE_STRING,
            label="Cookie header",
            default_value="",
            required=False,
            depends_on=CONF_AUTH_TYPE,
            # A scalar, not a list: both the 2.9 frontend and the 2.10 server
            # compare depends_on_value with plain equality, so a list never
            # matches anything and the gate silently stays open (or, on 2.10,
            # shut). Exact-match single value is the documented contract.
            depends_on_value=AUTH_TYPE_COOKIE,
            description="Paste your YouTube Music cookie from browser DevTools. "
            "Open music.youtube.com → DevTools → Network → copy the 'Cookie' header "
            "from any request. Must contain __Secure-3PAPISID.",
        ),
        ConfigEntry(
            key=CONF_BRAND_ACCOUNT,
            type=ConfigEntryType.STRING,
            label="Brand account ID (optional)",
            default_value="",
            required=False,
            depends_on=CONF_AUTH_TYPE,
            depends_on_value=AUTH_TYPE_COOKIE,
            description="Leave empty for personal account. For brand accounts, "
            "find your ID at myaccount.google.com/brandaccounts or check the "
            "X-Goog-PageId header in browser DevTools on music.youtube.com.",
        ),
        ConfigEntry(
            key=CONF_AUTH_USER,
            type=ConfigEntryType.INTEGER,
            label="Account index (advanced)",
            default_value=0,
            required=False,
            depends_on=CONF_AUTH_TYPE,
            depends_on_value=AUTH_TYPE_COOKIE,
            description="Which signed-in Google account the cookie should resolve to, "
            "taken from the X-Goog-AuthUser request header on music.youtube.com. "
            "Leave at 0 unless you captured the cookie from a browser with several "
            "Google accounts signed in: those accounts all share one cookie, and this "
            "index is the only thing that tells them apart.",
        ),
        ConfigEntry(
            key=CONF_PREFER_AUDIO_QUALITY,
            type=ConfigEntryType.BOOLEAN,
            label="Prefer highest audio quality",
            default_value=True,
            required=False,
            description="When enabled, selects the highest-bitrate audio stream available, "
            "which is usually Opus at roughly 130 to 160 kbps. Disable only if a player in "
            "your setup cannot handle Opus: that restricts playback to AAC, normally around "
            "128 kbps, though some accounts and regions are offered nothing better than a "
            "48 kbps AAC stream. Leave enabled unless you have a specific reason not to.",
        ),
        ConfigEntry(
            key=CONF_FILTER_AI_MUSIC,
            type=ConfigEntryType.BOOLEAN,
            label="Filter AI-generated music",
            default_value=False,
            required=False,
            description="When enabled, tracks by artists on your blocklist are removed "
            "from radio, mixes, similar tracks and recommendations before they reach the "
            "queue. Search results, your library and playlists you chose yourself are "
            "never filtered, so looking something up still finds it. Off by default, and "
            "it does nothing until you give it a list below.",
        ),
        ConfigEntry(
            key=CONF_AI_BLOCKLIST,
            type=ConfigEntryType.STRING,
            label="Blocked artists",
            default_value="",
            required=False,
            depends_on=CONF_FILTER_AI_MUSIC,
            description="One entry per line, or separated by semicolons. An entry is "
            "either an artist name or a YouTube channel id (UC...). Semicolons rather "
            "than commas, because commas appear inside real names and splitting on them "
            "would turn 'Earth, Wind & Fire' into a rule blocking everyone called Earth. "
            "Names match loosely, ignoring case and extra spaces; channel ids match "
            "exactly and are the reliable choice when two artists share a name. Lines "
            "starting with # are ignored.",
        ),
        ConfigEntry(
            key=CONF_AI_BLOCKLIST_URL,
            type=ConfigEntryType.STRING,
            label="Blocklist URL (optional)",
            default_value="",
            required=False,
            depends_on=CONF_FILTER_AI_MUSIC,
            description="Optional: a URL serving a community-maintained list, merged with "
            "your own entries above. Accepts a JSON array of names or channel ids, a JSON "
            "object with an 'artists' key, or plain text one per line. Refreshed in the "
            "background about twice a day; if the URL is unreachable the last good list "
            "keeps working and nothing is filtered that was not already.",
        ),
    )


class YoutubeMusicFreeProvider(MusicProvider):
    """Provider for YouTube Music without premium subscription."""

    _ytmusic = None
    _yt_dlp_module = None
    _prefer_quality: bool = True
    # Set from the installed yt-dlp version the first time a stream is
    # resolved. Defaults to True so the pre-roll wait is on unless a version we
    # know to be wrong about it is detected: the guard exists to suppress one
    # bad release range, not to opt in to the fix. See issue #51.
    _preroll_supported: bool = True
    # Once-per-load latch for the stale-yt-dlp warning: the condition holds
    # for every single track until Music Assistant restarts, and one loud
    # warning reads better than one per queue item.
    _warned_stale_ytdlp: bool = False
    # AI-music filter (issue #53). Two sets rather than one, because channel
    # ids must match exactly while names have to survive spelling drift.
    _ai_filter_enabled: bool = False
    _ai_blocklist_url: str = ""
    _ai_blocked_channel_ids: frozenset[str] = frozenset()
    _ai_blocked_names: frozenset[str] = frozenset()
    # Entries typed into the config, kept apart from the fetched ones so a
    # refresh can replace the remote half without discarding the user's own.
    _ai_local_channel_ids: frozenset[str] = frozenset()
    _ai_local_names: frozenset[str] = frozenset()
    _ai_blocklist_fetched_at: float = 0.0
    # Last attempt, successful or not, so a dead URL backs off instead of
    # re-firing on every filtered call.
    _ai_blocklist_attempted_at: float = 0.0
    _ai_blocklist_refreshing: bool = False
    # Holds a reference to the in-flight background refresh so the event loop
    # does not garbage collect it mid-request.
    _ai_blocklist_task: object = None
    _authenticated: bool = False
    _auth_lapse_warned: bool = False
    # The auth notice this instance wants shown in the Music Assistant UI, and
    # the one it last wrote there. None means no notice. See _sync_auth_notice.
    _auth_notice: str | None = None
    _auth_notice_shown: str | None = None
    # Waits for Music Assistant to finish loading the instance before the first
    # write; held so it is not garbage collected and so unload can cancel it.
    _auth_notice_task: object = None
    # Per-category flag: True once we've seen a non-empty sync. Used to tell a
    # genuinely empty library apart from a partial-auth HTTP 200 response that
    # ytmusicapi unwraps to []. See issue #10.
    # Annotation only, deliberately. Giving this a `= {}` default here would
    # make one dict shared by every instance, so a populated account would make
    # a second, genuinely empty account raise a false partial-auth error.
    _library_seen_nonempty: dict[str, bool]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the config entries for this provider instance.

        Music Assistant 2.10 resolves the options page through this instance
        method; the module-level hook above exists for 2.9.x, which never
        calls this one (issue #78). 2.10 also calls it on every load, before
        ``handle_async_init``, so it must not depend on state assigned there —
        the entries are static, so that holds trivially.
        """
        return _build_config_entries()

    async def handle_async_init(self) -> None:
        """Set up the YTMusicFree provider."""
        # First line out of the provider on every start, and deliberately before
        # anything that can fail. Bug reports arrive as log excerpts, and until
        # this existed the only answer to "which build are you running?" was
        # "whatever main was on the day I installed". Logged rather than read
        # from the manifest because Music Assistant drops the key. Issue #68.
        self.logger.info("YouTube Music (Free) provider version %s", __version__)
        logging.getLogger("yt_dlp").setLevel(logging.WARNING)
        await self._install_packages()
        await self._purge_legacy_auth_file()
        self._library_seen_nonempty = {}
        # Explicit None check: a plain `or True` would swallow a configured
        # False and pin every instance to the high-quality selector.
        prefer_quality = self.config.get_value(CONF_PREFER_AUDIO_QUALITY)
        self._prefer_quality = True if prefer_quality is None else bool(prefer_quality)

        self._load_ai_filter_config()
        if self._ai_filter_enabled and self._ai_blocklist_url:
            # Non-fatal on purpose: an unreachable list must not stop the
            # provider loading. The local entries are already in effect.
            await self._refresh_remote_blocklist()

        auth_type = self.config.get_value(CONF_AUTH_TYPE) or AUTH_TYPE_NONE
        if auth_type == AUTH_TYPE_COOKIE:
            # Stripped: a leading space left over from deleting a copied
            # "Cookie:" prefix makes requests reject the header outright.
            cookie = (self.config.get_value(CONF_COOKIE) or "").strip()
            if cookie:
                try:
                    brand_account = self.config.get_value(CONF_BRAND_ACCOUNT) or None
                    auth_headers = self._build_auth_headers(
                        cookie, self._configured_auth_user()
                    )
                    self._ytmusic = await asyncio.to_thread(
                        self._create_ytmusic_client, auth=auth_headers, user=brand_account
                    )
                    # Validate auth by making a lightweight library call.
                    #
                    # "Did it raise" is not enough on its own. A lapsed YouTube
                    # session does not answer 401: it answers HTTP 200 with a
                    # logged-out payload, which ytmusicapi unwraps to []. So the
                    # call below succeeds, and before this check the provider
                    # logged "library sync enabled" over a cookie that was
                    # already dead. That reassuring line was the reporter's
                    # first clue in issue #55 that something was wrong with the
                    # logging rather than with their troubleshooting.
                    songs = await asyncio.to_thread(
                        self._ytmusic.get_library_songs, limit=1
                    )
                    if not songs and await asyncio.to_thread(
                        self._probe_session_alive
                    ) is False:
                        raise RuntimeError(
                            "the cookie was accepted but the account is not "
                            "signed in (YouTube answers a lapsed session with "
                            "an empty library rather than an auth error)"
                        )
                    self._authenticated = True
                    self._auth_lapse_warned = False
                    self.logger.info(
                        "YouTube Music (Free) initialized with cookie authentication — "
                        "library sync enabled"
                    )
                except Exception as err:
                    self.logger.warning(
                        "Cookie authentication failed (%s), falling back to anonymous mode. "
                        "You may need to refresh your cookie.",
                        err,
                    )
                    self._authenticated = False
                    # Shown once Music Assistant has finished loading us: it
                    # clears the error field after a successful load, so a
                    # write from here would be wiped. See loaded_in_mass.
                    self._auth_notice = AUTH_NOTICE_COOKIE_FAILED.format(
                        reason=_auth_notice_reason(err)
                    )
                    self._ytmusic = await asyncio.to_thread(self._create_ytmusic_client)
            else:
                # Cookie auth chosen and the field left empty. Every library
                # sync fails over it (_require_library_auth), so this used to
                # be the quietest way to end up without a library.
                self.logger.warning(
                    "Cookie authentication is selected but no cookie is set, "
                    "running in anonymous mode."
                )
                self._auth_notice = AUTH_NOTICE_COOKIE_MISSING
                self._ytmusic = await asyncio.to_thread(self._create_ytmusic_client)
        else:
            self._ytmusic = await asyncio.to_thread(self._create_ytmusic_client)

        if not self._authenticated:
            self.logger.info("YouTube Music (Free) initialized — anonymous mode")

    async def loaded_in_mass(self) -> None:
        """Arrange for the auth notice to be shown once loading has finished.

        Music Assistant clears the provider's error field after every successful
        load, so the notice cannot be written from ``handle_async_init``. Here is
        not safe either on 2.10.5, which runs this method before that clear.
        ``initialized`` is set after both, on 2.10.5 and on 2.11, so the write
        waits for it in a task of its own: waiting inline would deadlock,
        because the event is only set once this method has returned.
        """
        await super().loaded_in_mass()
        self._auth_notice_task = asyncio.get_running_loop().create_task(
            self._show_auth_notice_when_loaded()
        )

    async def unload(self, is_removed: bool = False) -> None:
        """Stop a pending auth notice from landing on an unloaded instance."""
        task = self._auth_notice_task
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
        await super().unload(is_removed)

    async def _show_auth_notice_when_loaded(self) -> None:
        """Write the auth notice as soon as Music Assistant has finished loading."""
        initialized = getattr(self, "initialized", None)
        if initialized is not None:
            await initialized.wait()
        self._sync_auth_notice()

    def _is_loaded(self) -> bool:
        """Whether Music Assistant has finished loading this instance."""
        initialized = getattr(self, "initialized", None)
        return initialized is not None and initialized.is_set()

    def _raise_auth_notice(self, err: Exception) -> None:
        """Show that a cookie which worked at startup has stopped working.

        Keeps the first notice rather than replacing it with every later error,
        so one lapse is one write. Before the instance has finished loading the
        pending task picks it up instead.
        """
        if self._auth_notice is not None:
            return
        self._auth_notice = AUTH_NOTICE_COOKIE_LAPSED.format(reason=_auth_notice_reason(err))
        if self._is_loaded():
            self._sync_auth_notice()

    def _clear_auth_notice(self) -> None:
        """Withdraw a lapse notice once the session has proved to be alive."""
        if self._auth_notice is None:
            return
        self._auth_notice = None
        if self._is_loaded():
            self._sync_auth_notice()

    def _sync_auth_notice(self) -> None:
        """Bring the provider's error in the Music Assistant UI in line with ours.

        The error field is the one channel Music Assistant has for a provider
        that is running but needs attention: it puts a badge on the provider
        list, a banner on the provider's settings page and a hint on the home
        page, and it survives restarts until the next successful load clears it.
        It also has no softer level than an error, which is why the notice text
        says plainly what still works.

        Coded as LoginFailed, which Music Assistant shows as "Authentication
        required". ``translation_key`` stays unset on purpose: with one, Music
        Assistant replaces the message with its generic "Login failed" text.

        Best effort throughout. Failing to show the notice must never break the
        provider it is about.
        """
        notice = self._auth_notice
        if notice == self._auth_notice_shown:
            return
        mass = self.mass
        update = getattr(getattr(mass, "config", None), "update_provider_last_error", None)
        if update is None or ProviderError is None:
            # Music Assistant 2.9.x, or no server at all in tests.
            return
        if not any(provider is self for provider in getattr(mass, "providers", ())):
            # Unloaded, or replaced by a reload whose own load just cleared the
            # field. Writing now would pin this instance's notice on that one.
            return
        try:
            update(
                self.instance_id,
                ProviderError(error_code=LoginFailed.error_code, message=notice)
                if notice
                else None,
            )
            # update_provider_last_error sends no event of its own, and on
            # 2.10.5 the load's own PROVIDERS_UPDATED has already gone out.
            mass.signal_event(EventType.PROVIDERS_UPDATED, data=mass.providers)
        except Exception as err:
            self.logger.debug("Could not update the auth notice in the UI: %s", err)
            return
        self._auth_notice_shown = notice

    def _create_ytmusic_client(
        self, auth: dict[str, str] | None = None, user: str | None = None
    ):
        """Create a YTMusic client, optionally with authentication."""
        ytmusicapi = importlib.import_module("ytmusicapi")
        if auth:
            return ytmusicapi.YTMusic(auth=auth, user=user)
        return ytmusicapi.YTMusic()

    @property
    def instance_name_postfix(self) -> str | None:
        """Return a per-instance label so two entries can be told apart.

        Never returns None or an empty string. Music Assistant's
        `default_name` computes a numeric fallback into a local variable but
        then formats `self.instance_name_postfix`, so a None here renders
        literally as "YouTube Music (Free) [None]" on every instance. The
        name is not only cosmetic: playlist owners fall back to it, so it
        would be written into library metadata.
        """
        brand_account = self.config.get_value(CONF_BRAND_ACCOUNT) if self.config else None
        if brand_account:
            return str(brand_account)
        if self.config and (auth_user := self._configured_auth_user()):
            return f"account {auth_user}"
        # Last resort, but always distinct: MA builds instance ids as
        # "<domain>--<uuid>", so the trailing fragment is unique per instance.
        return self.instance_id.rsplit("--", 1)[-1][:8]

    def _configured_auth_user(self) -> int:
        """Return the configured X-Goog-AuthUser index, defaulting to 0."""
        raw = self.config.get_value(CONF_AUTH_USER)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0
        return value if value >= 0 else 0

    async def _purge_legacy_auth_file(self) -> None:
        """Delete the pre-multi-instance auth file if an older version left one.

        Best effort: a missing file is the normal case, and a read-only or
        absent /data must never keep the provider from starting.
        """

        def _remove() -> bool:
            if not os.path.exists(LEGACY_AUTH_FILE):
                return False
            os.remove(LEGACY_AUTH_FILE)
            return True

        try:
            removed = await asyncio.to_thread(_remove)
        except OSError as err:
            self.logger.debug("Could not remove legacy auth file %s: %s", LEGACY_AUTH_FILE, err)
            return
        if removed:
            self.logger.info(
                "Removed the legacy auth file %s. Credentials are now held in "
                "memory per provider instance and no longer written to disk.",
                LEGACY_AUTH_FILE,
            )

    def _build_auth_headers(self, cookie: str, auth_user: int = 0) -> dict[str, str]:
        """Build the browser auth headers for this instance, in memory.

        ytmusicapi takes a headers dict directly, so nothing is written to
        disk. That is what makes several instances safe to run side by side:
        a shared file would let two accounts overwrite each other's cookie,
        and whichever instance was mid-construction could authenticate as the
        wrong account. See issue #40.

        ``auth_user`` is the X-Goog-AuthUser index. It matters because a
        browser signed in to several Google accounts sends one identical
        cookie for all of them, so the cookie alone cannot say which account
        is meant. Config is read by the caller, keeping this method pure.
        """
        import hashlib

        if "__Secure-3PAPISID" not in cookie:
            raise ValueError("Cookie must contain __Secure-3PAPISID")
        cookie_names = {
            part.strip().split("=", 1)[0]
            for part in cookie.split(";")
            if "=" in part
        }
        missing = [c for c in RECOMMENDED_AUTH_COOKIES if c not in cookie_names]
        if missing:
            self.logger.warning(
                "Cookie is missing recommended values: %s. The provider may "
                "validate at init and then fail library calls a few minutes later. "
                "Recapture the Cookie header from a `youtubei/v1/...` request on "
                "music.youtube.com (see issue #6 for the full set to include).",
                ", ".join(missing),
            )
        # Extract SAPISID from cookie
        sapisid = None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("SAPISID="):
                sapisid = part.split("=", 1)[1]
                break
            if part.startswith("__Secure-3PAPISID="):
                sapisid = part.split("=", 1)[1]
                break
        if not sapisid:
            raise ValueError("Could not extract SAPISID from cookie")
        # Compute SAPISIDHASH — ytmusicapi needs this in the Authorization header
        # to detect auth type as BROWSER (see determine_auth_type in auth_parse.py).
        # ytmusicapi recomputes fresh hashes per-request, so this is only for detection.
        timestamp = str(int(time.time()))
        hash_input = f"{timestamp} {sapisid} {YTM_DOMAIN}"
        sapisid_hash = hashlib.sha1(hash_input.encode()).hexdigest()
        # Three of these keys are load-bearing for ytmusicapi and must stay:
        # "authorization" (containing SAPISIDHASH) is the only thing
        # determine_auth_type inspects to classify the session as BROWSER,
        # "cookie" must carry __Secure-3PAPISID, and one of "origin" /
        # "x-origin" is read once at construction. A fresh dict per call, so
        # instances never share a mutable headers object.
        return {
            "cookie": cookie,
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.5",
            "content-type": "application/json",
            "x-goog-authuser": str(auth_user),
            "x-origin": YTM_DOMAIN,
            "origin": YTM_DOMAIN,
            "authorization": f"SAPISIDHASH {timestamp}_{sapisid_hash}",
        }

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on YouTube Music."""
        # If the query is a YouTube/YTM URL, resolve it to the exact item rather
        # than doing a plain text search. For a track we also run a normal text
        # search using the resolved video's title, so the other results are
        # related songs/albums/artists by name — not garbage matches on the raw
        # URL string. An explicit link bypasses the media_types filter for the
        # resolved item itself: a deliberate paste should always resolve.
        if url_match := self._parse_youtube_url(search_query):
            kind, item_id = url_match
            return await self._search_by_url(kind, item_id, media_types, limit)

        return await self._text_search(search_query, media_types, limit)

    async def _text_search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Run a plain YTM text search and parse it into ``SearchResults``."""
        parsed_results = SearchResults()

        async def _search_type(ytm_filter: str | None) -> list[dict]:
            return await asyncio.to_thread(
                self._ytmusic.search,
                query=search_query,
                filter=ytm_filter,
                limit=limit,
            )

        # YTM has no multi-type search, and an unfiltered search skews heavily
        # to songs and videos so artists and playlists rarely surface
        # (issue #18). Run one filtered call per requested type and merge.
        filters = [
            SEARCH_FILTER_BY_TYPE[mt] for mt in media_types if mt in SEARCH_FILTER_BY_TYPE
        ]
        if not filters:
            return parsed_results
        # A track search gets a second pass over the "videos" tab, appended
        # last so catalog songs come back first and videos rank below them in
        # the merged results (issue #77).
        if MediaType.TRACK in media_types:
            filters.append(SEARCH_FILTER_VIDEOS)

        results: list[tuple[str, dict]] = []
        for ytm_filter in filters:
            # Keep categories independent: one failing filter must not sink
            # the others.
            try:
                results.extend(
                    (ytm_filter, item) for item in await _search_type(ytm_filter)
                )
            except Exception as err:  # noqa: BLE001
                self.logger.debug("search filter %s failed: %s", ytm_filter, err)

        # Tracks are collected per pass instead of straight into the result.
        # Music Assistant truncates the merged track list to `limit` before it
        # re-ranks, and ytmusicapi treats `limit` as a floor, so the songs tab
        # alone already fills that window; videos appended unbounded behind it
        # would be cut off before anyone saw them, quietly re-losing the
        # content issue #77 is about. Composing under `limit` here, with a
        # slice reserved for videos, is the only place that can be done.
        song_tracks: list[Track] = []
        video_tracks: list[Track] = []
        # YTM keeps a song and its music video as distinct ids, so this set
        # should never fire across the songs/videos passes; it guards against
        # the backend handing out a counterpart id twice, which it has done
        # elsewhere (ytmusicapi #739).
        seen_track_ids: set[str] = set()
        for ytm_filter, result in results:
            try:
                result_type = result.get("resultType")
                if result_type == "artist" and MediaType.ARTIST in media_types:
                    parsed_results.artists.append(self._parse_artist(result))
                elif result_type == "album" and MediaType.ALBUM in media_types:
                    parsed_results.albums.append(self._parse_album(result))
                elif result_type == "playlist" and MediaType.PLAYLIST in media_types:
                    parsed_results.playlists.append(self._parse_playlist(result))
                elif result_type == "podcast" and MediaType.PODCAST in media_types:
                    # A show arrives as "MPSP<playlistId>" here and as the bare
                    # playlist id everywhere else; _parse_podcast strips it so
                    # one id shape reaches Music Assistant.
                    parsed_results.podcasts.append(self._parse_podcast(result))
                elif (
                    result_type in ("song", "video")
                    and MediaType.TRACK in media_types
                    # The videos filter forces resultType "video" even on the
                    # podcast episodes it returns; those belong to the podcast
                    # domain, not in track results.
                    and result.get("videoType") != VIDEO_TYPE_PODCAST_EPISODE
                    and (track := self._parse_track(result))
                    and track.item_id not in seen_track_ids
                ):
                    seen_track_ids.add(track.item_id)
                    if ytm_filter == SEARCH_FILTER_VIDEOS:
                        video_tracks.append(track)
                    else:
                        song_tracks.append(track)
            except (InvalidDataError, KeyError, TypeError):
                pass  # skip invalid items

        # Catalog songs first, videos below them, together at most `limit`.
        # The reservation shrinks to what the videos pass actually returned,
        # so songs only give up window space when there is a video to show.
        reserve = min(len(video_tracks), max(1, limit // 4))
        songs_shown = song_tracks[: max(0, limit - reserve)]
        promoted, weaker = self._split_promoted_videos(search_query, song_tracks, video_tracks)
        # Promoted videos go in front of the songs, but must not evict them:
        # only the slots the songs were not going to use are theirs. Anything
        # over that stays ahead of the weaker videos and behind the songs,
        # which is where it would have ranked before.
        front_slots = max(0, limit - len(songs_shown))
        parsed_results.tracks = (
            promoted[:front_slots] + songs_shown + promoted[front_slots:] + weaker
        )[:limit]

        await self._attach_album_years(parsed_results.tracks)
        return parsed_results

    @staticmethod
    def _split_promoted_videos(
        search_query: str,
        song_tracks: list[Track],
        video_tracks: list[Track],
    ) -> tuple[list[Track], list[Track]]:
        """Split the videos pass into the ones that outrank the songs and the rest.

        Videos belong below catalog songs: for most queries the catalog holds
        the recording the user meant, and the videos tab offers a live cover of
        it. The exception is a query the catalog simply does not have, where the
        video is the answer and ranking it last hides it (issue #77) — Music
        Assistant slices the merged per-provider results to one shared window,
        so with a second provider enabled only the head of this list is seen.

        A video is promoted when it covers the query at least
        ``VIDEO_PROMOTION_MARGIN`` better than *every* song did, which is the
        strict reading of "clearly better" on purpose: a catalog song that
        matches the query as well as the video keeps its place in front.

        Order within each group is left as YouTube Music returned it, and no
        split is made when there are no songs to outrank, so a crude token
        score never gets to re-order results that YTM already ranked.
        """
        if not song_tracks or not video_tracks:
            return [], video_tracks
        query_tokens = _search_tokens(search_query)
        if not query_tokens:
            return [], video_tracks
        threshold = (
            max(_query_match_score(query_tokens, song) for song in song_tracks)
            + VIDEO_PROMOTION_MARGIN
        )
        promoted: list[Track] = []
        weaker: list[Track] = []
        for video in video_tracks:
            target = promoted if _query_match_score(query_tokens, video) >= threshold else weaker
            target.append(video)
        return promoted, weaker

    @classmethod
    def _parse_youtube_url(cls, query: str) -> tuple[str, str] | None:
        """Resolve a YouTube/YTM URL to ``(kind, id)`` or ``None``.

        ``kind`` is ``"track"`` (for a ``videoId``) or ``"playlist"`` (for a
        ``list`` id). Returns ``None`` when ``query`` is not a recognized URL,
        so the caller can fall through to a normal text search.

        Disambiguation: a watch URL that also carries a ``list`` param (a video
        opened inside a playlist) resolves to the track — pasting a song link
        should add the song, not the surrounding playlist.

        Tries a strict ``urlparse`` first (handles well-formed URLs and bare
        host-prefixed strings), then a mangle-tolerant regex fallback for the
        form MA's search controller produces after stripping "/" and "'".

        A trailing ``@start-end`` trim spec (see :func:`_split_trim_spec`) is
        peeled off first and, for a resolved track, encoded into the returned id
        as ``VIDEOID@start-end`` so the trim persists through playback and into
        the library. The spec is ignored for playlists.
        """
        if not isinstance(query, str):
            return None
        candidate, start, end = _split_trim_spec(query.strip())
        candidate = candidate.strip()
        if not candidate:
            return None
        resolved = cls._parse_youtube_url_strict(candidate)
        if resolved is None:
            resolved = cls._parse_youtube_url_mangled(candidate)
        if resolved is None:
            return None
        kind, item_id = resolved
        if kind == "track" and (start is not None or end is not None):
            item_id = _encode_track_id(item_id, start, end)
        return (kind, item_id)

    @staticmethod
    def _parse_youtube_url_strict(candidate: str) -> tuple[str, str] | None:
        """Parse a well-formed YouTube URL via ``urlparse``."""
        # Be lenient about a missing scheme (e.g. "youtu.be/ID" pasted bare).
        if "://" not in candidate:
            if candidate.lower().startswith(("youtu.be/", "www.", "music.", "m.", "youtube.com/")):
                candidate = f"https://{candidate}"
            else:
                return None

        parsed = urlparse(candidate)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        youtube_hosts = {
            "youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
        }
        if host not in youtube_hosts:
            return None

        # Short youtu.be links carry the video id in the path.
        if host == "youtu.be":
            video_id = parsed.path.lstrip("/").split("/")[0]
            return ("track", unquote(video_id)) if video_id else None

        query_params = parse_qs(parsed.query)
        path = parsed.path.rstrip("/")

        if video_ids := query_params.get("v"):
            if video_ids[0]:
                return ("track", video_ids[0])

        if path.endswith("/playlist") or path == "/playlist":
            if list_ids := query_params.get("list"):
                if list_ids[0]:
                    return ("playlist", list_ids[0])

        # A bare ?list= on a watch/other path with no ?v= still means a playlist.
        if list_ids := query_params.get("list"):
            if list_ids[0]:
                return ("playlist", list_ids[0])

        return None

    @staticmethod
    def _parse_youtube_url_mangled(candidate: str) -> tuple[str, str] | None:
        """Recover a YouTube id from MA's sanitized (de-slashed) query string.

        MA replaces "/" with " " and strips "'" before calling the provider, so
        ``https://www.youtube.com/watch?v=ID`` arrives as
        ``https:  www.youtube.com watch?v=ID``. Require a recognizable youtube
        host token plus a valid id so ordinary text searches aren't hijacked.
        """
        is_short = _YT_SHORT_TOKEN.search(candidate)
        if not is_short and not _YT_HOST_TOKEN.search(candidate):
            return None

        if is_short:
            return ("track", is_short.group(1))

        # A ?v= id always means the track, even when a list= is also present.
        if video_match := _YT_VIDEO_ID.search(candidate):
            return ("track", video_match.group(1))

        if list_match := _YT_LIST_ID.search(candidate):
            return ("playlist", list_match.group(1))

        return None

    async def _search_by_url(
        self,
        kind: str,
        item_id: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Resolve a pasted URL id into ``SearchResults``.

        For a playlist this is just the single resolved playlist. For a track,
        the resolved video is placed first, then a normal text search on the
        video's title fills in related tracks/albums/artists/playlists (deduped
        against the raw video). Failures degrade to whatever resolved so far
        rather than raising, mirroring the per-item resilience of text search.
        """
        results = SearchResults()
        if kind == "playlist":
            try:
                results.playlists.append(await self.get_playlist(item_id))
            except Exception as err:  # noqa: BLE001
                self.logger.debug("search by url failed for playlist %s: %s", item_id, err)
            return results

        # kind == "track"
        raw_track = None
        try:
            raw_track = await self.get_track(item_id)
        except Exception as err:  # noqa: BLE001
            self.logger.debug("search by url failed for track %s: %s", item_id, err)

        # The explicitly pasted video always resolves, even if TRACK isn't in the
        # requested media_types — a deliberate paste should always return it.
        if raw_track is not None:
            results.tracks.append(raw_track)

        # Run a name search using the resolved title so the rest of the results
        # are related items, not matches on the raw URL string. Skip it when the
        # title couldn't be resolved (falls back to the bare id) to avoid noise.
        video_id, _, _ = _split_track_id(item_id)
        title = raw_track.name if raw_track is not None else None
        if title and title not in (video_id, item_id):
            try:
                named = await self._text_search(title, media_types, limit)
            except Exception as err:  # noqa: BLE001
                self.logger.debug("name search failed for %r: %s", title, err)
            else:
                results.artists.extend(named.artists)
                results.albums.extend(named.albums)
                results.playlists.extend(named.playlists)
                # Don't list the pasted video twice.
                results.tracks.extend(
                    t for t in named.tracks if _split_track_id(t.item_id)[0] != video_id
                )
        return results

    @use_cache(CATALOG_CACHE_TTL, allow_expired_cache=True, cache_checksum=TRACK_CACHE_VERSION)
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id.

        ``prov_track_id`` may carry a trim window (``VIDEOID@start-end``). The
        YTM API is queried with the bare video id, but the returned Track keeps
        the encoded id so the trim persists, and its duration reflects the
        trimmed window.
        """
        video_id, start, end = _split_track_id(prov_track_id)
        try:
            track_obj = await asyncio.to_thread(self._ytmusic.get_song, video_id)
            video_details = track_obj.get("videoDetails", {}) if track_obj else {}
            if video_details:
                normalized = {
                    "videoId": video_details.get("videoId", video_id),
                    "title": video_details.get("title", video_id),
                    "duration_seconds": video_details.get("lengthSeconds"),
                    "artists": [{"name": video_details.get("author", "Unknown"), "id": None}],
                    "thumbnails": video_details.get("thumbnail", {}).get("thumbnails", []),
                    "isAvailable": True,
                }
                track = self._parse_track(normalized)
                return self._apply_trim(track, prov_track_id, start, end)
        except Exception as e:
            self.logger.debug("get_song failed for %s: %s", video_id, e)
        return self._minimal_track(prov_track_id)

    def _apply_trim(
        self, track: Track, encoded_id: str, start: int | None, end: int | None
    ) -> Track:
        """Re-key a parsed track to its encoded (trimmed) id and fix duration.

        ``_parse_track`` keys the track off the bare video id; when a trim window
        is present we swap in the encoded id (on both the track and its provider
        mapping) and adjust the reported duration to the trimmed length so the
        UI shows the right time and progress bar.
        """
        if start is None and end is None:
            return track
        full_duration = track.duration or 0
        upper = end if end is not None else full_duration
        lower = start or 0
        if upper and upper > lower:
            track.duration = upper - lower
        track.item_id = encoded_id
        for mapping in track.provider_mappings:
            mapping.item_id = encoded_id
        # Flag the trim in the version so trimmed entries are distinguishable in
        # the library/playlists without changing the song name itself.
        trim_label = _format_trim_label(start, end)
        track.version = f"{track.version} [{trim_label}]".strip() if track.version else f"[{trim_label}]"
        return track

    @use_cache(CATALOG_CACHE_TTL, allow_expired_cache=True)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        album_obj = await asyncio.to_thread(self._ytmusic.get_album, prov_album_id)
        if not album_obj:
            raise MediaNotFoundError(f"Album {prov_album_id} not found")
        return self._parse_album(album_obj, prov_album_id)

    @use_cache(CATALOG_CACHE_TTL, allow_expired_cache=True, cache_checksum=TRACK_CACHE_VERSION)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        album_obj = await asyncio.to_thread(self._ytmusic.get_album, prov_album_id)
        if not album_obj or not album_obj.get("tracks"):
            return []
        album = self._parse_album(album_obj, prov_album_id)
        tracks = []
        for track_number, track_obj in enumerate(album_obj["tracks"], 1):
            with suppress(InvalidDataError, KeyError, TypeError):
                track = self._parse_track(track_obj, track_number=track_number)
                # Free here: the album this track came from is already in hand,
                # year and all, so there is nothing to look up.
                track.album = self._get_item_mapping(
                    MediaType.ALBUM, album.item_id, album.name, album.year
                )
                tracks.append(track)
        return tracks

    @staticmethod
    def _looks_like_channel_id(prov_artist_id: str) -> bool:
        """Whether an id has the YTM channel-id shape (``UC...``).

        Artist links embedded in track metadata sometimes carry an id that is
        not a real channel id. Passing one to YTM's ``get_artist`` (a browse
        call) returns HTTP 400 "invalid argument", which previously surfaced
        raw to the user (issue #18). Gating on the shape avoids the doomed call.
        """
        return isinstance(prov_artist_id, str) and prov_artist_id.startswith("UC")

    async def _fetch_artist_obj(self, prov_artist_id: str) -> dict | None:
        """Fetch the raw YTM artist object, or None if the id can't resolve.

        Returns None for non-channel ids and for YTM errors (e.g. the HTTP 400
        a malformed id triggers), so callers degrade to an empty result instead
        of raising a raw HTTP error.
        """
        if not self._looks_like_channel_id(prov_artist_id):
            return None
        try:
            return await asyncio.to_thread(self._ytmusic.get_artist, prov_artist_id)
        except Exception as err:  # noqa: BLE001
            self.logger.debug("get_artist failed for %s: %s", prov_artist_id, err)
            return None

    @use_cache(ARTIST_CACHE_TTL, allow_expired_cache=True)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        # Fake IDs created when artist channel ID is unknown — return a stub.
        # "unknown_<name>" carries a display name; bare "unknown" is the
        # artist-less placeholder from _minimal_track and the videos-tab search
        # fallback (issue #77). Music Assistant resolves a track's artist
        # mappings through this method when the artist page opens and when a
        # track is added to the library, and it treats MediaNotFoundError as
        # "drop the artist" — which errors the artist page and, for a track
        # whose only artist is the placeholder, fails the library add with
        # "Track is missing artist(s)". So the placeholder must resolve.
        if prov_artist_id == "unknown" or prov_artist_id.startswith("unknown_"):
            name = prov_artist_id[8:] or "Unknown Artist"
            return Artist(
                item_id=prov_artist_id,
                name=name,
                provider=self.instance_id,
                provider_mappings={
                    ProviderMapping(
                        item_id=prov_artist_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
        # A non-channel id can only yield a 400 from YTM; treat it as not found
        # rather than handing YTM an argument it will reject (issue #18).
        if not self._looks_like_channel_id(prov_artist_id):
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
        try:
            artist_obj = await asyncio.to_thread(self._ytmusic.get_artist, prov_artist_id)
            if not artist_obj:
                raise MediaNotFoundError(f"Artist {prov_artist_id} not found")
            artist_obj.setdefault("channelId", prov_artist_id)
            return self._parse_artist(artist_obj)
        except MediaNotFoundError:
            raise
        except Exception as e:
            raise MediaNotFoundError(f"Artist {prov_artist_id} not found") from e

    @use_cache(ARTIST_CACHE_TTL, allow_expired_cache=True)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        artist_obj = await self._fetch_artist_obj(prov_artist_id)
        if not artist_obj:
            return []
        albums = []
        for album_obj in artist_obj.get("albums", {}).get("results", []):
            with suppress(InvalidDataError, KeyError, TypeError):
                if "artists" not in album_obj:
                    album_obj["artists"] = [
                        {"id": artist_obj.get("channelId"), "name": artist_obj.get("name")}
                    ]
                albums.append(self._parse_album(album_obj, album_obj.get("browseId")))
        return albums

    @use_cache(ARTIST_CACHE_TTL, allow_expired_cache=True, cache_checksum=TRACK_CACHE_VERSION)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a list of most popular tracks for the given artist."""
        artist_obj = await self._fetch_artist_obj(prov_artist_id)
        if not artist_obj:
            return []
        songs = artist_obj.get("songs", {})
        if songs.get("browseId"):
            playlist_tracks = await self.get_playlist_tracks(songs["browseId"])
            return playlist_tracks[:25]
        return []

    # ------------------------------------------------------------------
    # Podcasts (issue #52)
    #
    # Works without an account: search, show detail and episode detail all
    # answer anonymously, and an episode is an ordinary YouTube video id, so the
    # existing stream path carries the audio with no new code.
    #
    # Library sync is deliberately absent. get_library_podcasts and
    # get_saved_episodes need auth, and declaring LIBRARY_PODCASTS without them
    # would have Music Assistant sync a library we cannot read, which is exactly
    # how issue #55 emptied people's libraries.
    # ------------------------------------------------------------------

    async def _fetch_podcast_obj(self, prov_podcast_id: str) -> dict[str, Any]:
        """Fetch a show, accepting either the bare or MPSP-prefixed id."""
        podcast_id = _strip_podcast_browse_prefix(prov_podcast_id)
        try:
            podcast_obj = await asyncio.to_thread(
                self._ytmusic.get_podcast, podcast_id, limit=PODCAST_EPISODE_LIMIT
            )
        except Exception as err:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found") from err
        if not podcast_obj:
            raise MediaNotFoundError(f"Podcast {prov_podcast_id} not found")
        return podcast_obj

    @use_cache(PODCAST_CACHE_TTL, allow_expired_cache=True)
    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id."""
        podcast_id = _strip_podcast_browse_prefix(prov_podcast_id)
        podcast_obj = await self._fetch_podcast_obj(podcast_id)
        return self._parse_podcast(podcast_obj, podcast_id)

    async def get_podcast_episodes(
        self, prov_podcast_id: str
    ) -> AsyncGenerator[PodcastEpisode, None]:
        """Get the episodes of a podcast."""
        podcast_id = _strip_podcast_browse_prefix(prov_podcast_id)
        podcast_obj = await self._fetch_podcast_obj(podcast_id)
        podcast = self._parse_podcast(podcast_obj, podcast_id)
        for index, episode_obj in enumerate(podcast_obj.get("episodes") or [], start=1):
            with suppress(InvalidDataError, KeyError, TypeError):
                # "index" is None on every anonymous response measured, so the
                # enumeration order is the only ordering available. Episodes
                # come back newest first, which is the order a listener expects.
                position = episode_obj.get("index") or index
                yield self._parse_podcast_episode(episode_obj, podcast, position)

    @use_cache(PODCAST_CACHE_TTL, allow_expired_cache=True)
    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get a single podcast episode by its composite id."""
        podcast_id, video_id = _split_episode_id(prov_episode_id)
        if not video_id:
            raise MediaNotFoundError(f"Episode {prov_episode_id} not found")
        try:
            episode_obj = await asyncio.to_thread(self._ytmusic.get_episode, video_id)
        except Exception as err:
            raise MediaNotFoundError(f"Episode {prov_episode_id} not found") from err
        if not episode_obj:
            raise MediaNotFoundError(f"Episode {prov_episode_id} not found")
        # get_episode does not echo the video id back, and PodcastEpisode needs
        # it to build its own item_id, so put it back from what we were asked.
        episode_obj.setdefault("videoId", video_id)

        # A PodcastEpisode must carry a Podcast. The id we were given holds the
        # show, and the response names it too, so fall back to the response when
        # a caller hands us a bare video id.
        if not podcast_id:
            author = episode_obj.get("author")
            if isinstance(author, dict):
                podcast_id = _strip_podcast_browse_prefix(author.get("id") or "")
            podcast_id = podcast_id or _strip_podcast_browse_prefix(
                episode_obj.get("playlistId") or ""
            )
        podcast = await self._podcast_for_episode(podcast_id, episode_obj)
        return self._parse_podcast_episode(episode_obj, podcast)

    async def _podcast_for_episode(self, podcast_id: str, episode_obj: dict) -> Podcast:
        """Resolve the show an episode belongs to, degrading to a stub.

        A failed show lookup must not make the episode unplayable, so fall back
        to a minimal Podcast built from what the episode response already names.
        """
        if podcast_id:
            try:
                return await self.get_podcast(podcast_id)
            except Exception as err:  # noqa: BLE001 - a stub podcast still plays
                self.logger.debug(
                    "could not resolve podcast %s for episode: %s", podcast_id, err
                )
        author = episode_obj.get("author")
        name = author.get("name") if isinstance(author, dict) else None
        return self._parse_podcast(
            {"title": name or "Unknown Podcast"},
            podcast_id or episode_obj.get("playlistId") or "unknown",
        )

    @use_cache(PLAYLIST_TRACKS_CACHE_TTL, allow_expired_cache=True)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        try:
            playlist_obj = await asyncio.to_thread(
                self._ytmusic.get_playlist, prov_playlist_id, limit=1
            )
            if not playlist_obj:
                raise MediaNotFoundError(f"Playlist {prov_playlist_id} not found")
            return self._parse_playlist(playlist_obj)
        except MediaNotFoundError:
            raise
        except Exception as err:  # noqa: BLE001 - degrade to the yt-dlp fallback
            if _is_private_playlist_id(prov_playlist_id):
                # No fallback exists for a private playlist, so this is the
                # only place the real error can surface. At debug level all a
                # user ever saw was yt-dlp's misleading "playlist does not
                # exist" (issue #82).
                self.logger.warning(
                    "could not read private playlist %s with the authenticated "
                    "session (%s: %s)",
                    prov_playlist_id,
                    type(err).__name__,
                    err,
                )
                raise MediaNotFoundError(
                    f"Playlist {prov_playlist_id} not found"
                ) from err
            # ytmusicapi requires auth for some playlist types, and raises a
            # KeyError outright on song radio, so fall back to yt-dlp.
            self.logger.debug(
                "ytmusicapi get_playlist failed for %s (%s: %s), using yt-dlp fallback",
                prov_playlist_id,
                type(err).__name__,
                err,
            )
            # Without a seed this builds the playlist?list=RD... URL YouTube
            # refuses, so a personal mix resolved to "not found" here even after
            # its tracks became reachable. Song radio survived only because its
            # seed is embedded in the id. Issue #47 follow-up.
            seed = await self._resolve_radio_seed(prov_playlist_id)
            return await self._get_playlist_via_ytdlp(prov_playlist_id, seed)

    async def _resolve_radio_seed(self, playlist_id: str) -> str | None:
        """Return a video id able to seed the watch URL for a radio id.

        Anything without the "RD" prefix returns None without a request. Song
        radio carries its seed in the id itself ("RD<videoId>"), so that costs
        nothing either. Everything else RD-prefixed carries no seed, and a watch
        URL cannot be built without one, so the seed has to come from the first
        track: one extra call to the endpoint that is willing to answer for
        these ids at all.

        Returns None on three paths besides the non-radio one: the request
        failed, the response held no track with a video id, or the response was
        not shaped as expected. Each is logged; the caller degrades to the plain
        URL form.
        """
        if not _is_radio_playlist_id(playlist_id):
            return None
        if seed := _radio_seed_video_id(playlist_id):
            return seed
        try:
            watch_playlist = await asyncio.to_thread(
                self._ytmusic.get_watch_playlist,
                playlistId=_strip_browse_prefix(playlist_id),
                # One track is all a seed needs; anything more is wasted work.
                limit=1,
            )
            # Inside the try on purpose. This runs from inside get_playlist's
            # own except block, so anything raised here would replace the error
            # being handled and skip the fallback entirely.
            for track_obj in (watch_playlist or {}).get("tracks") or []:
                if isinstance(track_obj, dict) and (video_id := track_obj.get("videoId")):
                    return video_id
        except Exception as err:  # noqa: BLE001 - no seed, caller degrades
            self.logger.warning(
                "could not read a seed track for mix %s: %s", playlist_id, err
            )
            return None
        self.logger.warning(
            "mix %s returned no seed track, so its details cannot be fetched",
            playlist_id,
        )
        return None

    async def _get_radio_playlist_tracks(self, playlist_id: str) -> list[Track]:
        """Return the tracks of a personal mix or song radio.

        The watch/radio endpoint is what YouTube Music itself uses for these,
        and for song radio it is the only thing that answers at all:
        ``get_playlist`` raises a KeyError on the response shape and the yt-dlp
        fallback behind it cannot open them either, so before this existed they
        resolved to zero tracks and playback had nothing to start (issue #47).

        Not for editorial ``RDCLAK5uy_`` playlists. This endpoint will answer
        for those too, but only with a queue's worth, so they keep the ordinary
        path and its full track list. See ``_is_watch_only_playlist_id``.
        """
        bare_id = _strip_browse_prefix(playlist_id)
        try:
            watch_playlist = await asyncio.to_thread(
                self._ytmusic.get_watch_playlist,
                playlistId=bare_id,
                limit=RADIO_PLAYLIST_LIMIT,
            )
        except Exception as err:  # noqa: BLE001 - any failure means "no tracks"
            self.logger.warning(
                "get_watch_playlist failed for mix %s: %s", playlist_id, err
            )
            return []

        tracks_raw = (watch_playlist or {}).get("tracks") or []
        result = []
        for index, track_obj in enumerate(tracks_raw, 1):
            with suppress(InvalidDataError, KeyError, TypeError):
                track = self._parse_track(_normalize_watch_track(track_obj))
                if track:
                    track.position = index
                    result.append(track)
        if not result:
            self.logger.warning(
                "mix %s returned no usable tracks (%d raw entries)",
                playlist_id,
                len(tracks_raw),
            )
        return self._drop_ai_tracks(result, f"mix {playlist_id}")

    @use_cache(PLAYLIST_TRACKS_CACHE_TTL, allow_expired_cache=True)
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Return playlist tracks for the given playlist id.

        Cached, and the reason is the opposite of what it looks like. Nothing
        here was ever cached, so every browse of a mix re-rolled it: the watch
        endpoint hands back a freshly generated list on each call, measured at
        147 tracks with no overlap between two consecutive requests. A list
        that changes every time you look at it is not a playlist, and it also
        meant a request to YouTube for every render.

        The three-hour window makes a mix stable enough to read, and Music
        Assistant bypasses it where freshness actually matters: playback and
        refill pass ``force_refresh``, which sets the bypass context variable
        ``use_cache`` honours, so a dynamic playlist still yields new tracks
        when it is played rather than browsed. ``allow_expired_cache`` serves
        the stale list immediately and refreshes behind it, so an expiry never
        shows up as a spinner. Matches the official ytmusic provider. See
        issue #56.
        """
        if page > 0:
            return []
        # Only ids that nothing else will answer for go to the radio endpoint
        # first. Editorial RDCLAK5uy_ playlists are deliberately excluded: the
        # watch endpoint does answer for them, so routing them here looked
        # right, but it stops at a queue's length and silently drops the rest
        # of the playlist ("'80s Pop": 200 tracks the ordinary way, 101 this
        # way). Falling through on an empty result keeps the old behaviour
        # available for anything RD-prefixed that reads as an ordinary
        # playlist after all.
        watch_first = _is_watch_only_playlist_id(prov_playlist_id)
        if watch_first:
            if radio_tracks := await self._get_radio_playlist_tracks(prov_playlist_id):
                return radio_tracks
        try:
            playlist_obj = await asyncio.to_thread(
                self._ytmusic.get_playlist, prov_playlist_id, limit=None
            )
            if not playlist_obj or "tracks" not in playlist_obj:
                raise ValueError("No tracks in playlist response")
            tracks_raw = playlist_obj["tracks"] or []
            result = []
            for index, track_obj in enumerate(tracks_raw, 1):
                if not track_obj.get("isAvailable", True):
                    continue
                with suppress(InvalidDataError, KeyError, TypeError):
                    track = self._parse_track(track_obj)
                    if track:
                        track.position = index
                        result.append(track)
            expected_count = self._parse_playlist_track_count(playlist_obj)
            # Private playlists never take the fill-the-gap fallback: yt-dlp
            # cannot see them, and the undercount test is chronically true for
            # "LM" because YouTube's reported count includes unavailable
            # entries, so every liked-songs fetch warned about a "missing"
            # playlist that was read just fine (issue #82).
            if not _is_private_playlist_id(prov_playlist_id) and (
                not tracks_raw
                or (expected_count is not None and len(tracks_raw) < expected_count)
            ):
                # A radio id cannot be opened without a seed video. When we
                # already parsed a track, hand its id over so the fallback has
                # a usable URL instead of a guaranteed failure.
                seed = _split_track_id(result[0].item_id)[0] if result else None
                ytdlp_result = await self._get_playlist_tracks_via_ytdlp(
                    prov_playlist_id, seed
                )
                if len(ytdlp_result) > len(result):
                    result = self._merge_playlist_track_results(result, ytdlp_result)
            await self._attach_album_years(result)
            return result
        except (MediaNotFoundError, UnplayableMediaError):
            raise
        except Exception as err:  # noqa: BLE001 - degrade to the yt-dlp fallback
            if _is_private_playlist_id(prov_playlist_id):
                # yt-dlp cannot read a private playlist, so degrading would
                # return an empty list, and the cache decorator would then
                # store that emptiness over the last good listing. Re-raise
                # instead: nothing is cached on an exception, so the
                # stale-while-revalidate cache keeps serving the previous
                # listing, and the real error reaches the log at a level the
                # user can see (issue #82).
                self.logger.warning(
                    "could not read private playlist %s with the authenticated "
                    "session (%s: %s)",
                    prov_playlist_id,
                    type(err).__name__,
                    err,
                )
                raise
            # ytmusicapi requires auth for some playlist types, and raises a
            # KeyError outright on song radio, so a fallback here is expected.
            # Include the error: without it this line cannot tell an
            # auth problem apart from an unparseable response.
            self.logger.debug(
                "ytmusicapi get_playlist_tracks failed for %s (%s: %s), using yt-dlp fallback",
                prov_playlist_id,
                type(err).__name__,
                err,
            )
            # An RD id that is not watch-only got here without trying the radio
            # endpoint, and an editorial playlist the ordinary path could not
            # read may still answer there. Watch-only ids already tried it
            # above, so this never repeats that call.
            if not watch_first and _is_radio_playlist_id(prov_playlist_id):
                if radio_tracks := await self._get_radio_playlist_tracks(prov_playlist_id):
                    return radio_tracks
            return await self._get_playlist_tracks_via_ytdlp(prov_playlist_id)

    @staticmethod
    def _parse_playlist_track_count(playlist_obj: dict) -> int | None:
        """Return the playlist's reported track count when ytmusicapi exposes it."""
        raw_count = playlist_obj.get("trackCount") or playlist_obj.get("count")
        if raw_count is None:
            return None
        if isinstance(raw_count, int):
            return raw_count
        if isinstance(raw_count, str):
            if match := re.search(r"\d+", raw_count.replace(",", "")):
                return int(match.group(0))
        return None

    @staticmethod
    def _merge_playlist_track_results(primary: list[Track], fallback: list[Track]) -> list[Track]:
        """Merge yt-dlp-only entries into the richer ytmusicapi result when possible."""
        if not primary:
            return fallback
        primary_by_id = {track.item_id: track for track in primary}
        fallback_ids = {track.item_id for track in fallback}
        if not fallback_ids.intersection(primary_by_id):
            return fallback

        merged = [primary_by_id.get(track.item_id, track) for track in fallback]
        merged_ids = {track.item_id for track in merged}
        merged.extend(track for track in primary if track.item_id not in merged_ids)
        return merged

    @staticmethod
    def _yt_playlist_url(playlist_id: str, seed_video_id: str | None = None) -> str:
        """Build the youtube.com URL yt-dlp needs for this playlist id.

        Radio ids need the watch form. ``playlist?list=RD...`` is rejected by
        YouTube with "This playlist type is unviewable", so a mix requested that
        way yields nothing at all (issue #47). The watch form needs a seed video
        id: song radio embeds one, and for a curated mix the caller passes the
        first track it already knows about.

        A radio id with no seed still falls through to the plain form. It will
        fail, but the caller now logs that failure rather than discarding it.
        """
        bare_id = _strip_browse_prefix(playlist_id)
        if bare_id.startswith(_YT_RADIO_PREFIX):
            seed = seed_video_id or _radio_seed_video_id(playlist_id)
            if seed:
                return f"https://www.youtube.com/watch?v={seed}&list={bare_id}"
        return f"https://www.youtube.com/playlist?list={bare_id}"

    async def _get_playlist_via_ytdlp(
        self, playlist_id: str, seed_video_id: str | None = None
    ) -> Playlist:
        """Get playlist metadata via yt-dlp flat extraction (no auth required).

        ``seed_video_id`` mirrors ``_get_playlist_tracks_via_ytdlp``: a radio id
        can only be opened through a watch URL, and a curated mix carries no
        seed of its own, so the caller supplies one. See ``_yt_playlist_url``.
        """

        def _extract() -> dict | None:
            if self._yt_dlp_module is None:
                self._yt_dlp_module = importlib.import_module("yt_dlp")
            yt_dlp = self._yt_dlp_module
            url = self._yt_playlist_url(playlist_id, seed_video_id)
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": "in_playlist",
                "playlistend": 1,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    return ydl.extract_info(url, download=False)
                except Exception as err:  # noqa: BLE001 - reported, then degraded
                    # Never swallow this silently. yt-dlp is built quiet, its
                    # logger is pinned to WARNING, and this used to discard the
                    # error too, so a failure here produced no log line
                    # anywhere. That is why issue #47 was unreportable: the
                    # user saw a stall with an empty log.
                    self.logger.warning(
                        "yt-dlp could not read playlist %s (%s): %s",
                        playlist_id,
                        url,
                        err,
                    )
                    return None

        info = await asyncio.to_thread(_extract)
        if not info:
            raise MediaNotFoundError(f"Playlist {playlist_id} not found")

        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=info.get("title") or info.get("playlist_title") or playlist_id,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/playlist?list={playlist_id}",
                )
            },
            is_editable=False,
        )
        playlist.owner = info.get("uploader") or info.get("channel") or self.name
        if thumbnails := info.get("thumbnails"):
            playlist.metadata.images = self._parse_thumbnails(thumbnails)
        return playlist

    async def _get_playlist_tracks_via_ytdlp(
        self, playlist_id: str, seed_video_id: str | None = None
    ) -> list[Track]:
        """Get playlist tracks via yt-dlp flat extraction (no auth required).

        ``seed_video_id`` is only meaningful for radio ids, which cannot be
        opened without one. Callers that already hold a track from the mix
        should pass it; see ``_yt_playlist_url``.
        """

        def _extract() -> dict | None:
            if self._yt_dlp_module is None:
                self._yt_dlp_module = importlib.import_module("yt_dlp")
            yt_dlp = self._yt_dlp_module
            url = self._yt_playlist_url(playlist_id, seed_video_id)
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "extract_flat": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    return ydl.extract_info(url, download=False)
                except Exception as err:  # noqa: BLE001 - reported, then degraded
                    # See the note in _get_playlist_via_ytdlp: this swallow is
                    # the reason a broken mix produced an empty log (issue #47).
                    self.logger.warning(
                        "yt-dlp could not read tracks for playlist %s (%s): %s",
                        playlist_id,
                        url,
                        err,
                    )
                    return None

        info = await asyncio.to_thread(_extract)
        if not info or not info.get("entries"):
            return []

        result = []
        for index, entry in enumerate(info["entries"] or [], 1):
            if not entry or not entry.get("id"):
                continue
            try:
                duration_val = entry.get("duration")
                track_obj = {
                    "videoId": entry["id"],
                    "title": entry.get("title") or entry["id"],
                    "duration": int(duration_val) if duration_val is not None else None,
                    "artists": [
                        {
                            "name": entry.get("uploader") or entry.get("channel") or "Unknown",
                            "id": None,
                        }
                    ],
                    "thumbnails": entry.get("thumbnails") or [],
                    "isAvailable": True,
                }
                # Remove None duration so _parse_track doesn't choke on it
                if track_obj["duration"] is None:
                    del track_obj["duration"]
                track = self._parse_track(track_obj)
                if track:
                    track.position = index
                    result.append(track)
            except (InvalidDataError, KeyError, TypeError):
                pass
        return result

    @use_cache(SIMILAR_TRACKS_CACHE_TTL, allow_expired_cache=True)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Return a dynamic list of tracks based on the provided track (song radio).

        ``prov_track_id`` may carry a trim window (``VIDEOID@start-end``); only
        the bare video id is meaningful to YTM, so strip it before the call.
        """
        video_id, _, _ = _split_track_id(prov_track_id)
        try:
            watch_playlist = await asyncio.to_thread(
                self._ytmusic.get_watch_playlist,
                videoId=video_id,
                limit=limit,
            )
        except Exception as err:  # noqa: BLE001
            # ytmusicapi can raise (e.g. KeyError 'endpoint') when the watch-playlist
            # response lacks the expected navigation structure. Degrade to an empty
            # radio list rather than failing the whole play_media command.
            #
            # Warning, not debug: this is the same endpoint and the same failure
            # the mix path reports, and radio mode going quiet with nothing in
            # the log at default level is exactly what made issue #47 take
            # months to pin down.
            self.logger.warning("get_watch_playlist failed for %s: %s", video_id, err)
            return []
        if not watch_playlist or "tracks" not in watch_playlist:
            return []
        tracks = []
        for track_obj in watch_playlist["tracks"]:
            with suppress(InvalidDataError, KeyError, TypeError):
                # Same reshaping as the mix path: this endpoint reports duration
                # as a clock string and artwork under a differently named key.
                track = self._parse_track(_normalize_watch_track(track_obj))
                if track:
                    tracks.append(track)
        return self._drop_ai_tracks(tracks, f"song radio for {video_id}")

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return stream details for the given track or podcast episode.

        ``item_id`` may carry a trim window (``VIDEOID@start-end``): the bare
        video id is used to resolve the stream, and ffmpeg input args trim the
        audio to that window so unwanted intros/outros (e.g. end-card audio)
        don't play.

        A podcast episode is addressed as ``PODCASTID|VIDEOID`` (issue #52).
        Once the show is stripped off, an episode is an ordinary YouTube video
        and resolves down exactly the same path as a track.
        """
        # Strip into a separate name, never over item_id: Music Assistant
        # correlates the returned StreamDetails back to the queue item by the id
        # it asked for, so handing back the bare video id would break that link.
        stream_id = item_id
        if media_type == MediaType.PODCAST_EPISODE or PODCAST_EPISODE_SPLITTER in item_id:
            _, stream_id = _split_episode_id(item_id)
        video_id, start, end = _split_track_id(stream_id)
        stream_format = await self._get_stream_format(video_id)
        self.logger.debug(
            "Resolved stream format '%s' for track %s", stream_format.get("format"), video_id
        )

        # Sit out any pre-roll ad window before handing the URL over, because
        # fetching inside it returns 403 (issue #51). Ahead of the expiration
        # maths below, so the TTL we report is measured from the moment Music
        # Assistant actually receives the URL rather than from before the wait.
        if self._preroll_supported and (wait := _preroll_wait_seconds(stream_format)):
            if wait > MAX_PREROLL_WAIT:
                self.logger.warning(
                    "Track %s reports a %.0fs pre-roll window, beyond the %.0fs "
                    "we are willing to hold playback for. Handing the url over "
                    "now; it will most likely be refused with a 403 and the "
                    "track skipped.",
                    video_id,
                    wait,
                    MAX_PREROLL_WAIT,
                )
            else:
                self.logger.debug(
                    "Waiting %.1fs for the pre-roll ad window on track %s before "
                    "handing over the stream url",
                    wait,
                    video_id,
                )
                await asyncio.sleep(wait)

        url = stream_format["url"]
        expiration = DEFAULT_STREAM_URL_EXPIRATION
        if parsed := parse_qs(urlparse(url).query):
            if expire_ts := parsed.get("expire", [None])[0]:
                expiration = int(expire_ts) - int(time.time())

        audio_ext = stream_format.get("audio_ext") or stream_format.get("ext", "m4a")
        content_type = ContentType.try_parse(audio_ext)
        if content_type == ContentType.UNKNOWN:
            # Music Assistant's ContentType knows codecs but not every container:
            # it has no WEBM member, so an Opus stream reports as "?" if we hand it
            # the container. Fall back to the codec, which it does understand. Only
            # on the unknown path, so m4a keeps reporting as M4A rather than MP4A.
            if acodec := stream_format.get("acodec"):
                content_type = ContentType.try_parse(acodec)
        stream_details = StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=content_type,
            ),
            # CUSTOM rather than HTTP: googlevideo now refuses unbounded
            # fetches (see STREAM_CHUNK_SIZE), so ffmpeg cannot open this URL
            # itself. Music Assistant instead pulls the bytes through
            # get_audio_stream below.
            stream_type=StreamType.CUSTOM,
            # Unused for CUSTOM playback, but Music Assistant's logging and the
            # live canary both read the resolved URL from here.
            path=url,
            # False on purpose: with can_seek=False Music Assistant hands the
            # seek offset to ffmpeg, which decodes and discards up to it. Seeking
            # at this layer would mean starting the fetch at a byte offset, and a
            # WebM/MP4 stream without its byte-0 headers is unparseable.
            can_seek=False,
            allow_seek=True,
            expiration=expiration,
            data={
                "url": url,
                # yt-dlp's per-format headers (User-Agent etc.), sent on every
                # chunk so the fetch looks like the client that minted the URL.
                "headers": dict(stream_format.get("http_headers") or {}),
            },
        )
        if channels := stream_format.get("audio_channels"):
            with suppress(ValueError, TypeError):
                stream_details.audio_format.channels = int(channels)
        if sample_rate := stream_format.get("asr"):
            with suppress(ValueError, TypeError):
                stream_details.audio_format.sample_rate = int(sample_rate)
        # Report the bitrate yt-dlp already told us. Without this the provider
        # never sets one, ffmpeg cannot recover it from a WebM/Opus stream, and
        # the UI shows nothing — which is why issue #41 went unnoticed for so
        # long: the number that would have exposed it was never displayed.
        if (abr := stream_format.get("abr")) is not None:
            with suppress(ValueError, TypeError):
                stream_details.audio_format.bit_rate = int(float(abr))

        # Apply the trim window via ffmpeg input args. -ss as an *input* option
        # seeks before decoding (fast & accurate enough for audio); -t bounds the
        # output duration relative to that seek, so it's computed as end-start.
        if start is not None or end is not None:
            trim_args: list[str] = []
            if start:
                trim_args += ["-ss", str(start)]
            if end is not None:
                duration = end - (start or 0)
                if duration > 0:
                    trim_args += ["-t", str(duration)]
            if trim_args:
                stream_details.extra_input_args = [
                    *getattr(stream_details, "extra_input_args", []),
                    *trim_args,
                ]
                # Report the trimmed length so the progress bar is correct.
                if end is not None:
                    stream_details.duration = end - (start or 0)
        return stream_details

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Stream the resolved audio in bounded-range chunks.

        googlevideo enforces *bounded* Range requests on these URLs: no Range
        header or an open-ended ``bytes=0-`` answers 403, ``bytes=0-N`` answers
        206 from the same URL (see STREAM_CHUNK_SIZE). ffmpeg only opens URLs
        the unbounded way, so the provider fetches the bytes itself, the same
        way YouTube's own clients and yt-dlp do, and hands them down the pipe.

        ``seek_position`` is part of the MusicProvider signature but always
        arrives as 0 here: ``can_seek`` is False, so Music Assistant seeks by
        decoding and discarding on its side of the pipe. That is deliberate; a
        byte-offset start would drop the container headers that live at byte 0
        and hand ffmpeg an unparseable stream.

        The end of the file is detected two ways: the total from the first
        response's ``Content-Range``, and a chunk answering with fewer bytes
        than asked for. The second also covers a server that ignores the range
        and answers 200 with the whole body.
        """
        del seek_position  # always 0, see docstring
        data = streamdetails.data if isinstance(streamdetails.data, dict) else {}
        url = data.get("url") or streamdetails.path
        base_headers: dict[str, str] = dict(data.get("headers") or {})
        chunk_size = STREAM_CHUNK_SIZE
        pos = 0
        total: int | None = None
        while total is None or pos < total:
            end = pos + chunk_size - 1
            if total is not None:
                end = min(end, total - 1)
            headers = {**base_headers, "Range": f"bytes={pos}-{end}"}
            received = 0
            async with self.mass.http_session.get(url, headers=headers) as response:
                if response.status == 403:
                    # googlevideo caps the range size and the cap has already
                    # moved once (10 MiB worked, then didn't). Nothing has
                    # been yielded for this request, so retrying the same
                    # position with a smaller ask is safe; only when the floor
                    # still 403s is the URL itself the problem.
                    if chunk_size > STREAM_CHUNK_FLOOR:
                        chunk_size = max(chunk_size // 2, STREAM_CHUNK_FLOOR)
                        self.logger.debug(
                            "403 for a %d-byte range on %s; retrying bytes %d- "
                            "with %d-byte chunks",
                            end - pos + 1,
                            streamdetails.item_id,
                            pos,
                            chunk_size,
                        )
                        continue
                    # The strongest signal this provider has that the URL is
                    # dead (expired, or enforcement changed again). Name the
                    # position: a 403 at byte 0 and a 403 mid-track are
                    # different bugs.
                    raise UnplayableMediaError(
                        f"googlevideo answered 403 at bytes {pos}-{end} for "
                        f"{streamdetails.item_id} even at the "
                        f"{STREAM_CHUNK_FLOOR}-byte floor"
                    )
                if response.status == 416:
                    # Asked past the end of the file: a Content-Range total
                    # that overstated the size. Everything real was streamed.
                    return
                response.raise_for_status()
                if total is None and response.status == 206:
                    total = _content_range_total(response.headers.get("Content-Range"))
                async for part in response.content.iter_chunked(STREAM_YIELD_SIZE):
                    received += len(part)
                    yield part
            if response.status == 200:
                # The server ignored the Range header and the whole body just
                # streamed out in one response. Asking again would replay it.
                return
            if received < (end - pos + 1):
                # Short answer: either the file ended inside this chunk, or
                # the connection dropped mid-chunk. Content-Range tells the
                # two apart; without one, short can only be treated as done.
                if total is None or pos + received >= total:
                    return
                # Truncated read. Resume from what actually arrived rather
                # than skipping the gap.
                pos += received
                continue
            pos = end + 1

    # ------------------------------------------------------------------
    # Library methods (require authentication)
    # ------------------------------------------------------------------

    def _is_auth_lapse(self, err: Exception) -> bool:
        """Return True if the error looks like an expired or invalid cookie."""
        return bool(AUTH_LAPSE_ERROR_PATTERN.search(str(err)))

    def _probe_session_alive(self) -> bool | None:
        """Best-effort check that the ytmusicapi session is still authenticated.

        Returns True if the probe succeeded with account data, False if it
        responded in a logged-out shape or raised an auth-lapse error, and
        None if the probe is undetermined (method missing, transient error).
        See issue #10 — used to detect partial-auth HTTP 200 responses that
        ytmusicapi unwraps to [] for library calls.
        """
        if not self._ytmusic:
            return None
        probe = getattr(self._ytmusic, "get_account_info", None)
        if probe is None:
            return None
        try:
            info = probe()
        except Exception as err:
            return False if self._is_auth_lapse(err) else None
        if isinstance(info, dict) and not info.get("accountName"):
            return False
        return True

    def _record_library_count(self, category: str, count: int) -> bool:
        """Track per-category populated state; return True if previously populated."""
        # handle_async_init seeds this, but guard anyway: the attribute is
        # deliberately not a class-level default (that would share one dict
        # across instances), so any path reaching a library call without a
        # full init would otherwise hit AttributeError.
        if not hasattr(self, "_library_seen_nonempty"):
            self._library_seen_nonempty = {}
        prev = self._library_seen_nonempty.get(category, False)
        if count > 0:
            self._library_seen_nonempty[category] = True
        return prev

    async def _require_library_auth(self, category: str) -> None:
        """Fail a library sync we cannot vouch for, rather than reporting it empty.

        Music Assistant treats a completed sync as authoritative. Anything it
        previously held for this provider and did not see this round is dropped
        from the library, and any item left with no provider claiming it is
        unfavourited (``sync_library`` in its ``MusicProvider`` base). So
        yielding nothing is not a neutral "no data": it is an instruction to
        delete.

        The provider declares the library features unconditionally at setup, so
        a failed or lapsed cookie does not stop Music Assistant asking. Before
        this guard the library methods answered that question with a silent
        early return, which is why a cookie going stale emptied someone's
        favourites rather than just failing to refresh them. Issue #55.

        Genuinely anonymous instances are exempt: they have no library, never
        had one, and nothing of theirs is waiting to be deleted.
        """
        if self._authenticated:
            return
        auth_type = (self.config.get_value(CONF_AUTH_TYPE) if self.config else None) or (
            AUTH_TYPE_NONE
        )
        if auth_type != AUTH_TYPE_COOKIE:
            return
        err = RuntimeError(
            "Cookie authentication is configured but not active, so the library "
            "cannot be read. Failing this sync on purpose: reporting an empty "
            "library would make Music Assistant delete the items it already has "
            "(issue #55). Refresh the cookie on the provider config page."
        )
        self._warn_library_error(f"get_library_{category}", err)
        raise err

    async def _guard_partial_auth_empty(self, category: str, count: int) -> None:
        """Raise on a suspected partial-auth empty sync to preserve MA's library.

        Fires whenever a sync comes back empty and a side-channel probe confirms
        the session is no longer authenticated.

        It used to also require the category to have been seen populated earlier
        in this process. That state lives in ``_library_seen_nonempty``, which is
        in memory and reset by ``handle_async_init``, so after any restart every
        category read as "never populated" and the guard returned before probing
        anything. A lapsed cookie plus a restart therefore looked exactly like a
        genuinely empty library, which is the shape of issue #55. The probe is
        one request and only runs when a sync returned nothing, which is rare
        enough that its cost is irrelevant next to wiping someone's library.
        """
        self._record_library_count(category, count)
        if count > 0:
            # A populated answer is proof the session works, so a lapse notice
            # raised by an earlier one-off error has outlived its cause.
            if self._authenticated:
                self._clear_auth_notice()
            return
        alive = await asyncio.to_thread(self._probe_session_alive)
        if alive is None:
            # Undetermined. Accept the empty result rather than failing every
            # genuinely empty library whenever the probe is unavailable, but say
            # so, because this is the one path that can still lose items.
            self.logger.warning(
                "get_library_%s returned nothing and the session probe was "
                "inconclusive. Treating the empty library as genuine. If items "
                "disappear from Music Assistant after this, the cookie has "
                "most likely lapsed and needs refreshing.",
                category,
            )
            return
        if alive:
            if self._authenticated:
                self._clear_auth_notice()
            return
        err = RuntimeError(
            "Library returned empty but the session probe reports the account "
            "is not signed in (suspected cookie lapse, issues #10 and #55)"
        )
        self._warn_library_error(f"get_library_{category}", err)
        # The lapse pattern does not match this wording, so _warn_library_error
        # leaves the notice alone. The probe is the stronger evidence of the two.
        if self._authenticated:
            self._raise_auth_notice(err)
        raise err

    def _warn_library_error(self, context: str, err: Exception) -> None:
        """Log a library-call failure, upgrading the message on suspected auth lapse."""
        if self._is_auth_lapse(err):
            # Only for a cookie that worked at startup. One that never did
            # already has its notice from handle_async_init.
            if self._authenticated:
                self._raise_auth_notice(err)
            if not self._auth_lapse_warned:
                self._auth_lapse_warned = True
                self.logger.warning(
                    "%s failed with an auth error (%s). Your YouTube cookie has "
                    "likely expired. Refresh it on the provider config page by "
                    "capturing the Cookie header from a `youtubei/v1/...` request "
                    "on music.youtube.com.",
                    context,
                    err,
                )
            else:
                self.logger.debug("%s failed (auth lapse, already warned): %s", context, err)
        else:
            self.logger.warning("%s failed: %s", context, err)

    async def _fetch_library(self, method: str, **kwargs: Any) -> list[dict]:
        """Run one ytmusicapi library call, failing the sync if it cannot answer.

        Music Assistant treats a completed sync as authoritative: anything it
        held for this provider and did not see this round is dropped from the
        library, and whatever is then left with no provider claiming it is
        unfavourited. So the "log it and return nothing" this replaces was not
        a neutral "no data". It was an instruction to delete, issued on the
        strength of a rate limit, a timeout or a stray parse error. Issue #64,
        the same thesis as #55.

        One helper rather than five copies. The swallow it replaces was written
        once and then copied into each library method as it was added, most
        recently into ``get_library_podcasts``. There is now one place to get
        this wrong instead of five.

        The original exception is re-raised unchanged rather than wrapped. Its
        type and message are what a bug report needs, and ``_warn_library_error``
        has already logged the explanation next to it.

        ``except Exception`` is deliberate and must not be widened to
        ``BaseException``: Music Assistant cancels the sync task on shutdown,
        and a ``CancelledError`` is not a library failure to be logged as one.
        """
        try:
            if self._ytmusic is None:
                raise RuntimeError(
                    "the ytmusicapi client is not initialised, so the library "
                    "cannot be read"
                )
            call = getattr(self._ytmusic, method, None)
            if call is None:
                raise RuntimeError(
                    f"the installed ytmusicapi has no {method}(). Upgrade it "
                    "inside the Music Assistant container with "
                    "`pip install --upgrade ytmusicapi`, then restart Music "
                    "Assistant: the old module is already loaded in the "
                    "running process, so the upgrade on its own changes "
                    "nothing"
                )
            results = await asyncio.to_thread(call, **kwargs)
            if results is None:
                return []
            if not isinstance(results, (list, tuple)):
                # A shape we cannot count is a shape we cannot vouch for. len()
                # of a dict counts its keys, so a changed response would sail
                # past the empty-library guard on a non-zero count and then
                # yield nothing, which is the deletion this helper exists to
                # prevent.
                raise TypeError(f"{method} returned {type(results).__name__}, expected a list")
        except Exception as err:
            # Warned here rather than at the call sites so the message still
            # names the ytmusicapi method that actually failed.
            self._warn_library_error(method, err)
            raise
        return list(results)

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Get artists from the user's library (subscriptions + library artists).

        Two independent fetches, and a failure in either fails the whole method.
        Each used to be swallowed on its own, after which the empty-library
        guard ran on ``len(subs) + len(lib_artists)``: a failed subscriptions
        call next to five library artists produced a non-zero count, a clean
        sync, and the deletion of every subscribed artist. That sum only means
        something when both halves actually answered. Issue #64.

        The second fetch is skipped once the first has failed. Whatever took out
        one call (a lapsed cookie, a rate limit, YouTube being down) is about to
        take out the other, its result would be discarded by the raise anyway,
        and retrying into a rate limit is how a short one becomes a long one.
        """

        if not self._authenticated:
            await self._require_library_auth("artists")
            return
        subs = await self._fetch_library("get_library_subscriptions", limit=9999)
        lib_artists = await self._fetch_library("get_library_artists", limit=9999)
        # Reached only when both fetches answered, which is the only reading of
        # this sum that means anything.
        await self._guard_partial_auth_empty("artists", len(subs) + len(lib_artists))
        seen_ids: set[str] = set()
        for item in subs:
            with suppress(InvalidDataError, KeyError, TypeError):
                # _parse_artist reads browseId/artist directly, so the
                # search-result keys do not need pre-mapping here.
                artist = self._parse_artist(item)
                if artist.item_id not in seen_ids:
                    seen_ids.add(artist.item_id)
                    yield artist
        for item in lib_artists:
            with suppress(InvalidDataError, KeyError, TypeError):
                # _parse_artist reads browseId/artist directly, so the
                # search-result keys do not need pre-mapping here.
                artist = self._parse_artist(item)
                if artist.item_id not in seen_ids:
                    seen_ids.add(artist.item_id)
                    yield artist

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Get albums from the user's library."""

        if not self._authenticated:
            await self._require_library_auth("albums")
            return
        results = await self._fetch_library("get_library_albums", limit=9999)
        await self._guard_partial_auth_empty("albums", len(results))
        for item in results:
            with suppress(InvalidDataError, KeyError, TypeError):
                yield self._parse_album(item, item.get("browseId"))

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Get tracks from the user's library."""

        if not self._authenticated:
            await self._require_library_auth("tracks")
            return
        results = await self._fetch_library("get_library_songs", limit=9999)
        await self._guard_partial_auth_empty("tracks", len(results))
        for item in results:
            with suppress(InvalidDataError, KeyError, TypeError):
                track = self._parse_track(item)
                if track:
                    yield track

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Get playlists from the user's library."""

        if not self._authenticated:
            await self._require_library_auth("playlists")
            return
        results = await self._fetch_library("get_library_playlists", limit=9999)
        await self._guard_partial_auth_empty("playlists", len(results))
        for item in results:
            with suppress(InvalidDataError, KeyError, TypeError):
                item.setdefault("id", item.get("playlistId"))
                yield self._parse_playlist(item)

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast, None]:
        """Get the shows the user subscribes to.

        Same guard order as every other library method, and for the same reason:
        Music Assistant deletes anything a completed sync did not return, so an
        unauthenticated run has to fail rather than answer "no subscriptions",
        and so does a run whose fetch failed. See issues #55 and #64.
        """
        if not self._authenticated:
            await self._require_library_auth("podcasts")
            return
        results = await self._fetch_library("get_library_podcasts", limit=LIBRARY_PODCAST_LIMIT)
        shows = [
            obj
            for obj in results
            if str(obj.get("podcastId") or "") not in PERSONAL_PODCAST_PLAYLIST_IDS
        ]
        # Counted after the auto-playlists are dropped. Counting before would
        # make a lapsed session look populated on the strength of two entries
        # YouTube returns whether or not you subscribe to anything.
        await self._guard_partial_auth_empty("podcasts", len(shows))
        for obj in shows:
            with suppress(InvalidDataError, KeyError, TypeError):
                yield self._parse_podcast(obj)

    async def library_add(self, item: MediaItemType) -> bool:
        """Add an item to the user's library."""
        if not self._authenticated:
            return False
        prov_mapping = next(
            (m for m in item.provider_mappings if m.provider_instance == self.instance_id),
            None,
        )
        if not prov_mapping:
            return False
        item_id = prov_mapping.item_id
        try:
            if item.media_type == MediaType.ARTIST:
                await asyncio.to_thread(self._ytmusic.subscribe_artists, [item_id])
            elif item.media_type in (MediaType.ALBUM, MediaType.PLAYLIST):
                await asyncio.to_thread(self._ytmusic.rate_playlist, item_id, "LIKE")
            elif item.media_type == MediaType.TRACK:
                # A library track is a liked song. The id may carry a trim
                # window ("VIDEOID@start-end"); only the bare video id rates.
                video_id, _, _ = _split_track_id(item_id)
                await asyncio.to_thread(self._ytmusic.rate_song, video_id, "LIKE")
            else:
                return False
            return True
        except Exception as err:
            # HTTP 403 typically means the item is already in the user's library
            # (e.g. a playlist they own — YouTube refuses to "like" your own playlist).
            # Treat this as a benign no-op so MA's library cache stays consistent.
            if "403" in str(err) and item.media_type in (MediaType.ALBUM, MediaType.PLAYLIST):
                self.logger.debug(
                    "library_add for %s returned 403 (likely user-owned, already in library)",
                    item_id,
                )
                return True
            self._warn_library_error(f"library_add for {item_id}", err)
            return False

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove an item from the user's library."""
        if not self._authenticated:
            return False
        try:
            if media_type == MediaType.ARTIST:
                await asyncio.to_thread(self._ytmusic.unsubscribe_artists, [prov_item_id])
            elif media_type in (MediaType.ALBUM, MediaType.PLAYLIST):
                await asyncio.to_thread(
                    self._ytmusic.rate_playlist, prov_item_id, "INDIFFERENT"
                )
            elif media_type == MediaType.TRACK:
                # Un-rate the song so the delete also removes the like on
                # YouTube; otherwise the next sync re-imports it (issue #82).
                video_id, _, _ = _split_track_id(prov_item_id)
                await asyncio.to_thread(
                    self._ytmusic.rate_song, video_id, "INDIFFERENT"
                )
            else:
                return False
            return True
        except Exception as err:
            # HTTP 403 typically means the item is user-owned and cannot be
            # un-rated — for MA's purposes the "not in library" state is satisfied.
            if "403" in str(err) and media_type in (MediaType.ALBUM, MediaType.PLAYLIST):
                self.logger.debug(
                    "library_remove for %s returned 403 (likely user-owned)",
                    prov_item_id,
                )
                return True
            self._warn_library_error(f"library_remove for {prov_item_id}", err)
            return False

    async def recommendations(self) -> list[RecommendationFolder]:
        """Get personalized recommendations from YouTube Music home feed."""
        if not self._authenticated:
            return []
        try:
            home = await asyncio.to_thread(self._ytmusic.get_home, limit=6)
        except Exception as err:
            self._warn_library_error("get_home", err)
            return []
        folders: list[RecommendationFolder] = []
        for section in home:
            title = section.get("title", "Recommendations")
            items: list[MediaItemType | ItemMapping] = []
            for content in section.get("contents", []):
                if not content:
                    continue
                with suppress(InvalidDataError, KeyError, TypeError):
                    if video_id := content.get("videoId"):
                        track = self._parse_track(content)
                        # Filtered per track, so a folder left with nothing is
                        # dropped by the `if items` check below rather than
                        # rendering as an empty shelf.
                        if track and self._drop_ai_tracks([track], f"home feed '{title}'"):
                            items.append(track)
                    elif browse_id := content.get("browseId"):
                        if content.get("subscribers") or content.get("type") == "artist":
                            items.append(self._get_item_mapping(
                                MediaType.ARTIST, browse_id, content.get("title", "")
                            ))
                        elif content.get("type") in ("album", "single", "ep"):
                            items.append(self._get_item_mapping(
                                MediaType.ALBUM, browse_id, content.get("title", "")
                            ))
                        elif content.get("playlistId") or "playlist" in content.get("type", ""):
                            items.append(self._get_item_mapping(
                                MediaType.PLAYLIST,
                                content.get("playlistId", browse_id),
                                content.get("title", ""),
                            ))
            if items:
                folder = RecommendationFolder(
                    item_id=f"ytm_rec_{title.lower().replace(' ', '_')}",
                    provider=self.instance_id,
                    name=title,
                    items=UniqueList(items),
                )
                folders.append(folder)
        return folders

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # AI-music filter (issue #53)
    # ------------------------------------------------------------------

    def _load_ai_filter_config(self) -> None:
        """Read the filter settings into the sets the hot path consults."""
        self._ai_filter_enabled = bool(self.config.get_value(CONF_FILTER_AI_MUSIC))
        self._ai_blocklist_url = str(self.config.get_value(CONF_AI_BLOCKLIST_URL) or "").strip()
        raw = str(self.config.get_value(CONF_AI_BLOCKLIST) or "")
        # Semicolons, not commas. A single-line config box needs some in-line
        # separator, and commas are far too common inside real artist names:
        # splitting on them turns "Earth, Wind & Fire" into a rule that blocks
        # every artist called "Earth". Semicolons effectively never appear in
        # a name, so they separate unambiguously.
        self._ai_local_channel_ids, self._ai_local_names = _parse_blocklist(
            raw.replace(";", "\n")
        )
        self._ai_blocked_channel_ids = self._ai_local_channel_ids
        self._ai_blocked_names = self._ai_local_names
        self._ai_blocklist_fetched_at = 0.0
        if self._ai_filter_enabled:
            self.logger.debug(
                "AI filter enabled with %d local channel ids and %d local names",
                len(self._ai_local_channel_ids),
                len(self._ai_local_names),
            )

    async def _refresh_remote_blocklist(self) -> None:
        """Fetch the remote list and merge it over the local entries.

        Never raises. A failure leaves whatever is already loaded in place,
        because the alternative is a filter that quietly empties itself when a
        community list goes offline, which would look identical to the filter
        being switched off.
        """
        url = self._ai_blocklist_url
        if not url or self._ai_blocklist_refreshing:
            return
        self._ai_blocklist_refreshing = True
        # Stamped before the attempt, not after it, so the backoff applies to a
        # failure too. Only advancing this on success is what let a dead URL
        # re-fire on every single filtered call.
        self._ai_blocklist_attempted_at = time.time()
        try:
            # Imported here rather than at module scope: aiohttp is guaranteed
            # inside Music Assistant but is not a declared test dependency, and
            # a module-level import would make the offline suite unrunnable
            # without it.
            aiohttp = importlib.import_module("aiohttp")
            async with self.mass.http_session.get(
                url, timeout=aiohttp.ClientTimeout(total=AI_BLOCKLIST_TIMEOUT)
            ) as response:
                response.raise_for_status()
                body = await response.text()
        except Exception as err:  # noqa: BLE001 - any failure means "keep the old list"
            self.logger.warning(
                "could not refresh the AI blocklist from %s: %s. Keeping the "
                "previous list of %d channel ids and %d names.",
                url,
                err,
                len(self._ai_blocked_channel_ids),
                len(self._ai_blocked_names),
            )
            return
        finally:
            self._ai_blocklist_refreshing = False

        remote_ids, remote_names = _parse_blocklist(body)
        if not remote_ids and not remote_names:
            self.logger.warning(
                "the AI blocklist at %s parsed to nothing. Either it is empty "
                "or its format is not one this provider understands; only your "
                "own entries are in effect.",
                url,
            )
        self._ai_blocked_channel_ids = self._ai_local_channel_ids | remote_ids
        self._ai_blocked_names = self._ai_local_names | remote_names
        self._ai_blocklist_fetched_at = time.time()
        self.logger.debug(
            "AI blocklist refreshed from %s: %d channel ids, %d names in total",
            url,
            len(self._ai_blocked_channel_ids),
            len(self._ai_blocked_names),
        )

    def _schedule_blocklist_refresh_if_stale(self) -> None:
        """Kick off a background refresh when the fetched list has aged out.

        Fire and forget. The caller is on the path that builds a queue, so it
        uses the list it already has and picks up the new one next time round.
        """
        if not self._ai_filter_enabled or not self._ai_blocklist_url:
            return
        if self._ai_blocklist_refreshing:
            return
        now = time.time()
        if self._ai_blocklist_fetched_at and now - self._ai_blocklist_fetched_at < AI_BLOCKLIST_TTL:
            return
        if now - self._ai_blocklist_attempted_at < AI_BLOCKLIST_RETRY_AFTER:
            return
        with suppress(RuntimeError):
            # RuntimeError when there is no running loop, which only happens in
            # tests driving these helpers synchronously.
            task = asyncio.get_running_loop().create_task(self._refresh_remote_blocklist())
            # Keep a reference so the task is not garbage collected mid-flight.
            self._ai_blocklist_task = task

    def _is_ai_blocked(self, track: Track) -> bool:
        """Whether ``track`` is by an artist on the blocklist."""
        for artist in track.artists or ():
            artist_id = getattr(artist, "item_id", None)
            if artist_id and artist_id in self._ai_blocked_channel_ids:
                return True
            name = getattr(artist, "name", None)
            if name and _normalize_artist_name(name) in self._ai_blocked_names:
                return True
        return False

    def _drop_ai_tracks(self, tracks: list[Track], source: str) -> list[Track]:
        """Remove blocklisted tracks from an auto-generated list.

        Applied only to lists YouTube generated for us (radio, mixes, similar
        tracks, recommendations). Search, library and hand-picked playlists are
        left alone: filtering those would make a deliberate lookup fail to find
        something the user explicitly asked for.
        """
        if not self._ai_filter_enabled:
            return tracks
        # Ahead of the empty-list exit, not after it. A config with only a
        # remote URL starts with both sets empty, so gating the refresh on
        # having entries meant that if the one fetch at startup failed, nothing
        # ever retried and the filter stayed off for the process lifetime.
        # Ahead of the empty-list exit, not after it. A config with only a
        # remote URL starts with both sets empty, so gating the refresh on
        # having entries meant that if the one fetch at startup failed, nothing
        # ever retried and the filter stayed off for the process lifetime.
        self._schedule_blocklist_refresh_if_stale()
        if not self._ai_blocked_channel_ids and not self._ai_blocked_names:
            return tracks
        kept = [track for track in tracks if not self._is_ai_blocked(track)]
        if dropped := len(tracks) - len(kept):
            self.logger.debug(
                "AI filter removed %d of %d tracks from %s", dropped, len(tracks), source
            )
        return kept

    async def _get_stream_format(self, item_id: str) -> dict[str, Any]:
        """Extract the best audio stream URL via yt-dlp (no cookies required)."""

        prefer_quality = self._prefer_quality
        # Defensive: strip any trim suffix so yt-dlp always sees a bare video id.
        video_id, _, _ = _split_track_id(item_id)

        def _extract() -> dict[str, Any]:
            if self._yt_dlp_module is None:
                self._yt_dlp_module = importlib.import_module("yt_dlp")
            yt_dlp = self._yt_dlp_module

            # Decided here rather than at the call site because this is the only
            # place the module is guaranteed to be imported.
            ytdlp_version = getattr(getattr(yt_dlp, "version", None), "__version__", None)
            self._preroll_supported = _ytdlp_honours_preroll(ytdlp_version)

            # Checked against the *imported* module, not the installed package:
            # setup upgrades the package on disk, but a process that already
            # imported the old yt_dlp keeps it until Music Assistant restarts,
            # and that gap looks exactly like the outage the upgrade fixed.
            if not self._warned_stale_ytdlp and not _ytdlp_can_stream(ytdlp_version):
                self._warned_stale_ytdlp = True
                self.logger.warning(
                    "yt-dlp %s still extracts through the android_vr client, "
                    "which YouTube shut down on 2026-08-17: its URLs stop "
                    "serving after ~1 MiB, so every track dies mid-stream. "
                    "The provider requests %s at startup; restart Music "
                    "Assistant so the upgrade is picked up, or run "
                    "pip install -U --pre 'yt-dlp[default]' in the container.",
                    ytdlp_version,
                    MIN_YTDLP_REQUIREMENT,
                )

            url = f"{YTM_DOMAIN}/watch?v={video_id}"
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                # Deliberately no "player_client" pin. It is tempting to name the
                # clients that work without an account, but no single list is valid
                # across the yt-dlp range the manifest allows: android_vr does not
                # exist in 2024.01, and android_music was removed by 2026.07, where
                # the remaining android/ios clients are GVS PO-token gated and yield
                # no usable anonymous formats at all. yt-dlp's own defaults track
                # that moving target for us, so let them. See PR #44.
                "extractor_args": {
                    "youtube": {
                        "skip": ["translated_subs", "dash"]
                    },
                },
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                except yt_dlp.utils.DownloadError as err:
                    raise UnplayableMediaError(str(err)) from err

                if not info or "formats" not in info:
                    raise UnplayableMediaError(f"No formats found for {video_id}")

                # Rank by bitrate, never by container. A bare container name like
                # "m4a" outranks bitrate in a yt-dlp selector, and on a free account
                # the only m4a is itag 139 at 48 kbps, so "m4a/bestaudio/best" picked
                # 48 kbps over the 160 kbps Opus sitting next to it. That was issue
                # #41. The compatibility branch still asks for m4a, but as a filter on
                # bestaudio rather than ahead of it, so it takes the best AAC stream
                # available instead of the worst audio of any kind.
                fmt_selector_str = (
                    "bestaudio/best"
                    if prefer_quality
                    else "bestaudio[ext=m4a]/bestaudio/best"
                )
                try:
                    format_selector = ydl.build_format_selector(fmt_selector_str)
                    stream_format = next(
                        format_selector({"formats": info["formats"]}),
                        None,
                    )
                except Exception:
                    stream_format = None

                if not stream_format:
                    # Last resort: the selector produced nothing, so rank the
                    # audio-only formats ourselves. The previous code took
                    # audio_formats[-1] and relied on yt-dlp listing formats
                    # worst-to-best. That ordering is a convention, not a
                    # contract, and trusting an implicit ordering instead of the
                    # bitrate is the same mistake that produced issue #41 in the
                    # selector. Rank explicitly, mirroring the selector above so
                    # both paths agree on what "best" means.
                    audio_formats = [
                        f for f in info["formats"] if f.get("vcodec") == "none"
                    ]
                    if audio_formats:
                        stream_format = max(
                            audio_formats,
                            key=lambda fmt: _rank_audio_format(fmt, prefer_quality),
                        )
                    else:
                        # No audio-only format at all. Anything here is a video
                        # format and a poor stream, but it is still better than
                        # failing outright, which is what the original code did.
                        stream_format = info["formats"][-1]

                return stream_format

        return await asyncio.to_thread(_extract)

    def _catalog_audio_format(self) -> AudioFormat:
        """Format a catalog entry advertises before any stream is resolved.

        This is necessarily nominal: the real container is only known once
        ``_get_stream_format`` has run. It still has to be broadly right,
        because Music Assistant surfaces it in the UI and uses it to order
        providers when the same track resolves through more than one.

        Every mapping used to claim M4A. Since the issue #41 fix the quality
        branch selects ``bestaudio``, which on a free account is Opus, so the
        advertised format contradicted every stream the provider actually
        handed back. Follow the configured preference instead: Opus when
        ranking by bitrate, M4A when the compatibility toggle pins us to AAC.
        """
        return AudioFormat(
            content_type=ContentType.OPUS if self._prefer_quality else ContentType.M4A,
        )

    def _minimal_track(self, track_id: str) -> Track:
        """Return a bare-minimum Track so playback can still proceed.

        ``track_id`` may be a trimmed (``VIDEOID@start-end``) id: the watch URL
        uses the bare video id while the track keeps the encoded id so the trim
        survives even when metadata lookup failed.
        """
        video_id, _, _ = _split_track_id(track_id)
        return Track(
            item_id=track_id,
            provider=self.instance_id,
            name=video_id,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/watch?v={video_id}",
                    audio_format=self._catalog_audio_format(),
                )
            },
            artists=[
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id="unknown",
                    provider=self.instance_id,
                    name="Unknown Artist",
                )
            ],
        )

    def _parse_track(self, track_obj: dict, track_number: int = 0) -> Track:
        """Parse a YTM track dict into a Track model object."""
        track_id = track_obj.get("videoId")
        if not track_id:
            raise InvalidDataError("Track is missing videoId")
        track_id = str(track_id)
        name, version = parse_title_and_version(track_obj.get("title", "Unknown"))
        track = Track(
            item_id=track_id,
            provider=self.instance_id,
            name=name,
            version=version,
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=track_obj.get("isAvailable", True),
                    url=f"{YTM_DOMAIN}/watch?v={track_id}",
                    audio_format=self._catalog_audio_format(),
                )
            },
            disc_number=0,
            track_number=track_obj.get("trackNumber") or track_number or 0,
        )

        artists_raw = track_obj.get("artists", [])
        if artists_raw:
            track.artists = [
                self._get_artist_item_mapping(a)
                for a in artists_raw
                if (
                    a.get("id")
                    or a.get("channelId")
                    or a.get("browseId")
                    or a.get("name") == "Various Artists"
                    or a.get("artist") == "Various Artists"
                )
            ]
        # Fall back: build a minimal artist mapping from whatever name is available
        if not track.artists and artists_raw:
            first = artists_raw[0]
            name_only = first.get("name", "Unknown Artist")
            track.artists = [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=f"unknown_{name_only}",
                    provider=self.instance_id,
                    name=name_only,
                )
            ]
        # A videos-tab search result sometimes carries no artists at all: the
        # plain-YouTube uploads issue #77 exists to surface are exactly the
        # ones whose subtitle has no linked channel. Dropping them would
        # re-lose that content, so they get the same placeholder _minimal_track
        # uses. Everything else keeps the raise: a catalog song without artists
        # is malformed data, not a known shape.
        if not track.artists and track_obj.get("resultType") == "video":
            track.artists = [
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id="unknown",
                    provider=self.instance_id,
                    name="Unknown Artist",
                )
            ]
        if not track.artists:
            raise InvalidDataError("Track is missing artists")

        if track_obj.get("thumbnails"):
            track.metadata.images = self._parse_thumbnails(
                track_obj["thumbnails"], keep_small_frame=True
            )
        album = track_obj.get("album")
        if isinstance(album, dict) and album.get("id"):
            track.album = self._get_item_mapping(MediaType.ALBUM, album["id"], album.get("name", ""))
        if "isExplicit" in track_obj:
            track.metadata.explicit = track_obj["isExplicit"]
        if "duration" in track_obj and str(track_obj["duration"]).isdigit():
            track.duration = int(track_obj["duration"])
        elif "duration_seconds" in track_obj and str(track_obj.get("duration_seconds", "")).isdigit():
            track.duration = int(track_obj["duration_seconds"])
        return track

    def _parse_album(self, album_obj: dict, album_id: str | None = None) -> Album:
        """Parse a YTM album dict into an Album model object."""
        album_id = album_id or album_obj.get("id") or album_obj.get("browseId")
        if not album_id:
            raise InvalidDataError("Album is missing an ID")

        title_raw = album_obj.get("title") or album_obj.get("name") or ""
        name, version = parse_title_and_version(title_raw)
        album = Album(
            item_id=album_id,
            name=name,
            version=version,
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/playlist?list={album_obj.get('audioPlaylistId', album_id)}",
                )
            },
        )
        if album_obj.get("year") and str(album_obj["year"]).isdigit():
            album.year = int(album_obj["year"])
        if "thumbnails" in album_obj:
            album.metadata.images = UniqueList(self._parse_thumbnails(album_obj["thumbnails"]))
        if description := album_obj.get("description"):
            album.metadata.description = unquote(description)
        if "isExplicit" in album_obj:
            album.metadata.explicit = album_obj["isExplicit"]
        if "artists" in album_obj:
            album.artists = UniqueList(
                [
                    self._get_artist_item_mapping(a)
                    for a in album_obj["artists"]
                    if (
                        a.get("id")
                        or a.get("channelId")
                        or a.get("browseId")
                        or a.get("name") == "Various Artists"
                        or a.get("artist") == "Various Artists"
                    )
                ]
            )
        album_type_raw = album_obj.get("type", "")
        if album_type_raw == "Single":
            album.album_type = AlbumType.SINGLE
        elif album_type_raw == "EP":
            album.album_type = AlbumType.EP
        elif album_type_raw == "Album":
            album.album_type = AlbumType.ALBUM
        else:
            album.album_type = AlbumType.UNKNOWN
        inferred = infer_album_type(name, version)
        if inferred in (AlbumType.SOUNDTRACK, AlbumType.LIVE):
            album.album_type = inferred
        return album

    def _parse_artist(self, artist_obj: dict) -> Artist:
        """Parse a YTM artist dict into an Artist model object."""
        artist_id = (
            artist_obj.get("channelId")
            or artist_obj.get("browseId")  # search results (filter="artists") use browseId
            or artist_obj.get("id")
        )
        if not artist_id and (
            artist_obj.get("name") == "Various Artists"
            or artist_obj.get("artist") == "Various Artists"
        ):
            artist_id = VARIOUS_ARTISTS_YTM_ID
        if not artist_id:
            raise InvalidDataError("Artist is missing an ID")
        artist = Artist(
            item_id=artist_id,
            # search results (filter="artists") use the "artist" key for the
            # display name instead of "name". Final fallback is unconditional so
            # a present-but-empty value cannot leak through as the name.
            name=artist_obj.get("name") or artist_obj.get("artist") or "Unknown Artist",
            provider=self.instance_id,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/channel/{artist_id}",
                )
            },
        )
        if "description" in artist_obj:
            artist.metadata.description = artist_obj["description"]
        if artist_obj.get("thumbnails"):
            artist.metadata.images = self._parse_thumbnails(artist_obj["thumbnails"])
        return artist

    def _parse_playlist(self, playlist_obj: dict) -> Playlist:
        """Parse a YTM playlist dict into a Playlist model object."""
        # ytmusicapi uses different key names depending on context:
        #   get_playlist()  → "id"
        #   search results  → "browseId"  (e.g. "VLPLxxx")
        #   some contexts   → "playlistId"
        playlist_id = (
            playlist_obj.get("id")
            or playlist_obj.get("playlistId")
            or playlist_obj.get("browseId", "")
        )
        playlist_name = playlist_obj.get("title", "Unknown Playlist")
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=playlist_name,
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/playlist?list={playlist_id}",
                )
            },
            is_editable=False,
        )
        if "description" in playlist_obj:
            playlist.metadata.description = playlist_obj["description"]
        if playlist_obj.get("thumbnails"):
            playlist.metadata.images = self._parse_thumbnails(playlist_obj["thumbnails"])
        authors = playlist_obj.get("author")
        if isinstance(authors, str):
            playlist.owner = authors
        elif isinstance(authors, list) and authors:
            playlist.owner = authors[0].get("name", self.name)
        elif isinstance(authors, dict):
            playlist.owner = authors.get("name", self.name)
        else:
            playlist.owner = self.name
        return playlist

    def _parse_podcast(self, podcast_obj: dict, podcast_id: str | None = None) -> Podcast:
        """Parse a YTM podcast dict into a Podcast model object.

        ``podcast_id`` overrides whatever the payload carries, because the show
        detail response does not repeat its own id and the search result spells
        it as an ``MPSP``-prefixed browse id.
        """
        resolved_id = podcast_id or _strip_podcast_browse_prefix(
            podcast_obj.get("podcastId") or podcast_obj.get("browseId") or ""
        )
        if not resolved_id:
            raise InvalidDataError("Podcast is missing an ID")
        podcast = Podcast(
            item_id=resolved_id,
            provider=self.instance_id,
            name=podcast_obj.get("title") or "Unknown Podcast",
            provider_mappings={
                ProviderMapping(
                    item_id=resolved_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/playlist?list={resolved_id}",
                )
            },
        )
        if description := _description_text(podcast_obj.get("description")):
            podcast.metadata.description = description
        author = podcast_obj.get("author") or podcast_obj.get("channel")
        if isinstance(author, dict):
            podcast.publisher = author.get("name")
        elif isinstance(author, str):
            podcast.publisher = author
        if podcast_obj.get("thumbnails"):
            podcast.metadata.images = self._parse_thumbnails(podcast_obj["thumbnails"])
        if isinstance(episodes := podcast_obj.get("episodes"), list):
            podcast.total_episodes = len(episodes)
        return podcast

    def _parse_podcast_episode(
        self, episode_obj: dict, podcast: Podcast, position: int = 0
    ) -> PodcastEpisode:
        """Parse a YTM episode dict into a PodcastEpisode model object."""
        video_id = episode_obj.get("videoId")
        if not video_id:
            raise InvalidDataError("Podcast episode is missing videoId")
        item_id = _episode_item_id(podcast.item_id, video_id)
        episode = PodcastEpisode(
            item_id=item_id,
            provider=self.instance_id,
            name=episode_obj.get("title") or "Unknown Episode",
            position=position,
            podcast=podcast,
            provider_mappings={
                ProviderMapping(
                    item_id=item_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=f"{YTM_DOMAIN}/watch?v={video_id}",
                    audio_format=self._catalog_audio_format(),
                )
            },
        )
        if (duration := _parse_duration_words(episode_obj.get("duration"))) is not None:
            episode.duration = duration
        if description := _description_text(episode_obj.get("description")):
            episode.metadata.description = description
        if episode_obj.get("thumbnails"):
            episode.metadata.images = self._parse_thumbnails(episode_obj["thumbnails"])
        # Deliberately no release_date. The "date" key on an anonymous response
        # holds a view count ("591K views"), not a date, on every episode of
        # every show checked. Parsing it would either raise or, worse, land a
        # nonsense date on the item.
        #
        # fully_played and resume_position_ms are also left unset: None tells
        # Music Assistant to use its own resume point, which is better than us
        # inventing one from a field YouTube does not give us anonymously.
        return episode

    def _parse_thumbnails(
        self, thumbnails_obj: list[dict], *, keep_small_frame: bool = False
    ) -> list[MediaItemImage]:
        """Convert YTM thumbnail list to MediaItemImage list.

        ``keep_small_frame`` is for tracks only: a playlist without a THUMB gets
        a generated collage from Music Assistant, which beats a 400 px frame.
        """
        result: list[MediaItemImage] = []
        processed = set()
        # Widest video frame the size filter below drops (sorted widest first).
        small_frame: str | None = None
        for img in sorted(thumbnails_obj, key=lambda w: w.get("width", 0), reverse=True):
            url: str = img.get("url", "")
            if not url:
                continue
            url_base = url.split("=w")[0]
            width: int = img.get("width", 0)
            height: int = img.get("height", 1)
            ratio: float = width / height if height else 1.0
            image_type = (
                ImageType.LANDSCAPE
                if "maxresdefault" in url or ratio > 2.0
                else ImageType.THUMB
            )
            if "=w" not in url and width < 500:
                if small_frame is None and width and "ytimg.com/vi" in url:
                    small_frame = url
                continue
            if "=w" in url and width < 600:
                url = f"{url_base}=w600-h600-p"
                image_type = ImageType.THUMB
            if (url_base, image_type) in processed:
                continue
            processed.add((url_base, image_type))
            result.append(
                MediaItemImage(
                    type=image_type,
                    path=url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            )
        # Music Assistant only takes THUMB images for queue items and player
        # artwork, and sends its logo to Cast and every other player without
        # one. Video uploads can end up with none: a 16:9 maxresdefault is typed
        # LANDSCAPE, and search and playlist rows carry a single 400x225 frame
        # the filter above drops. Fall back to the widest kept image, or for a
        # track to the widest dropped video frame (issue #90).
        if not any(img.type == ImageType.THUMB for img in result):
            fallback = result[0].path if result else None
            if fallback is None and keep_small_frame:
                fallback = small_frame
            if fallback and "=w" in fallback:
                # Size-param URLs crop to a square server-side, as above.
                fallback = f"{fallback.split('=w')[0]}=w600-h600-p"
            if fallback:
                result.append(
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=fallback,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                )
        return result

    def _get_item_mapping(
        self, media_type: MediaType, key: str, name: str, year: int | None = None
    ) -> ItemMapping:
        mapping = ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.instance_id,
            name=name,
        )
        if year is not None:
            # Upstream ItemMapping carries a year; this provider never set one,
            # so an album reached through a track had no release year at all.
            # Issue #53.
            mapping.year = year
        return mapping

    async def _attach_album_years(self, tracks: list[Track]) -> None:
        """Fill in the release year on each track's album, in place.

        A track parsed from a playlist or a search result carries only
        ``{"id", "name"}`` for its album, so the year has to come from an album
        lookup. That is only affordable because ``get_album`` is cached for a
        month: in steady state this costs nothing, and a cold cache costs one
        request per distinct album rather than one per track.

        Bounded on both axes. Concurrency is capped so a cold cache does not
        arrive as a burst, and the number of distinct albums is capped so one
        pathological playlist cannot become hundreds of requests. Past the cap
        the remaining tracks simply keep a year of None, which is what they had
        before this existed.

        Never raises: a year is a nice-to-have, and failing a playlist load over
        one is a bad trade. Requested in issue #53.
        """
        wanted: dict[str, list[ItemMapping]] = {}
        for track in tracks:
            album = getattr(track, "album", None)
            album_id = getattr(album, "item_id", None)
            if album_id and getattr(album, "year", None) is None:
                wanted.setdefault(album_id, []).append(album)
        if not wanted:
            return

        album_ids = list(wanted)[:ALBUM_YEAR_LOOKUP_LIMIT]
        if len(wanted) > ALBUM_YEAR_LOOKUP_LIMIT:
            self.logger.debug(
                "album-year lookup capped at %d of %d distinct albums",
                ALBUM_YEAR_LOOKUP_LIMIT,
                len(wanted),
            )
        semaphore = asyncio.Semaphore(ALBUM_YEAR_LOOKUP_CONCURRENCY)

        async def _fill(album_id: str) -> None:
            async with semaphore:
                try:
                    album = await self.get_album(album_id)
                except Exception as err:  # noqa: BLE001 - a missing year is not an error
                    self.logger.debug("album-year lookup failed for %s: %s", album_id, err)
                    return
            if album.year:
                for mapping in wanted[album_id]:
                    mapping.year = album.year

        await asyncio.gather(*(_fill(album_id) for album_id in album_ids))

    def _get_artist_item_mapping(self, artist_obj: dict) -> ItemMapping:
        # search results (filter="artists") key the id as "browseId" and the
        # display name as "artist" instead of "channelId"/"name".
        artist_id = (
            artist_obj.get("id")
            or artist_obj.get("channelId")
            or artist_obj.get("browseId")
        )
        if not artist_id and (
            artist_obj.get("name") == "Various Artists"
            or artist_obj.get("artist") == "Various Artists"
        ):
            artist_id = VARIOUS_ARTISTS_YTM_ID
        return self._get_item_mapping(
            MediaType.ARTIST,
            artist_id or "",
            # Unconditional final fallback: a present-but-empty "artist" value
            # must not leak through (dict.get default only fires when absent).
            artist_obj.get("name") or artist_obj.get("artist") or "Unknown",
        )

    async def _install_packages(self) -> None:
        """Install required packages, upgrading yt-dlp past the android_vr cutoff.

        The yt-dlp requirement carries the MIN_YTDLP_REQUIREMENT floor rather
        than a bare name, and that difference is what actually delivers the
        2026-08 fix to existing installs: Music Assistant skips manifest
        requirements without ``==``, and pip never touches an
        already-satisfied bare requirement, so a floor here at setup is the
        only hook that runs on every provider load.

        An install failure degrades to a warning while something importable
        exists: a stale yt-dlp plays nothing but browses fine, and the stream
        path warns about staleness itself, whereas refusing to load would
        take browsing down with it (and the import checks below still stop a
        genuinely absent package).
        """
        for pkg in (MIN_YTDLP_REQUIREMENT, "ytmusicapi"):
            try:
                await install_package(pkg)
            except Exception as err:  # noqa: BLE001 - degrade to what's installed
                self.logger.warning(
                    "could not install or upgrade %s: %s. Continuing with "
                    "whatever is already installed.",
                    pkg,
                    err,
                )
        try:
            await asyncio.to_thread(importlib.import_module, "yt_dlp")
        except ImportError as err:
            raise SetupFailedError("yt-dlp failed to install") from err
        try:
            await asyncio.to_thread(importlib.import_module, "ytmusicapi")
        except ImportError as err:
            raise SetupFailedError("ytmusicapi failed to install") from err
