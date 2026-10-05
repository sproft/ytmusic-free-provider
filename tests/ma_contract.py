"""The upstream Music Assistant facts this provider depends on, in one place.

Two suites assert against this one table:

* ``tests/python_integration/test_models_contract.py`` checks that the real
  ``music_assistant_models`` package still behaves this way.
* ``tests/python/test_stub_matches_contract.py`` checks that the hand-written
  stubs in ``tests/python/conftest.py`` behave the same way.

Pinning both ends to a single table is what closes the blind spot behind
issue #41. The unit suite runs entirely against stubs, so it can only ever be
as correct as they are: an earlier stub invented a ``ContentType.WEBM`` member,
and the whole suite passed green while real Music Assistant reported every
Opus stream as ``"?"``. Asserting the stub against the same table as the real
package makes that class of drift impossible in either direction.

This module is deliberately plain data with no imports. It is not collected as
a test (it does not match ``python_files``); both suites import it by path.
"""

from __future__ import annotations

# Container and codec strings the provider can hand to ``ContentType.try_parse``,
# mapped to the ContentType *member name* it has to come back as. Member names
# rather than members, so the table stays importable without either package.
TRY_PARSE_EXPECTATIONS: dict[str, str] = {
    # yt-dlp reports Opus streams in a WebM container. Upstream has no WEBM
    # member, which is the entire reason ``_get_stream_format`` falls back to
    # the codec when the container does not parse. If this ever stops being
    # UNKNOWN, that fallback can be simplified.
    "webm": "UNKNOWN",
    "opus": "OPUS",
    "m4a": "M4A",
    "mp4": "MP4",
    "vorbis": "VORBIS",
    # yt-dlp spells AAC as "mp4a.40.2" / "mp4a.40.5". Upstream strips the codec
    # profile suffix, so both land on MP4A.
    "mp4a.40.2": "MP4A",
    "mp4a.40.5": "MP4A",
    # yt-dlp uses the literal string "none" for a stream with no audio.
    "none": "UNKNOWN",
    "": "UNKNOWN",
}

# Members the provider references by name. Losing any of these upstream is a
# breaking change we want to hear about from CI, not from a user report.
REQUIRED_CONTENT_TYPE_MEMBERS: tuple[str, ...] = (
    "M4A",
    "MP4",
    "MP4A",
    "AAC",
    "OPUS",
    "VORBIS",
    "UNKNOWN",
)

# Absent upstream, and the codec fallback exists because of it.
FORBIDDEN_CONTENT_TYPE_MEMBERS: tuple[str, ...] = ("WEBM",)

# ``ContentType.UNKNOWN`` is "?" upstream, not "unknown". The provider compares
# against the member rather than the value, but a stub that gets this wrong is
# a signal the stub was written from memory instead of from the source.
UNKNOWN_VALUE = "?"

# ``AudioFormat`` fields the provider assigns. Upstream has more; these are the
# ones whose absence would break playback metadata.
REQUIRED_AUDIO_FORMAT_FIELDS: tuple[str, ...] = (
    "content_type",
    "sample_rate",
    "channels",
    "bit_rate",
)

# ``MediaType`` members the provider hands back or switches on. Podcast support
# (issue #52) is built entirely on the last two, and the unit suite runs against
# a stub, so pin them here before anything depends on them.
REQUIRED_MEDIA_TYPE_MEMBERS: tuple[str, ...] = (
    "ARTIST",
    "ALBUM",
    "TRACK",
    "PLAYLIST",
    "PODCAST",
    "PODCAST_EPISODE",
)

# ``ProviderFeature`` members the provider names. Unlike everything else in this
# table, these are consumed at *import* time: ``BASE_FEATURES`` and
# ``AUTHENTICATED_FEATURES`` are built from attribute access while the module
# loads, so a member renamed or dropped upstream is not one degraded feature, it
# is an AttributeError that stops ytmusic_free loading at all. Upstream has
# deleted a member with no deprecation window before (``AUDIO_OVERLAY``, added
# and gone again inside 63 days, on a pull request titled for something else),
# so this is a live risk rather than a theoretical one.
#
# Pinned by member *name* on purpose. The enum defines ``_missing_`` and answers
# UNKNOWN for anything it does not recognise, so a check written as a lookup by
# value would stay green forever after a deletion.
#
# This is the one tuple here that describes us rather than upstream, so it is
# also the one that can go stale on its own. ``tests/python/`` keeps it honest:
# it is the only suite that can import the provider, and it asserts this tuple
# against the two sets. See issue #65.
REQUIRED_PROVIDER_FEATURE_MEMBERS: tuple[str, ...] = (
    "SEARCH",
    "ARTIST_ALBUMS",
    "ARTIST_TOPTRACKS",
    "SIMILAR_TRACKS",
    "BROWSE",
    "LIBRARY_ARTISTS",
    "LIBRARY_ALBUMS",
    "LIBRARY_TRACKS",
    "LIBRARY_PLAYLISTS",
    "RECOMMENDATIONS",
    "LIBRARY_ARTISTS_EDIT",
    "LIBRARY_ALBUMS_EDIT",
    "LIBRARY_PLAYLISTS_EDIT",
    "LIBRARY_TRACKS_EDIT",
    "LIBRARY_PODCASTS",
)

# ``ItemMapping`` fields the provider sets. ``year`` is the one that matters:
# the album-year feature in issue #53 exists only because upstream carries it,
# and if it ever disappears the provider would be assigning to nothing.
REQUIRED_ITEM_MAPPING_FIELDS: tuple[str, ...] = (
    "media_type",
    "item_id",
    "provider",
    "name",
    "year",
)

# ``Podcast`` fields the provider sets.
REQUIRED_PODCAST_FIELDS: tuple[str, ...] = (
    "item_id",
    "provider",
    "name",
    "provider_mappings",
    "publisher",
    "metadata",
)

# ``PodcastEpisode`` fields the provider sets. ``position`` and ``podcast`` are
# mandatory upstream with no default, so a stub that gave them one would let a
# parser that forgets either of them pass here and fail in production.
REQUIRED_PODCAST_EPISODE_FIELDS: tuple[str, ...] = (
    "item_id",
    "provider",
    "name",
    "provider_mappings",
    "position",
    "podcast",
    "duration",
    "metadata",
)

# Resume state lives on the episode and is explicitly nullable upstream: the
# docstring says None lets Music Assistant fall back to its own resume point.
# The provider leaves both unset, because YouTube's anonymous responses carry
# no reliable playback position, and that is only safe while None keeps its
# meaning.
NULLABLE_PODCAST_EPISODE_FIELDS: tuple[str, ...] = (
    "fully_played",
    "resume_position_ms",
)

# ``StreamDetails`` fields the provider sets, either as constructor arguments
# or by assignment afterwards. ``data`` carries the stream url and headers over
# to ``get_audio_stream``; losing it upstream would sever playback entirely.
REQUIRED_STREAM_DETAILS_FIELDS: tuple[str, ...] = (
    "provider",
    "item_id",
    "audio_format",
    "stream_type",
    "path",
    "can_seek",
    "allow_seek",
    "expiration",
    "duration",
    "extra_input_args",
    "data",
)

# ``StreamType`` members the provider names. CUSTOM is the one playback stands
# on: googlevideo refuses the unbounded fetches ffmpeg makes (bounded-Range
# enforcement, 2026-08), so the provider streams the audio itself through
# ``get_audio_stream``, and that hook is only ever called for CUSTOM.
REQUIRED_STREAM_TYPE_MEMBERS: tuple[str, ...] = ("CUSTOM",)

# Names for "this url is not fetchable until T". Upstream has none of them, and
# that absence is load-bearing: it is why the provider sits out YouTube's
# pre-roll ad window itself, inside ``get_stream_details``, rather than passing
# the timestamp along (issue #51). Blocking the provider call is the worse of
# two options and only justified while there is nowhere to put the value. If
# any of these appears upstream, ``_preroll_wait_seconds`` should feed it
# instead of ``asyncio.sleep``.
FORBIDDEN_STREAM_DETAILS_FIELDS: tuple[str, ...] = (
    "available_at",
    "available_from",
    "not_before",
    "start_after",
)

# The auth notice (issue #92). While cookie auth is configured but not working,
# the provider writes a ``ProviderError`` as its last_error and signals
# ``PROVIDERS_UPDATED`` so open UIs refetch. ``translation_key`` has to exist and
# default to None: the provider leaves it unset, and a key there would make Music
# Assistant replace the notice with its generic "Login failed" text.
REQUIRED_EVENT_TYPE_MEMBERS: tuple[str, ...] = ("PROVIDERS_UPDATED",)
REQUIRED_PROVIDER_ERROR_FIELDS: tuple[str, ...] = (
    "error_code",
    "message",
    "translation_key",
)

# ``LoginFailed.error_code``. The server maps it to the "Authentication
# required" status, which is the badge the notice is meant to carry; another
# code would read "Setup failed" over a provider that is running.
LOGIN_FAILED_ERROR_CODE = 6

# ``SetupFailedError.error_code``. Used for a startup failure that does not point
# at the cookie (network, 5xx, parse error): the server shows it as ERROR, and
# on that status the provider's settings page offers a Reload button.
SETUP_FAILED_ERROR_CODE = 5
