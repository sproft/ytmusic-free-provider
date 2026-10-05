"""Pytest setup that stubs Music Assistant runtime imports.

The provider depends on `music_assistant_models`, `music_assistant.helpers.util`,
and `music_assistant.models.music_provider`. Those packages are not on PyPI
under those names, and the full `music-assistant` runtime is far too heavy to
install in CI. Instead, we register lightweight stand-ins on `sys.modules`
before the provider module is imported, so unit tests exercise our code
against deterministic stubs.

The stubs only need to satisfy the surface the provider actually touches —
attribute access, simple kwargs construction, enum membership. They do not
attempt to mimic real Music Assistant behavior.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _new_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_music_assistant_models() -> None:
    pkg = _new_module("music_assistant_models")

    config_entries = _new_module("music_assistant_models.config_entries")

    class _ConfigEntryType(str, Enum):
        STRING = "string"
        SECURE_STRING = "secure_string"
        BOOLEAN = "boolean"
        INTEGER = "integer"

    @dataclass
    class _ConfigValueOption:
        title: str
        value: str

    @dataclass
    class _ConfigEntry:
        key: str
        type: _ConfigEntryType
        label: str
        default_value: object = None
        required: bool = False
        options: tuple = ()
        depends_on: str | None = None
        depends_on_value: list | None = None
        description: str | None = None

    @dataclass
    class _ProviderError:
        # Mirrors the upstream field order. translation_key must default to
        # None: the provider leaves it unset so its own message is shown.
        error_code: int
        message: str
        translation_key: str | None = None
        translation_args: list = field(default_factory=list)
        translation_owner: str | None = None

    config_entries.ConfigEntry = _ConfigEntry
    config_entries.ConfigValueOption = _ConfigValueOption
    config_entries.ConfigValueType = object
    config_entries.ProviderConfig = object
    config_entries.ProviderError = _ProviderError

    enums = _new_module("music_assistant_models.enums")

    class _AlbumType(str, Enum):
        ALBUM = "album"
        SINGLE = "single"
        EP = "ep"
        SOUNDTRACK = "soundtrack"
        LIVE = "live"
        UNKNOWN = "unknown"

    class _ContentType(str, Enum):
        # Mirrors music_assistant_models.enums.ContentType. Keep it faithful:
        # there is deliberately no WEBM member, because upstream has none, and
        # UNKNOWN is "?" not "unknown". An earlier stub invented ContentType.WEBM,
        # which made the suite pass while real Music Assistant reported every Opus
        # stream as "?". A test double that is kinder than reality tests nothing.
        M4A = "m4a"
        MP4 = "mp4"
        MP4A = "mp4a"
        AAC = "aac"
        MP3 = "mp3"
        OGG = "ogg"
        OPUS = "opus"
        VORBIS = "vorbis"
        FLAC = "flac"
        UNKNOWN = "?"

        @classmethod
        def try_parse(cls, value: str) -> "_ContentType":
            if not value:
                return cls.UNKNOWN
            # Upstream strips codec profile suffixes, so "mp4a.40.2" -> MP4A.
            candidate = str(value).lower().split(".")[0].split(";")[0].strip()
            try:
                return cls(candidate)
            except ValueError:
                return cls.UNKNOWN

    class _ImageType(str, Enum):
        THUMB = "thumb"
        LANDSCAPE = "landscape"

    class _MediaType(str, Enum):
        ARTIST = "artist"
        ALBUM = "album"
        TRACK = "track"
        PLAYLIST = "playlist"
        PODCAST = "podcast"
        PODCAST_EPISODE = "podcast_episode"

    class _ProviderFeature(str, Enum):
        SEARCH = "search"
        ARTIST_ALBUMS = "artist_albums"
        ARTIST_TOPTRACKS = "artist_toptracks"
        SIMILAR_TRACKS = "similar_tracks"
        BROWSE = "browse"
        LIBRARY_ARTISTS = "library_artists"
        LIBRARY_ALBUMS = "library_albums"
        LIBRARY_TRACKS = "library_tracks"
        LIBRARY_PLAYLISTS = "library_playlists"
        LIBRARY_PODCASTS = "library_podcasts"
        RECOMMENDATIONS = "recommendations"
        LIBRARY_ARTISTS_EDIT = "library_artists_edit"
        LIBRARY_ALBUMS_EDIT = "library_albums_edit"
        LIBRARY_PLAYLISTS_EDIT = "library_playlists_edit"
        LIBRARY_TRACKS_EDIT = "library_tracks_edit"

    class _StreamType(str, Enum):
        HTTP = "http"
        CUSTOM = "custom"

    class _EventType(str, Enum):
        PROVIDERS_UPDATED = "providers_updated"

    enums.AlbumType = _AlbumType
    enums.ConfigEntryType = _ConfigEntryType
    enums.ContentType = _ContentType
    enums.EventType = _EventType
    enums.ImageType = _ImageType
    enums.MediaType = _MediaType
    enums.ProviderFeature = _ProviderFeature
    enums.StreamType = _StreamType

    errors = _new_module("music_assistant_models.errors")

    class _MAError(Exception):
        error_code = 0

    class _InvalidDataError(_MAError):
        pass

    class _LoginFailed(_MAError):
        # Upstream value, pinned in tests/ma_contract.py: Music Assistant maps
        # it to the "Authentication required" status.
        error_code = 6

    class _MediaNotFoundError(_MAError):
        pass

    class _SetupFailedError(_MAError):
        # Upstream value, pinned in tests/ma_contract.py: the startup notice
        # that does not blame the cookie is written with it.
        error_code = 5

    class _UnplayableMediaError(_MAError):
        pass

    errors.InvalidDataError = _InvalidDataError
    errors.LoginFailed = _LoginFailed
    errors.MediaNotFoundError = _MediaNotFoundError
    errors.SetupFailedError = _SetupFailedError
    errors.UnplayableMediaError = _UnplayableMediaError

    media_items = _new_module("music_assistant_models.media_items")

    class _UniqueList(list):
        def __init__(self, iterable=()):
            super().__init__()
            for item in iterable:
                if item not in self:
                    self.append(item)

    @dataclass
    class _ProviderMapping:
        item_id: str
        provider_domain: str
        provider_instance: str
        url: str | None = None
        available: bool = True
        audio_format: object = None

        def __hash__(self):
            return hash((self.item_id, self.provider_instance))

        def __eq__(self, other):
            return (
                isinstance(other, _ProviderMapping)
                and self.item_id == other.item_id
                and self.provider_instance == other.provider_instance
            )

    @dataclass
    class _ItemMapping:
        media_type: object
        item_id: str
        provider: str
        name: str = ""
        # Upstream carries this and the provider now fills it for albums
        # reached through a track (issue #53). Without it here the stub would be
        # missing a field reality has, and a test could not tell a year that was
        # set from one that silently went nowhere.
        year: int | None = None

        def __hash__(self):
            return hash((self.media_type, self.item_id, self.provider))

        def __eq__(self, other):
            return (
                isinstance(other, _ItemMapping)
                and self.media_type == other.media_type
                and self.item_id == other.item_id
                and self.provider == other.provider
            )

    @dataclass
    class _MediaItemImage:
        type: object
        path: str
        provider: str
        remotely_accessible: bool = True

        def __hash__(self):
            return hash((self.type, self.path, self.provider))

        def __eq__(self, other):
            return (
                isinstance(other, _MediaItemImage)
                and self.type == other.type
                and self.path == other.path
                and self.provider == other.provider
            )

    @dataclass
    class _AudioFormat:
        content_type: object | None = None
        codec_type: object | None = None
        channels: int | None = None
        sample_rate: int | None = None
        bit_rate: int | None = None

    @dataclass
    class _MetaData:
        images: list = field(default_factory=list)
        description: str = ""
        explicit: bool = False

    @dataclass
    class _BaseMediaItem:
        item_id: str = ""
        provider: str = ""
        name: str = ""
        version: str = ""
        provider_mappings: set = field(default_factory=set)
        metadata: _MetaData = field(default_factory=_MetaData)

    @dataclass
    class _Track(_BaseMediaItem):
        artists: list = field(default_factory=list)
        album: object | None = None
        disc_number: int = 0
        track_number: int = 0
        position: int = 0
        duration: int = 0

    @dataclass
    class _Album(_BaseMediaItem):
        artists: list = field(default_factory=list)
        year: int = 0
        album_type: object = None

    @dataclass
    class _Artist(_BaseMediaItem):
        pass

    @dataclass
    class _Playlist(_BaseMediaItem):
        owner: str = ""
        is_editable: bool = False

    @dataclass
    class _Podcast(_BaseMediaItem):
        publisher: str | None = None
        total_episodes: int | None = None

    @dataclass
    class _PodcastEpisode(_BaseMediaItem):
        # Matches the released upstream, where both report a None default even
        # though the source declares them without one. The provider always sets
        # both explicitly, and the unit suite asserts that, so the stub does not
        # need to be stricter than the package users run.
        position: int = None  # type: ignore[assignment]
        podcast: object = None
        duration: int = 0
        fully_played: bool | None = None
        resume_position_ms: int | None = None

    @dataclass
    class _RecommendationFolder:
        item_id: str
        provider: str
        name: str
        items: list = field(default_factory=list)

    @dataclass
    class _SearchResults:
        artists: list = field(default_factory=list)
        albums: list = field(default_factory=list)
        tracks: list = field(default_factory=list)
        playlists: list = field(default_factory=list)
        podcasts: list = field(default_factory=list)

    media_items.Album = _Album
    media_items.Artist = _Artist
    media_items.AudioFormat = _AudioFormat
    media_items.ItemMapping = _ItemMapping
    media_items.MediaItemImage = _MediaItemImage
    media_items.MediaItemType = object
    media_items.MediaType = enums.MediaType
    media_items.Playlist = _Playlist
    media_items.Podcast = _Podcast
    media_items.PodcastEpisode = _PodcastEpisode
    media_items.ProviderMapping = _ProviderMapping
    media_items.RecommendationFolder = _RecommendationFolder
    media_items.SearchResults = _SearchResults
    media_items.Track = _Track
    media_items.UniqueList = _UniqueList

    streamdetails = _new_module("music_assistant_models.streamdetails")

    @dataclass
    class _StreamDetails:
        provider: str = ""
        item_id: str = ""
        audio_format: _AudioFormat = field(default_factory=_AudioFormat)
        stream_type: object = None
        path: str = ""
        can_seek: bool = True
        allow_seek: bool = True
        expiration: int = 0
        duration: int | None = None
        extra_input_args: list = field(default_factory=list)
        # Free-form provider payload upstream; get_audio_stream reads the
        # stream url and headers from it.
        data: object = None

    streamdetails.StreamDetails = _StreamDetails

    provider_mod = _new_module("music_assistant_models.provider")
    provider_mod.ProviderManifest = object

    pkg.config_entries = config_entries
    pkg.enums = enums
    pkg.errors = errors
    pkg.media_items = media_items
    pkg.streamdetails = streamdetails
    pkg.provider = provider_mod


def _install_music_assistant() -> None:
    pkg = _new_module("music_assistant")
    pkg.MusicAssistant = object

    helpers = _new_module("music_assistant.helpers")
    util = _new_module("music_assistant.helpers.util")

    enums = sys.modules["music_assistant_models.enums"]

    def _infer_album_type(name: str, version: str) -> object:
        text = f"{name} {version or ''}".lower()
        if "live" in text:
            return enums.AlbumType.LIVE
        if "soundtrack" in text or "ost" in text:
            return enums.AlbumType.SOUNDTRACK
        return enums.AlbumType.UNKNOWN

    async def _install_package(pkg_name: str) -> None:
        return None

    def _parse_title_and_version(title: str) -> tuple[str, str]:
        return (title or "", "")

    util.infer_album_type = _infer_album_type
    util.install_package = _install_package
    util.parse_title_and_version = _parse_title_and_version
    helpers.util = util
    pkg.helpers = helpers

    models = _new_module("music_assistant.models")
    music_provider_mod = _new_module("music_assistant.models.music_provider")

    class _MusicProvider:
        """Minimal MusicProvider stand-in."""

        instance_id: str = "test_instance"
        domain: str = "ytmusic_free"
        name: str = "YouTube Music (Free)"

        def __init__(self, mass=None, manifest=None, config=None, supported_features=None):
            import asyncio
            import logging

            self.mass = mass
            self.manifest = manifest
            self.config = config
            self.supported_features = supported_features or set()
            self.logger = logging.getLogger("ytmusic_free_test")
            # Set by Music Assistant once loaded_in_mass has returned. See
            # verify_server_contract.
            self.initialized = asyncio.Event()

        async def loaded_in_mass(self) -> None:
            return None

        async def unload(self, is_removed: bool = False) -> None:
            return None

    music_provider_mod.MusicProvider = _MusicProvider
    models.music_provider = music_provider_mod
    pkg.models = models

    controllers = _new_module("music_assistant.controllers")
    cache_mod = _new_module("music_assistant.controllers.cache")

    def _use_cache(expiration=600, **kwargs):
        """Pass-through stand-in for Music Assistant's ``use_cache``.

        Deliberately does no caching. The real decorator needs ``self.mass.cache``,
        which the unit suite does not have, and every other test here wants the
        undecorated behaviour anyway. What it does do is record the arguments on
        the wrapped function, so a test can assert the decorator is applied with
        the intended lifetime rather than merely that the code still runs.
        """
        import functools

        def _decorator(func):
            @functools.wraps(func)
            async def _wrapper(*args, **fn_kwargs):
                return await func(*args, **fn_kwargs)

            _wrapper.__ma_cache__ = {"expiration": expiration, **kwargs}
            return _wrapper

        return _decorator

    cache_mod.use_cache = _use_cache
    controllers.cache = cache_mod
    pkg.controllers = controllers


_install_music_assistant_models()
_install_music_assistant()


# Ensure the repo root is on sys.path so `import ytmusic_free` works.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def provider():
    """Return a YoutubeMusicFreeProvider instance with stubbed dependencies.

    The provider's __init__ goes through the stubbed MusicProvider base and
    skips the real async setup. Tests poke at parser/helper methods directly.
    """
    from ytmusic_free import YoutubeMusicFreeProvider

    instance = YoutubeMusicFreeProvider(mass=None, manifest=None, config=None)
    instance._ytmusic = None
    instance._yt_dlp_module = None
    instance._prefer_quality = True
    instance._authenticated = False
    return instance
