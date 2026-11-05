"""Synthetic sessions only: extraction must use the selected account safely."""

import asyncio
from http.cookiejar import CookieJar
from types import SimpleNamespace
from urllib.request import Request
from unittest.mock import Mock

import pytest
import yt_dlp

from music_assistant_models.errors import UnplayableMediaError
from ytmusic_free.playback_cookies import load_playback_cookies


def test_cookie_scope_and_values():
    ydl = SimpleNamespace(cookiejar=CookieJar())
    load_playback_cookies(ydl, "__Secure-3PAPISID=synthetic; SID=another=value")
    cookies = {cookie.name: cookie for cookie in ydl.cookiejar}
    assert cookies["SID"].value == "another=value"
    assert all(c.secure and c.discard and c.expires is None for c in cookies.values())
    allowed = Request("https://music.youtube.com/watch")
    ydl.cookiejar.add_cookie_header(allowed)
    assert allowed.get_header("Cookie")
    for url in ("http://youtube.com/", "https://youtube.com.example.invalid/",
                "https://media.googlevideo.com/"):
        request = Request(url)
        ydl.cookiejar.add_cookie_header(request)
        assert request.get_header("Cookie") is None


@pytest.mark.parametrize("header", ["SID=secret", "Cookie: secret", "__Secure-3PAPISID=",
                                     "__Secure-3PAPISID=secret\nSID=other"])
def test_invalid_cookie_is_credential_safe(header):
    ydl = SimpleNamespace(cookiejar=CookieJar())
    with pytest.raises(ValueError) as exc:
        load_playback_cookies(ydl, header)
    assert "secret" not in str(exc.value)
    assert not list(ydl.cookiejar)


def test_extraction_reads_current_cookie_and_keeps_accounts_isolated(provider):
    observed = []

    class Extractor(yt_dlp.YoutubeDL):
        def extract_info(self, url, download=False):
            observed.append({c.name: c.value for c in self.cookiejar})
            assert "Cookie" not in self.params.get("http_headers", {})
            return {"formats": [{"format_id": "251", "url": "https://example.invalid/audio",
                                  "ext": "webm", "vcodec": "none", "acodec": "opus"}]}

    provider._yt_dlp_module = SimpleNamespace(YoutubeDL=Extractor, utils=yt_dlp.utils)
    provider._warned_stale_ytdlp = True
    values = {"auth_type": "cookie", "cookie_header": "__Secure-3PAPISID=first"}
    provider.config = Mock()
    provider.config.get_value.side_effect = values.get
    for cookie in ("first", "second"):
        values["cookie_header"] = f"__Secure-3PAPISID={cookie}"
        asyncio.run(provider._get_stream_format("synthetic01"))
    values["auth_type"] = "none"
    asyncio.run(provider._get_stream_format("synthetic01"))
    assert observed == [{"__Secure-3PAPISID": "first"}, {"__Secure-3PAPISID": "second"}, {}]


def test_empty_cookie_keeps_anonymous_behavior():
    ydl = SimpleNamespace(cookiejar=CookieJar())
    load_playback_cookies(ydl, "")
    assert len(ydl.cookiejar) == 0


def test_invalid_config_fails_before_network(provider):
    provider.config = Mock()
    provider.config.get_value.side_effect = {"auth_type": "cookie", "cookie_header": "SID=secret"}.get
    provider._yt_dlp_module = yt_dlp
    provider._warned_stale_ytdlp = True
    with pytest.raises(UnplayableMediaError, match="__Secure-3PAPISID"):
        asyncio.run(provider._get_stream_format("synthetic01"))
