"""Localized API requests and metadata, without contacting YouTube."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
import ytmusicapi

import ytmusic_free as ytm


def configure(provider, locale):
    values = {ytm.CONF_METADATA_LANGUAGE: locale}
    provider.config = SimpleNamespace(get_value=values.get)
    return values


def test_language_option_defaults_for_new_and_existing_instances(provider):
    entry = next(
        e for e in ytm._build_config_entries() if e.key == ytm.CONF_METADATA_LANGUAGE
    )
    assert entry.default_value == "ja-JP"
    assert {o.value for o in entry.options} == {"ja-JP", "en-US"}
    assert provider._metadata_locale == "ja-JP"
    configure(provider, None)
    assert provider._metadata_locale == "ja-JP"


@pytest.mark.parametrize(
    "locale,language,location", [("ja-JP", "ja", "JP"), ("en-US", "en", "US")]
)
@pytest.mark.parametrize("authenticated", [False, True])
def test_real_ytmusicapi_request_context_headers_and_auth(
    provider, monkeypatch, locale, language, location, authenticated
):
    configure(provider, locale)
    # Cookie auth initialization asks for a visitor ID; substitute it locally.
    monkeypatch.setattr(
        "ytmusicapi.ytmusic.get_visitor_id", lambda *a: {"X-Goog-Visitor-Id": "fixture"}
    )
    cookie = "__Secure-3PAPISID=fixture; SAPISID=fixture"
    auth = provider._build_auth_headers(cookie, auth_user=2) if authenticated else None
    client = provider._create_ytmusic_client(
        auth=auth, user="brand-fixture" if authenticated else None
    )
    assert isinstance(client, ytmusicapi.YTMusic)
    captured = {}

    def send(request, **kwargs):
        captured.update(request=request, timeout=kwargs.get("timeout"))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"ok": true}'
        return response

    monkeypatch.setattr(client._session, "send", send)
    try:
        assert client._send_request("browse", {"browseId": "fixture"}) == {"ok": True}
        import json

        context = json.loads(captured["request"].body)["context"]
        assert context["client"]["hl"] == language
        assert context["client"]["gl"] == location
        assert captured["request"].headers["Accept-Language"].startswith(locale)
        assert captured["timeout"] == 30
        if authenticated:
            assert context["user"]["onBehalfOfUser"] == "brand-fixture"
            assert captured["request"].headers["X-Goog-AuthUser"] == "2"
            assert "__Secure-3PAPISID=fixture" in captured["request"].headers["Cookie"]
            assert (
                captured["request"].headers["Authorization"].startswith("SAPISIDHASH ")
            )
        else:
            assert "Authorization" not in captured["request"].headers
    finally:
        client._session.close()


def test_invalid_locale_does_not_silently_fall_back(provider):
    configure(provider, "unknown")
    with pytest.raises(ytm.InvalidDataError, match="Unsupported metadata language"):
        provider._create_ytmusic_client()


def test_japanese_names_survive_all_metadata_parsers(provider):
    track = provider._parse_track(
        {
            "videoId": "song",
            "title": "夜に駆ける",
            "artists": [{"name": "ヨアソビ", "id": "artist"}],
        }
    )
    album = provider._parse_album(
        {"title": "ザ・ブック", "artists": [{"name": "ヨアソビ", "id": "artist"}]},
        "album",
    )
    artist = provider._parse_artist({"name": "ヨアソビ", "browseId": "artist"})
    playlist = provider._parse_playlist({"id": "playlist", "title": "日本のヒット曲"})
    assert track.name == "夜に駆ける"
    assert track.artists[0].name == "ヨアソビ"
    assert album.name == "ザ・ブック"
    assert artist.name == "ヨアソビ"
    assert playlist.name == "日本のヒット曲"


@pytest.mark.parametrize("locale,language", [("ja-JP", "ja"), ("en-US", "en")])
def test_playlist_fallback_requests_selected_language(provider, locale, language):
    configure(provider, locale)
    options = []

    class Extractor:
        def __init__(self, opts):
            options.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            assert download is False
            return {"title": "日本のヒット曲", "entries": []}

    provider._yt_dlp_module = SimpleNamespace(YoutubeDL=Extractor)
    playlist = asyncio.run(provider._get_playlist_via_ytdlp("playlist"))
    assert playlist.name == "日本のヒット曲"
    assert asyncio.run(provider._get_playlist_tracks_via_ytdlp("playlist")) == []
    assert len(options) == 2
    for opts in options:
        assert opts["extractor_args"]["youtube"]["lang"] == [language]
        assert opts["http_headers"]["Accept-Language"].startswith(locale)
        assert "cookiefile" not in opts


def test_search_and_authenticated_library_preserve_japanese(provider):
    provider._authenticated = True
    provider._library_seen_nonempty = {}
    provider._ytmusic = MagicMock()
    song = {
        "resultType": "song",
        "videoId": "song",
        "title": "夜に駆ける",
        "artists": [{"name": "ヨアソビ", "id": "artist"}],
    }
    provider._ytmusic.search.side_effect = (
        lambda query, filter, limit: [song] if filter == "songs" else []
    )
    provider._ytmusic.get_library_songs.return_value = [song]

    async def run():
        results = await provider.search("ヨアソビ", [ytm.MediaType.TRACK])
        library = [track async for track in provider.get_library_tracks()]
        return results, library

    results, library = asyncio.run(run())
    assert results.tracks[0].name == library[0].name == "夜に駆ける"
    assert results.tracks[0].item_id == library[0].item_id == "song"


def test_metadata_cache_calls_include_locale(provider, monkeypatch):
    # Capture the key passed to MA's cache wrapper rather than just an attribute.
    captured = []

    def cache(expiration, **options):
        def decorate(func):
            async def wrapper(self, *args, **kwargs):
                captured.append(kwargs["_metadata_locale"])
                return await func(self, *args, **kwargs)

            return wrapper

        return decorate

    monkeypatch.setattr(ytm, "use_cache", cache)

    @ytm.use_metadata_cache(60)
    async def probe(self, item_id):
        return item_id

    values = configure(provider, "ja-JP")
    assert asyncio.run(probe(provider, "same-id")) == "same-id"
    values[ytm.CONF_METADATA_LANGUAGE] = "en-US"
    assert asyncio.run(probe(provider, "same-id")) == "same-id"
    assert captured == ["ja-JP", "en-US"]
