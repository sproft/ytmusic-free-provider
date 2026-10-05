"""Unit tests for the YouTube Music (Free) provider."""

from __future__ import annotations

import asyncio
import json
import time
import types
from unittest.mock import MagicMock

import pytest


# Imports below are resolved against the stubs registered in conftest.py.
from music_assistant_models.enums import (
    AlbumType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    UnplayableMediaError,
)

import ytmusic_free as ytm


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_module_constants_present():
    assert ytm.YTM_DOMAIN == "https://music.youtube.com"
    assert ytm.VARIOUS_ARTISTS_YTM_ID == "UCUTXlgdcKU5vfzFqHOWIvkA"
    assert ytm.DEFAULT_STREAM_URL_EXPIRATION == 3600
    # A cap, not a target. Has to clear a realistic multi-ad pod (the mid
    # thirties) without letting a bogus timestamp present as a hung player.
    assert 40 <= ytm.MAX_PREROLL_WAIT <= 90
    assert ytm.MIN_YTDLP_VERSION_FOR_PREROLL == (2025, 12, 8)


def test_base_features_are_anonymous_safe():
    assert ProviderFeature.SEARCH in ytm.BASE_FEATURES
    assert ProviderFeature.BROWSE in ytm.BASE_FEATURES
    assert ProviderFeature.ARTIST_ALBUMS in ytm.BASE_FEATURES
    assert ProviderFeature.ARTIST_TOPTRACKS in ytm.BASE_FEATURES
    assert ProviderFeature.SIMILAR_TRACKS in ytm.BASE_FEATURES


def test_authenticated_features_separate_from_base():
    overlap = ytm.BASE_FEATURES & ytm.AUTHENTICATED_FEATURES
    assert overlap == set(), "library/auth features must not double-up with base set"
    assert ProviderFeature.LIBRARY_TRACKS in ytm.AUTHENTICATED_FEATURES
    assert ProviderFeature.RECOMMENDATIONS in ytm.AUTHENTICATED_FEATURES


def test_auth_constants():
    assert ytm.AUTH_TYPE_NONE == "none"
    assert ytm.AUTH_TYPE_COOKIE == "cookie"
    assert ytm.CONF_AUTH_TYPE == "auth_type"
    assert ytm.CONF_COOKIE == "cookie_header"


# ---------------------------------------------------------------------------
# _yt_playlist_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("playlist_id", "expected"),
    [
        ("VLPLxxx123", "https://www.youtube.com/playlist?list=PLxxx123"),
        ("PLxxx123", "https://www.youtube.com/playlist?list=PLxxx123"),
        ("OLAK5uy_abc", "https://www.youtube.com/playlist?list=OLAK5uy_abc"),
        ("VLOLAK5uy_abc", "https://www.youtube.com/playlist?list=OLAK5uy_abc"),
    ],
)
def test_yt_playlist_url_strips_vl_prefix(playlist_id, expected):
    assert ytm.YoutubeMusicFreeProvider._yt_playlist_url(playlist_id) == expected


# ---------------------------------------------------------------------------
# Cookie / auth header building
# ---------------------------------------------------------------------------


def _forbid_open(monkeypatch):
    """Fail the test if anything tries to open a file.

    Auth headers are built in memory so several provider instances can hold
    different credentials at once (issue #40). Writing them to disk is the
    exact regression these tests guard against.
    """
    monkeypatch.setattr(
        "builtins.open",
        lambda *a, **kw: pytest.fail("auth headers must not be written to disk"),
    )


def test_build_auth_headers_rejects_cookie_without_secure_3papisid(provider, monkeypatch):
    _forbid_open(monkeypatch)
    with pytest.raises(ValueError, match="__Secure-3PAPISID"):
        provider._build_auth_headers("SID=abc; HSID=def")


def test_build_auth_headers_rejects_cookie_with_no_extractable_sapisid(provider, monkeypatch):
    # __Secure-3PAPISID present in the string but only as a substring,
    # never as its own `name=value` pair.
    _forbid_open(monkeypatch)
    with pytest.raises(ValueError, match="SAPISID"):
        provider._build_auth_headers("note=__Secure-3PAPISID-mention; SID=abc")


def test_build_auth_headers_extracts_sapisid_when_present(provider, monkeypatch):
    _forbid_open(monkeypatch)
    cookie = "SAPISID=mySapisid; __Secure-3PAPISID=otherValue; SID=foo"
    headers = provider._build_auth_headers(cookie)

    assert isinstance(headers, dict)
    assert headers["cookie"] == cookie
    assert headers["origin"] == ytm.YTM_DOMAIN
    assert headers["x-origin"] == ytm.YTM_DOMAIN
    # Authorization is SAPISIDHASH <ts>_<sha1(<ts> <sapisid> <origin>)>
    assert headers["authorization"].startswith("SAPISIDHASH ")
    ts_str, hash_str = headers["authorization"][len("SAPISIDHASH "):].split("_")
    assert ts_str.isdigit()
    assert int(ts_str) <= int(time.time()) + 5
    assert len(hash_str) == 40  # sha1 hex digest


def test_build_auth_headers_falls_back_to_secure_3papisid_when_sapisid_missing(
    provider, monkeypatch
):
    _forbid_open(monkeypatch)
    cookie = "__Secure-3PAPISID=fallbackValue; SID=foo"
    headers = provider._build_auth_headers(cookie)
    # The hash uses the extracted SAPISID â€” we can't see the secret, but we can
    # confirm the same input produces a stable-shape header.
    assert headers["authorization"].startswith("SAPISIDHASH ")


def test_build_auth_headers_json_serializable(provider, monkeypatch):
    """ytmusicapi copies the dict into a CaseInsensitiveDict; keep it plain."""
    _forbid_open(monkeypatch)
    headers = provider._build_auth_headers("__Secure-3PAPISID=a; SAPISID=b")
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
    json.loads(json.dumps(headers))


def test_build_auth_headers_satisfies_ytmusicapi_browser_contract(provider, monkeypatch):
    """The three keys ytmusicapi actually reads at construction time.

    `determine_auth_type` classifies the session as BROWSER only when an
    `authorization` header contains "SAPISIDHASH"; `sapisid_from_cookie` needs
    `__Secure-3PAPISID` in `cookie`; and one of `origin` / `x-origin` is read
    for the per-request hash. Drop any of them and auth degrades silently.
    """
    _forbid_open(monkeypatch)
    headers = provider._build_auth_headers("__Secure-3PAPISID=a; SAPISID=b")
    assert "SAPISIDHASH" in headers["authorization"]
    assert "__Secure-3PAPISID" in headers["cookie"]
    assert headers.get("origin") or headers.get("x-origin")


# ---------------------------------------------------------------------------
# Multi-instance isolation (issue #40)
# ---------------------------------------------------------------------------


def _make_provider(instance_id):
    """Build a second provider instance the way the `provider` fixture does."""
    instance = ytm.YoutubeMusicFreeProvider(mass=None, manifest=None, config=None)
    instance.instance_id = instance_id
    instance._ytmusic = None
    instance._authenticated = False
    return instance


def test_two_instances_build_independent_auth_headers(monkeypatch):
    """Two accounts must never end up sharing a credentials object."""
    _forbid_open(monkeypatch)
    alice = _make_provider("inst_alice")
    bob = _make_provider("inst_bob")

    alice_cookie = "__Secure-3PAPISID=alice; SAPISID=alice2"
    bob_cookie = "__Secure-3PAPISID=bob; SAPISID=bob2"
    alice_headers = alice._build_auth_headers(alice_cookie)
    bob_headers = bob._build_auth_headers(bob_cookie)

    assert alice_headers is not bob_headers
    assert alice_headers["cookie"] == alice_cookie
    assert bob_headers["cookie"] == bob_cookie
    # Mutating one must not reach the other.
    alice_headers["cookie"] = "tampered"
    assert bob_headers["cookie"] == bob_cookie


def test_build_auth_headers_defaults_to_account_index_zero(provider, monkeypatch):
    _forbid_open(monkeypatch)
    headers = provider._build_auth_headers("__Secure-3PAPISID=a; SAPISID=b")
    assert headers["x-goog-authuser"] == "0"


def test_build_auth_headers_honors_the_account_index(provider, monkeypatch):
    """A browser signed in to several Google accounts sends one shared cookie.

    X-Goog-AuthUser is the only thing that says which of those accounts a
    request resolves to, so two instances must be able to differ here.
    """
    _forbid_open(monkeypatch)
    headers = provider._build_auth_headers("__Secure-3PAPISID=a; SAPISID=b", 2)
    assert headers["x-goog-authuser"] == "2"


def test_two_instances_can_target_different_accounts_of_one_cookie(monkeypatch):
    _forbid_open(monkeypatch)
    shared_cookie = "__Secure-3PAPISID=shared; SAPISID=shared2"
    first = _make_provider("inst_first")._build_auth_headers(shared_cookie, 0)
    second = _make_provider("inst_second")._build_auth_headers(shared_cookie, 1)

    assert first["cookie"] == second["cookie"]  # same browser session
    assert first["x-goog-authuser"] == "0"
    assert second["x-goog-authuser"] == "1"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 0),
        (0, 0),
        (1, 1),
        ("2", 2),
        ("", 0),
        ("not_a_number", 0),
        (-1, 0),
    ],
)
def test_configured_auth_user_coerces_safely(provider, configured, expected):
    provider.config = _StubConfig({ytm.CONF_AUTH_USER: configured})
    assert provider._configured_auth_user() == expected


def test_build_auth_headers_returns_a_fresh_dict_each_call(provider, monkeypatch):
    _forbid_open(monkeypatch)
    cookie = "__Secure-3PAPISID=a; SAPISID=b"
    first = provider._build_auth_headers(cookie)
    second = provider._build_auth_headers(cookie)
    assert first is not second


def test_instance_name_postfix_is_never_none_or_empty():
    """MA formats this property directly, so None renders as a literal "[None]".

    `Provider.default_name` builds a numeric fallback into a local variable
    and then interpolates `self.instance_name_postfix` instead, so the
    fallback never reaches the name. The postfix also lands in library data,
    since playlist owners fall back to the provider name.
    """
    instance = _make_provider("ytmusic_free--abcdef123456")
    instance.config = _StubConfig({})
    postfix = instance.instance_name_postfix
    assert postfix
    assert postfix is not None
    assert "None" not in postfix


def test_instance_name_postfix_differs_between_instances():
    first = _make_provider("ytmusic_free--aaaaaaaa1111")
    second = _make_provider("ytmusic_free--bbbbbbbb2222")
    first.config = _StubConfig({})
    second.config = _StubConfig({})
    assert first.instance_name_postfix != second.instance_name_postfix


def test_instance_name_postfix_prefers_the_brand_account():
    instance = _make_provider("ytmusic_free--abcdef123456")
    instance.config = _StubConfig({ytm.CONF_BRAND_ACCOUNT: "112233445566"})
    assert instance.instance_name_postfix == "112233445566"


def test_instance_name_postfix_uses_the_account_index_when_set():
    instance = _make_provider("ytmusic_free--abcdef123456")
    instance.config = _StubConfig({ytm.CONF_AUTH_USER: 2})
    assert instance.instance_name_postfix == "account 2"


def test_instance_name_postfix_survives_a_missing_config():
    instance = _make_provider("ytmusic_free--abcdef123456")
    instance.config = None
    assert instance.instance_name_postfix


def test_library_seen_nonempty_is_not_shared_between_instances():
    """A class-level `= {}` default here would cross-contaminate accounts."""
    assert "_library_seen_nonempty" not in vars(ytm.YoutubeMusicFreeProvider)

    alice = _make_provider("inst_alice")
    bob = _make_provider("inst_bob")
    alice._record_library_count("tracks", 5)

    assert alice._library_seen_nonempty == {"tracks": True}
    # Bob's library is genuinely empty; Alice having tracks must not make
    # Bob look like a partial-auth lapse (issue #10).
    assert bob._record_library_count("tracks", 0) is False


def test_create_ytmusic_client_passes_headers_and_brand_account_through(provider, monkeypatch):
    """The headers dict reaches YTMusic untouched, alongside the brand account."""
    captured = {}

    class _FakeYTMusic:
        def __init__(self, auth=None, user=None):
            captured["auth"] = auth
            captured["user"] = user

    fake_module = MagicMock()
    fake_module.YTMusic = _FakeYTMusic
    monkeypatch.setattr(ytm.importlib, "import_module", lambda name: fake_module)

    headers = {"cookie": "__Secure-3PAPISID=a", "authorization": "SAPISIDHASH x_y"}
    provider._create_ytmusic_client(auth=headers, user="brand123")

    assert captured["auth"] == headers
    assert captured["user"] == "brand123"


def test_create_ytmusic_client_anonymous_passes_no_auth(provider, monkeypatch):
    captured = {}

    class _FakeYTMusic:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            captured["called_with"] = kwargs

    fake_module = MagicMock()
    fake_module.YTMusic = _FakeYTMusic
    monkeypatch.setattr(ytm.importlib, "import_module", lambda name: fake_module)

    provider._create_ytmusic_client()
    assert captured["called_with"] == {}


class _StubConfig:
    """Minimal stand-in for MA's ProviderConfig."""

    def __init__(self, values):
        self._values = values

    def get_value(self, key):
        return self._values.get(key)


def _setup_instance(monkeypatch, instance_id, values):
    """Run handle_async_init against stubs and return the provider.

    Package installation and client construction are stubbed out: neither
    yt-dlp nor ytmusicapi is installed in the test environment.
    """
    instance = _make_provider(instance_id)
    instance.config = _StubConfig(values)
    _forbid_open(monkeypatch)
    created = []

    async def _noop():
        return None

    def _fake_client(auth=None, user=None):
        created.append({"auth": auth, "user": user})
        return MagicMock()

    monkeypatch.setattr(instance, "_install_packages", _noop)
    monkeypatch.setattr(instance, "_purge_legacy_auth_file", _noop)
    monkeypatch.setattr(instance, "_create_ytmusic_client", _fake_client)
    asyncio.run(instance.handle_async_init())
    instance._created_clients = created
    return instance


def _setup_cookie_instance(monkeypatch, instance_id, *, library, account_info):
    """Run handle_async_init with cookie auth and a scripted ytmusicapi client."""
    instance = _make_provider(instance_id)
    instance.config = _StubConfig(
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "__Secure-3PAPISID=abc; SID=def",
        }
    )
    _forbid_open(monkeypatch)

    async def _noop():
        return None

    client = MagicMock()
    client.get_library_songs = MagicMock(return_value=library)
    client.get_account_info = MagicMock(return_value=account_info)

    monkeypatch.setattr(instance, "_install_packages", _noop)
    monkeypatch.setattr(instance, "_purge_legacy_auth_file", _noop)
    monkeypatch.setattr(instance, "_create_ytmusic_client", lambda auth=None, user=None: client)
    monkeypatch.setattr(instance, "_build_auth_headers", lambda cookie, user: {"Cookie": cookie})
    handler = _attach_capture(instance)
    asyncio.run(instance.handle_async_init())
    return instance, handler


def test_init_does_not_claim_success_on_a_lapsed_cookie(monkeypatch):
    """Issue #55: "library sync enabled" was printed over a dead cookie.

    A lapsed YouTube session answers HTTP 200 with a logged-out payload rather
    than 401, so the validation call succeeded and the provider announced that
    library sync was enabled. The reporter flagged that line as the odd part of
    their evidence, and they were right: it was the bug.
    """
    instance, handler = _setup_cookie_instance(
        monkeypatch, "inst_lapsed", library=[], account_info={}
    )

    assert instance._authenticated is False
    joined = " ".join(handler.messages()).lower()
    assert "library sync enabled" not in joined
    assert "anonymous" in joined


def test_init_still_authenticates_an_account_with_an_empty_library(monkeypatch):
    """An empty library is not evidence of a bad cookie when the session is live."""
    instance, _ = _setup_cookie_instance(
        monkeypatch, "inst_empty_but_valid", library=[], account_info={"accountName": "Real"}
    )

    assert instance._authenticated is True


def test_init_skips_the_probe_when_the_library_call_returns_items(monkeypatch):
    """The common path must not pay for an extra request."""
    instance, _ = _setup_cookie_instance(
        monkeypatch,
        "inst_populated",
        library=[{"videoId": "v1", "title": "x"}],
        account_info={"accountName": "Real"},
    )

    assert instance._authenticated is True
    assert instance._ytmusic.get_account_info.call_count == 0


def test_prefer_quality_false_is_honored(monkeypatch):
    """A configured False must survive; `or True` used to swallow it."""
    instance = _setup_instance(
        monkeypatch, "inst_low", {ytm.CONF_PREFER_AUDIO_QUALITY: False}
    )
    assert instance._prefer_quality is False


def test_prefer_quality_defaults_to_true_when_unset(monkeypatch):
    instance = _setup_instance(monkeypatch, "inst_default", {})
    assert instance._prefer_quality is True


def test_two_instances_keep_independent_quality_settings(monkeypatch):
    """Per-instance config is the point of multi-instance support."""
    high = _setup_instance(
        monkeypatch, "inst_high", {ytm.CONF_PREFER_AUDIO_QUALITY: True}
    )
    low = _setup_instance(
        monkeypatch, "inst_low", {ytm.CONF_PREFER_AUDIO_QUALITY: False}
    )
    assert high._prefer_quality is True
    assert low._prefer_quality is False


def test_anonymous_instance_builds_client_without_auth(monkeypatch):
    instance = _setup_instance(
        monkeypatch, "inst_anon", {ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_NONE}
    )
    assert instance._authenticated is False
    assert instance._created_clients == [{"auth": None, "user": None}]


def test_cookie_instance_passes_headers_dict_not_a_path(monkeypatch):
    """The regression guard for issue #40: no filename ever reaches ytmusicapi."""
    instance = _setup_instance(
        monkeypatch,
        "inst_cookie",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "__Secure-3PAPISID=a; SAPISID=b",
            ytm.CONF_BRAND_ACCOUNT: "brand42",
        },
    )
    call = instance._created_clients[0]
    assert isinstance(call["auth"], dict)
    assert call["user"] == "brand42"
    assert "SAPISIDHASH" in call["auth"]["authorization"]
    assert instance._authenticated is True


def test_two_cookie_instances_do_not_share_credentials(monkeypatch):
    """Different accounts, different headers, no shared file to overwrite."""
    alice = _setup_instance(
        monkeypatch,
        "inst_alice",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "__Secure-3PAPISID=alice; SAPISID=alice2",
        },
    )
    bob = _setup_instance(
        monkeypatch,
        "inst_bob",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "__Secure-3PAPISID=bob; SAPISID=bob2",
            ytm.CONF_BRAND_ACCOUNT: "brand_bob",
        },
    )
    alice_auth = alice._created_clients[0]["auth"]
    bob_auth = bob._created_clients[0]["auth"]

    assert "alice" in alice_auth["cookie"]
    assert "bob" in bob_auth["cookie"]
    assert alice_auth["cookie"] != bob_auth["cookie"]
    assert alice._created_clients[0]["user"] is None
    assert bob._created_clients[0]["user"] == "brand_bob"


def test_configured_account_index_reaches_the_client(monkeypatch):
    """Two household members sharing one browser need this to differ."""
    instance = _setup_instance(
        monkeypatch,
        "inst_second_user",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "__Secure-3PAPISID=a; SAPISID=b",
            ytm.CONF_AUTH_USER: 1,
        },
    )
    assert instance._created_clients[0]["auth"]["x-goog-authuser"] == "1"


def test_cookie_instance_falls_back_to_anonymous_on_bad_cookie(monkeypatch):
    """An unusable cookie must not take the instance down."""
    instance = _setup_instance(
        monkeypatch,
        "inst_bad",
        {
            ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE,
            ytm.CONF_COOKIE: "SID=no_papisid_here",
        },
    )
    assert instance._authenticated is False
    # Second call is the anonymous retry after _build_auth_headers raised.
    assert instance._created_clients[-1] == {"auth": None, "user": None}


# ---------------------------------------------------------------------------
# Auth notice in the Music Assistant UI (issue #92)
# ---------------------------------------------------------------------------


class _FakeConfigController:
    """Records what the provider writes as its last_error."""

    def __init__(self):
        self.writes = []

    def update_provider_last_error(self, instance_id, error):
        self.writes.append((instance_id, error))


class _FakeMass:
    """Just the server surface _sync_auth_notice touches."""

    def __init__(self):
        self.config = _FakeConfigController()
        self.events = []
        self.providers = []

    def signal_event(self, event, object_id=None, data=None):
        self.events.append((event, data))


def _attach_mass(instance):
    """Register the instance with a fake server, as a successful load would."""
    mass = _FakeMass()
    mass.providers.append(instance)
    instance.mass = mass
    return mass


def _loaded_cookie_instance(monkeypatch, instance_id):
    """A cookie instance that authenticated at startup and finished loading."""
    instance, handler = _setup_cookie_instance(
        monkeypatch, instance_id, library=[{"videoId": "v1"}], account_info={"accountName": "A"}
    )
    mass = _attach_mass(instance)
    instance.initialized.set()
    return instance, mass, handler


_BAD_COOKIE = {ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE, ytm.CONF_COOKIE: "SID=no_papisid_here"}


def test_cookie_failure_records_a_notice_naming_the_mode_and_the_error(monkeypatch):
    instance = _setup_instance(monkeypatch, "inst_notice_bad", _BAD_COOKIE)

    notice = instance._auth_notice
    assert notice is not None
    assert "anonymous mode" in notice
    # Quoted in a code span, or Markdown would eat the underscores.
    assert "`Cookie must contain __Secure-3PAPISID`" in notice


def test_empty_cookie_records_a_notice_and_a_warning(monkeypatch):
    """Cookie auth chosen with the field left empty used to log nothing at all."""
    instance = _make_provider("inst_notice_empty")
    handler = _attach_capture(instance)
    instance.config = _StubConfig({ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE, ytm.CONF_COOKIE: ""})

    async def _noop():
        return None

    monkeypatch.setattr(instance, "_install_packages", _noop)
    monkeypatch.setattr(instance, "_purge_legacy_auth_file", _noop)
    monkeypatch.setattr(instance, "_create_ytmusic_client", lambda auth=None, user=None: MagicMock())
    asyncio.run(instance.handle_async_init())

    assert instance._auth_notice == ytm.AUTH_NOTICE_COOKIE_MISSING
    assert any(
        r.levelname == "WARNING" and "no cookie is set" in r.getMessage() for r in handler.records
    )


@pytest.mark.parametrize(
    "values",
    [
        {ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_NONE},
        {},
        {ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE, ytm.CONF_COOKIE: "__Secure-3PAPISID=a; SAPISID=b"},
    ],
    ids=["anonymous", "unset", "working-cookie"],
)
def test_no_notice_when_nothing_is_wrong(monkeypatch, values):
    """A deliberately anonymous instance is not a failure and must not look like one."""
    instance = _setup_instance(monkeypatch, "inst_notice_none", values)
    assert instance._auth_notice is None


def test_notice_is_written_only_after_music_assistant_finishes_loading(monkeypatch):
    """Music Assistant clears last_error after a successful load.

    On 2.10.5 that clear runs after loaded_in_mass, so writing any earlier gets
    the notice wiped. ``initialized`` is set after the clear on every version.
    """
    instance = _setup_instance(monkeypatch, "inst_notice_wait", _BAD_COOKIE)
    mass = _attach_mass(instance)

    async def _scenario():
        await instance.loaded_in_mass()
        await asyncio.sleep(0)
        writes_before = list(mass.config.writes)
        instance.initialized.set()
        await instance._auth_notice_task
        return writes_before

    writes_before = asyncio.run(_scenario())

    assert writes_before == []
    assert len(mass.config.writes) == 1
    instance_id, error = mass.config.writes[0]
    assert instance_id == "inst_notice_wait"
    assert error.error_code == ytm.LoginFailed.error_code
    assert error.message == instance._auth_notice
    # A translation key would make Music Assistant show its generic text.
    assert error.translation_key is None
    assert mass.events == [(ytm.EventType.PROVIDERS_UPDATED, mass.providers)]


def test_loaded_instance_without_a_problem_writes_nothing(monkeypatch):
    instance = _setup_instance(
        monkeypatch, "inst_notice_quiet", {ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_NONE}
    )
    mass = _attach_mass(instance)

    async def _scenario():
        await instance.loaded_in_mass()
        instance.initialized.set()
        await instance._auth_notice_task

    asyncio.run(_scenario())

    assert mass.config.writes == []
    assert mass.events == []


def test_notice_is_not_written_for_an_instance_that_was_replaced(monkeypatch):
    """A reload swaps in a new instance whose own load just cleared the field."""
    instance = _setup_instance(monkeypatch, "inst_notice_stale", _BAD_COOKIE)
    mass = _attach_mass(instance)
    mass.providers[:] = [_make_provider("inst_notice_stale")]
    instance.initialized.set()

    instance._sync_auth_notice()

    assert mass.config.writes == []


def test_unload_cancels_a_pending_notice(monkeypatch):
    instance = _setup_instance(monkeypatch, "inst_notice_unload", _BAD_COOKIE)
    mass = _attach_mass(instance)

    async def _scenario():
        await instance.loaded_in_mass()
        await asyncio.sleep(0)
        await instance.unload()
        await asyncio.sleep(0)
        return instance._auth_notice_task

    task = asyncio.run(_scenario())

    assert task.cancelled()
    assert mass.config.writes == []


@pytest.mark.parametrize("mass", [None, types.SimpleNamespace(config=object())])
def test_notice_is_skipped_without_server_support(monkeypatch, mass):
    """No server in the tests, or Music Assistant 2.9 without the API."""
    instance = _setup_instance(monkeypatch, "inst_notice_old", _BAD_COOKIE)
    instance.mass = mass
    instance.initialized.set()

    instance._sync_auth_notice()

    assert instance._auth_notice_shown is None


def test_a_failing_write_never_breaks_the_provider(monkeypatch):
    instance = _setup_instance(monkeypatch, "inst_notice_raise", _BAD_COOKIE)
    mass = _attach_mass(instance)

    def _boom(instance_id, error):
        raise RuntimeError("config store unavailable")

    mass.config.update_provider_last_error = _boom
    instance.initialized.set()

    instance._sync_auth_notice()

    # Not marked shown, so the next sync tries again.
    assert instance._auth_notice_shown is None


def test_runtime_auth_lapse_raises_the_notice_once(monkeypatch):
    """A cookie that worked at startup and lapses later must surface too."""
    instance, mass, _ = _loaded_cookie_instance(monkeypatch, "inst_notice_lapse")

    instance._warn_library_error("get_library_songs", RuntimeError("Server returned HTTP 401"))
    instance._warn_library_error("get_library_albums", RuntimeError("401 Client Error: x"))

    assert len(mass.config.writes) == 1
    _, error = mass.config.writes[0]
    assert error.message.startswith("YouTube stopped accepting the cookie")
    assert "`Server returned HTTP 401`" in error.message


def test_non_auth_library_errors_raise_no_notice(monkeypatch):
    instance, mass, _ = _loaded_cookie_instance(monkeypatch, "inst_notice_timeout")

    instance._warn_library_error("get_library_songs", RuntimeError("Read timed out"))

    assert instance._auth_notice is None
    assert mass.config.writes == []


def test_session_probe_lapse_raises_the_notice(monkeypatch):
    """The probe path words its error so the lapse pattern does not match it."""
    instance, mass, _ = _loaded_cookie_instance(monkeypatch, "inst_notice_probe")
    instance._ytmusic.get_account_info = MagicMock(return_value={})

    with pytest.raises(RuntimeError, match="not signed in"):
        asyncio.run(instance._guard_partial_auth_empty("tracks", 0))

    assert len(mass.config.writes) == 1
    assert "not signed in" in mass.config.writes[0][1].message


def test_a_populated_sync_withdraws_a_lapse_notice(monkeypatch):
    """One stray 401 must not leave the badge up while the library syncs fine."""
    instance, mass, _ = _loaded_cookie_instance(monkeypatch, "inst_notice_clear")
    instance._warn_library_error("library_add for x", RuntimeError("HTTP 401 Unauthorized"))

    asyncio.run(instance._guard_partial_auth_empty("tracks", 3))

    assert [error for _, error in mass.config.writes][1:] == [None]
    assert instance._auth_notice is None
    assert len(mass.events) == 2


def test_a_lapse_does_not_replace_the_startup_notice(monkeypatch):
    """An instance that never authenticated already says why; keep that text."""
    instance = _setup_instance(monkeypatch, "inst_notice_keep", _BAD_COOKIE)
    startup_notice = instance._auth_notice

    instance._warn_library_error("get_home", RuntimeError("HTTP 401"))

    assert instance._auth_notice == startup_notice


def test_notice_reason_is_one_short_line_without_backticks():
    long_error = RuntimeError("first line\n  second `quoted` line " + "x" * 400)

    reason = ytm._auth_notice_reason(long_error)

    assert "\n" not in reason
    assert "`" not in reason
    assert len(reason) <= ytm.AUTH_NOTICE_REASON_MAX
    assert reason.endswith("...")
    assert reason.startswith("first line second 'quoted' line")


def test_notice_reason_never_quotes_cookie_values():
    """requests embeds a rejected header value in its error message."""
    err = ValueError(
        "Invalid leading whitespace in header value: ' SID=abc123; HSID=def456; SSID=ghi'"
    )

    reason = ytm._auth_notice_reason(err)

    for secret in ("abc123", "def456", "ghi"):
        assert secret not in reason
    assert "SID=<redacted>" in reason


def test_cookie_with_surrounding_whitespace_still_authenticates(monkeypatch):
    instance = _setup_instance(
        monkeypatch,
        "inst_cookie_space",
        {ytm.CONF_AUTH_TYPE: ytm.AUTH_TYPE_COOKIE, ytm.CONF_COOKIE: "  __Secure-3PAPISID=a; SAPISID=b \n"},
    )
    assert instance._authenticated is True
    assert instance._created_clients[0]["auth"]["cookie"] == "__Secure-3PAPISID=a; SAPISID=b"


def test_notice_reason_falls_back_to_the_exception_type():
    assert ytm._auth_notice_reason(RuntimeError()) == "RuntimeError"


# ---------------------------------------------------------------------------
# Legacy auth file cleanup (issue #40)
# ---------------------------------------------------------------------------


def test_legacy_auth_file_constant_is_the_old_hardcoded_path():
    assert ytm.LEGACY_AUTH_FILE == "/data/ytmusic_browser_auth.json"


def test_purge_legacy_auth_file_removes_a_leftover_file(provider, tmp_path, monkeypatch):
    stale = tmp_path / "ytmusic_browser_auth.json"
    stale.write_text('{"cookie": "secret"}', encoding="utf-8")
    monkeypatch.setattr(ytm, "LEGACY_AUTH_FILE", str(stale))

    asyncio.run(provider._purge_legacy_auth_file())

    assert not stale.exists()


def test_purge_legacy_auth_file_is_a_noop_when_absent(provider, tmp_path, monkeypatch):
    monkeypatch.setattr(ytm, "LEGACY_AUTH_FILE", str(tmp_path / "not_here.json"))
    # Must not raise.
    asyncio.run(provider._purge_legacy_auth_file())


def test_purge_legacy_auth_file_survives_an_unremovable_file(provider, monkeypatch):
    """A read-only or missing /data must never block provider setup."""
    monkeypatch.setattr(ytm.os.path, "exists", lambda _p: True)

    def _boom(_path):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(ytm.os, "remove", _boom)
    # Must not raise.
    asyncio.run(provider._purge_legacy_auth_file())


# ---------------------------------------------------------------------------
# _parse_track
# ---------------------------------------------------------------------------


def test_parse_track_minimal(provider):
    track = provider._parse_track(
        {
            "videoId": "abc123",
            "title": "Some Song",
            "artists": [{"id": "UCart", "name": "An Artist"}],
        }
    )
    assert track.item_id == "abc123"
    assert track.name == "Some Song"
    assert track.artists[0].item_id == "UCart"
    assert track.artists[0].name == "An Artist"
    mappings = list(track.provider_mappings)
    assert mappings[0].item_id == "abc123"
    assert mappings[0].provider_domain == "ytmusic_free"
    assert mappings[0].url == f"{ytm.YTM_DOMAIN}/watch?v=abc123"


def test_parse_track_missing_video_id_raises(provider):
    with pytest.raises(InvalidDataError, match="videoId"):
        provider._parse_track({"title": "no id"})


def test_parse_track_missing_artists_raises(provider):
    with pytest.raises(InvalidDataError, match="artists"):
        provider._parse_track({"videoId": "abc", "title": "x", "artists": []})


def test_parse_track_artist_fallback_when_id_missing(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Song",
            "artists": [{"name": "Solo Singer"}],
        }
    )
    assert track.artists[0].name == "Solo Singer"
    assert track.artists[0].item_id == "unknown_Solo Singer"


def test_parse_track_various_artists_resolves_to_canonical_id(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Compilation Song",
            "artists": [{"name": "Various Artists"}],
        }
    )
    assert track.artists[0].item_id == ytm.VARIOUS_ARTISTS_YTM_ID


def test_parse_track_artist_uses_browse_id_and_artist_keys(provider):
    """A browseId/artist-keyed track artist resolves to a real id, not unknown_*."""
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Song",
            "artists": [{"browseId": "UCart", "artist": "Real Artist"}],
        }
    )
    assert track.artists[0].item_id == "UCart"
    assert track.artists[0].name == "Real Artist"


def test_parse_track_various_artists_via_artist_key(provider):
    """Track artist keyed only as artist='Various Artists' resolves to canonical id."""
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "Compilation",
            "artists": [{"artist": "Various Artists"}],
        }
    )
    assert track.artists[0].item_id == ytm.VARIOUS_ARTISTS_YTM_ID
    assert track.artists[0].name == "Various Artists"


def test_parse_track_duration_from_seconds(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
            "duration_seconds": "245",
        }
    )
    assert track.duration == 245


def test_parse_track_duration_from_int(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
            "duration": "180",
        }
    )
    assert track.duration == 180


def test_parse_track_album_mapping(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
            "album": {"id": "MPREb_album", "name": "Album Name"},
        }
    )
    assert track.album.item_id == "MPREb_album"
    assert track.album.name == "Album Name"
    assert track.album.media_type == MediaType.ALBUM


def test_parse_track_track_number_kwarg(provider):
    track = provider._parse_track(
        {
            "videoId": "abc",
            "title": "x",
            "artists": [{"id": "UC1", "name": "A"}],
        },
        track_number=7,
    )
    assert track.track_number == 7


# ---------------------------------------------------------------------------
# _parse_album
# ---------------------------------------------------------------------------


def test_parse_album_basic(provider):
    album = provider._parse_album(
        {
            "browseId": "MPREb_xyz",
            "title": "An Album",
            "artists": [{"id": "UC1", "name": "Artist"}],
            "year": "2023",
            "type": "Album",
        }
    )
    assert album.item_id == "MPREb_xyz"
    assert album.name == "An Album"
    assert album.year == 2023
    assert album.album_type == AlbumType.ALBUM


def test_parse_album_missing_id_raises(provider):
    with pytest.raises(InvalidDataError, match="ID"):
        provider._parse_album({"title": "no id"})


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("Single", AlbumType.SINGLE),
        ("EP", AlbumType.EP),
        ("Album", AlbumType.ALBUM),
        ("", AlbumType.UNKNOWN),
        ("Compilation", AlbumType.UNKNOWN),
    ],
)
def test_parse_album_type_mapping(provider, raw_type, expected):
    album = provider._parse_album(
        {"browseId": "MPREb_x", "title": "A", "type": raw_type}
    )
    assert album.album_type == expected


def test_parse_album_explicit_id_argument_wins(provider):
    album = provider._parse_album(
        {"browseId": "ignored", "title": "A"}, album_id="explicit-id"
    )
    assert album.item_id == "explicit-id"


def test_parse_album_inferred_live(provider):
    album = provider._parse_album(
        {"browseId": "MPREb_live", "title": "Live at the Apollo", "type": "Album"}
    )
    assert album.album_type == AlbumType.LIVE


def test_parse_album_artist_uses_browse_id_and_artist_keys(provider):
    """Album artists from search results are keyed browseId/artist, not id/name."""
    album = provider._parse_album(
        {
            "browseId": "MPREb_x",
            "title": "A",
            "artists": [{"browseId": "UCartist", "artist": "Album Artist"}],
        }
    )
    assert len(album.artists) == 1
    assert album.artists[0].item_id == "UCartist"
    assert album.artists[0].name == "Album Artist"


def test_parse_album_artist_various_artists_via_artist_key(provider):
    album = provider._parse_album(
        {
            "browseId": "MPREb_x",
            "title": "A",
            "artists": [{"artist": "Various Artists"}],
        }
    )
    assert len(album.artists) == 1
    assert album.artists[0].item_id == ytm.VARIOUS_ARTISTS_YTM_ID


# ---------------------------------------------------------------------------
# _parse_artist
# ---------------------------------------------------------------------------


def test_parse_artist_basic(provider):
    artist = provider._parse_artist(
        {"channelId": "UCabc", "name": "An Artist"}
    )
    assert artist.item_id == "UCabc"
    assert artist.name == "An Artist"


def test_parse_artist_uses_id_field_when_channelid_missing(provider):
    artist = provider._parse_artist({"id": "UC123", "name": "Other"})
    assert artist.item_id == "UC123"


def test_parse_artist_various_artists_canonical_id(provider):
    artist = provider._parse_artist({"name": "Various Artists"})
    assert artist.item_id == ytm.VARIOUS_ARTISTS_YTM_ID


def test_parse_artist_missing_id_raises(provider):
    with pytest.raises(InvalidDataError, match="ID"):
        provider._parse_artist({"name": "Mystery"})


def test_parse_artist_uses_browse_id_and_artist_keys(provider):
    """Search results (filter='artists') key the id as browseId, name as artist."""
    artist = provider._parse_artist({"browseId": "UCxyz", "artist": "Some Artist"})
    assert artist.item_id == "UCxyz"
    assert artist.name == "Some Artist"


def test_parse_artist_channel_id_takes_precedence_over_browse_id(provider):
    artist = provider._parse_artist(
        {"channelId": "UCchan", "browseId": "UCbrowse", "name": "A"}
    )
    assert artist.item_id == "UCchan"


def test_parse_artist_name_key_preferred_over_artist_key(provider):
    artist = provider._parse_artist(
        {"browseId": "UCx", "name": "Real Name", "artist": "Alt Name"}
    )
    assert artist.name == "Real Name"


def test_parse_artist_various_artists_via_artist_key(provider):
    artist = provider._parse_artist({"artist": "Various Artists"})
    assert artist.item_id == ytm.VARIOUS_ARTISTS_YTM_ID


# ---------------------------------------------------------------------------
# _get_artist_item_mapping
# ---------------------------------------------------------------------------


def test_artist_item_mapping_id_precedence(provider):
    mapping = provider._get_artist_item_mapping(
        {"id": "UCid", "channelId": "UCchan", "browseId": "UCbrowse", "name": "A"}
    )
    assert mapping.media_type == MediaType.ARTIST
    assert mapping.item_id == "UCid"
    assert mapping.name == "A"


def test_artist_item_mapping_browse_id_and_artist_keys(provider):
    mapping = provider._get_artist_item_mapping(
        {"browseId": "UCbrowse", "artist": "Search Artist"}
    )
    assert mapping.item_id == "UCbrowse"
    assert mapping.name == "Search Artist"


def test_artist_item_mapping_various_artists_via_artist_key(provider):
    mapping = provider._get_artist_item_mapping({"artist": "Various Artists"})
    assert mapping.item_id == ytm.VARIOUS_ARTISTS_YTM_ID


def test_artist_item_mapping_empty_name_falls_back_to_unknown(provider):
    """A truthy id with a present-but-empty name/artist must not yield a blank name."""
    assert provider._get_artist_item_mapping(
        {"channelId": "UCx", "artist": ""}
    ).name == "Unknown"
    assert provider._get_artist_item_mapping(
        {"browseId": "UCy", "name": "", "artist": None}
    ).name == "Unknown"


def test_parse_artist_empty_name_falls_back_to_unknown(provider):
    artist = provider._parse_artist({"browseId": "UCz", "name": "", "artist": ""})
    assert artist.name == "Unknown Artist"


# ---------------------------------------------------------------------------
# _parse_playlist
# ---------------------------------------------------------------------------


def test_parse_playlist_id_field(provider):
    playlist = provider._parse_playlist({"id": "PL123", "title": "P"})
    assert playlist.item_id == "PL123"
    assert playlist.is_editable is False


def test_parse_playlist_falls_back_to_browse_id(provider):
    playlist = provider._parse_playlist({"browseId": "VLPL456", "title": "P"})
    assert playlist.item_id == "VLPL456"


def test_parse_playlist_owner_string(provider):
    playlist = provider._parse_playlist(
        {"id": "PL", "title": "P", "author": "Some User"}
    )
    assert playlist.owner == "Some User"


def test_parse_playlist_owner_list_of_dicts(provider):
    playlist = provider._parse_playlist(
        {"id": "PL", "title": "P", "author": [{"name": "First"}, {"name": "Second"}]}
    )
    assert playlist.owner == "First"


def test_parse_playlist_owner_dict(provider):
    playlist = provider._parse_playlist(
        {"id": "PL", "title": "P", "author": {"name": "Channel"}}
    )
    assert playlist.owner == "Channel"


def test_parse_playlist_owner_default_to_provider_name(provider):
    playlist = provider._parse_playlist({"id": "PL", "title": "P"})
    assert playlist.owner == provider.name


# ---------------------------------------------------------------------------
# _parse_thumbnails
# ---------------------------------------------------------------------------


def test_parse_thumbnails_picks_largest_first(provider):
    thumbs = [
        {"url": "https://example/a=w200-h200", "width": 200, "height": 200},
        {"url": "https://example/a=w800-h800", "width": 800, "height": 800},
        {"url": "https://example/a=w400-h400", "width": 400, "height": 400},
    ]
    images = provider._parse_thumbnails(thumbs)
    assert len(images) == 1
    assert "w800" in images[0].path or "w600" in images[0].path
    assert images[0].type == ImageType.THUMB


def test_parse_thumbnails_landscape_for_maxres(provider):
    thumbs = [
        {"url": "https://example/maxresdefault.jpg", "width": 1280, "height": 720},
    ]
    images = provider._parse_thumbnails(thumbs)
    assert images[0].type == ImageType.LANDSCAPE


# Issue #90: Music Assistant only uses THUMB images for player artwork, so every
# item that has any usable picture must come out with one.
MAXRES = "https://i.ytimg.com/vi/x/maxresdefault.jpg"
MAXRES_ONLY_VIDEO = [
    {"url": "https://i.ytimg.com/vi/x/hqdefault.jpg", "width": 480, "height": 360},
    {"url": MAXRES, "width": 1280, "height": 720},
]


def test_parse_thumbnails_mirrors_landscape_as_thumb_for_video(provider):
    # hqdefault is dropped by the width filter, leaving only a LANDSCAPE.
    images = provider._parse_thumbnails(MAXRES_ONLY_VIDEO)
    assert [img.type for img in images] == [ImageType.LANDSCAPE, ImageType.THUMB]
    assert images[1].path == images[0].path == MAXRES
    assert images[1].provider == provider.instance_id
    assert images[1].remotely_accessible is True


def test_parse_thumbnails_no_mirror_when_thumb_exists(provider):
    thumbs = [
        {"url": MAXRES, "width": 1280, "height": 720},
        {"url": "https://example/a=w800-h800", "width": 800, "height": 800},
    ]
    images = provider._parse_thumbnails(thumbs)
    assert [img.type for img in images] == [ImageType.LANDSCAPE, ImageType.THUMB]
    assert images[1].path == "https://example/a=w800-h800"


def test_parse_thumbnails_mirrors_once_for_several_landscapes(provider):
    thumbs = [
        {"url": MAXRES, "width": 1280, "height": 720},
        {"url": "https://i.ytimg.com/vi/y/maxresdefault.jpg", "width": 1920, "height": 1080},
    ]
    images = provider._parse_thumbnails(thumbs)
    thumbs_out = [img for img in images if img.type == ImageType.THUMB]
    assert [img.path for img in thumbs_out] == ["https://i.ytimg.com/vi/y/maxresdefault.jpg"]


def test_parse_thumbnails_crops_a_wide_size_param_banner_square(provider):
    # A banner with no entry under 600 px is all LANDSCAPE; the THUMB fallback
    # asks the image server for a square crop of it.
    thumbs = [
        {"url": "https://lh3.googleusercontent.com/a=w1440-h600", "width": 1440, "height": 600},
        {"url": "https://lh3.googleusercontent.com/a=w2880-h1200-p-l90-rj", "width": 2880, "height": 1200},
    ]
    images = provider._parse_thumbnails(thumbs)
    assert [img.type for img in images] == [ImageType.LANDSCAPE, ImageType.THUMB]
    assert images[1].path == "https://lh3.googleusercontent.com/a=w600-h600-p"


def test_parse_thumbnails_no_fallback_when_a_small_size_param_image_became_thumb(provider):
    # Real artist headers: the 540x225 entry is rewritten to a square THUMB.
    thumbs = [
        {"url": "https://lh3.googleusercontent.com/a=w540-h225", "width": 540, "height": 225},
        {"url": "https://lh3.googleusercontent.com/a=w1440-h600", "width": 1440, "height": 600},
    ]
    images = provider._parse_thumbnails(thumbs)
    assert [img.type for img in images] == [ImageType.LANDSCAPE, ImageType.THUMB]
    assert images[1].path == "https://lh3.googleusercontent.com/a=w600-h600-p"


# Search and playlist rows for video uploads carry a single signed 400x225.
SMALL_FRAME = "https://i.ytimg.com/vi/x/hqdefault.jpg?sqp=-oaymwE&rs=AOn4CL"
SMALL_FRAME_ONLY = [{"url": SMALL_FRAME, "width": 400, "height": 225}]


def test_parse_thumbnails_keeps_a_small_video_frame_for_a_track(provider):
    images = provider._parse_thumbnails(SMALL_FRAME_ONLY, keep_small_frame=True)
    assert [(img.type, img.path) for img in images] == [(ImageType.THUMB, SMALL_FRAME)]


def test_parse_thumbnails_drops_a_small_video_frame_by_default(provider):
    # Playlists: Music Assistant generates a collage when there is no THUMB.
    assert provider._parse_thumbnails(SMALL_FRAME_ONLY) == []


def test_parse_thumbnails_keeps_the_widest_small_video_frame(provider):
    # yt-dlp flat playlist entries top out at 336x188.
    thumbs = [
        {"url": f"https://i.ytimg.com/vi/x/hqdefault.jpg?sqp={w}", "width": w, "height": h}
        for w, h in ((168, 94), (336, 188), (246, 138), (196, 110))
    ]
    images = provider._parse_thumbnails(thumbs, keep_small_frame=True)
    assert [img.path for img in images] == ["https://i.ytimg.com/vi/x/hqdefault.jpg?sqp=336"]
    assert images[0].type == ImageType.THUMB


@pytest.mark.parametrize(
    "thumb",
    [
        # Channel avatar: artists keep deferring to fanart.tv and TheAudioDB.
        {"url": "https://yt3.ggpht.com/abc=s88-c-k-c0x00ffffff-no-rj", "width": 88, "height": 88},
        # No width: the one real case pointed at a deleted video and 404s.
        {"url": "https://i.ytimg.com/vi/gone/mqdefault.jpg"},
    ],
    ids=["channel-avatar", "no-width"],
)
def test_parse_thumbnails_keep_small_frame_ignores_non_frames(provider, thumb):
    assert provider._parse_thumbnails([thumb], keep_small_frame=True) == []


def test_parse_track_keeps_a_small_video_frame(provider):
    track = provider._parse_track(
        {
            "videoId": "x",
            "title": "Mix",
            "artists": [{"name": "Uploader", "id": "UCx"}],
            "thumbnails": SMALL_FRAME_ONLY,
        }
    )
    assert [img.path for img in track.metadata.images] == [SMALL_FRAME]


def test_parse_playlist_leaves_a_frame_only_playlist_to_the_collage(provider):
    playlist = provider._parse_playlist(
        {"playlistId": "PLx", "title": "P", "thumbnails": SMALL_FRAME_ONLY}
    )
    assert not playlist.metadata.images


def test_parse_album_keeps_the_mirrored_thumb_through_unique_list(provider):
    album = provider._parse_album(
        {"browseId": "MPREb_x", "title": "A", "artists": [], "thumbnails": MAXRES_ONLY_VIDEO}
    )
    assert [img.type for img in album.metadata.images] == [ImageType.LANDSCAPE, ImageType.THUMB]


@pytest.mark.parametrize(
    "thumbnails",
    [
        MAXRES_ONLY_VIDEO,
        [{"url": "https://i.ytimg.com/vi/x/hqdefault.jpg?sqp=a", "width": 320, "height": 180}],
    ],
    ids=["maxresdefault-only", "small-frame-only"],
)
def test_get_track_video_upload_carries_a_thumb(provider, thumbnails):
    """End to end through get_track's videoDetails normalisation (#90)."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "x",
                "title": "Mix",
                "lengthSeconds": "3600",
                "author": "Uploader",
                "thumbnail": {"thumbnails": thumbnails},
            }
        }
    )
    provider._ytmusic = mock
    track = asyncio.run(provider.get_track("x"))
    assert any(img.type == ImageType.THUMB for img in track.metadata.images)


def test_parse_thumbnails_skips_empty_url(provider):
    thumbs = [{"url": "", "width": 800, "height": 800}]
    images = provider._parse_thumbnails(thumbs)
    assert images == []


def test_parse_thumbnails_skips_low_res_without_size_param(provider):
    thumbs = [{"url": "https://example/raw.jpg", "width": 100, "height": 100}]
    images = provider._parse_thumbnails(thumbs)
    assert images == []


# ---------------------------------------------------------------------------
# _minimal_track
# ---------------------------------------------------------------------------


def test_minimal_track_returns_playable_stub(provider):
    track = provider._minimal_track("vid42")
    assert track.item_id == "vid42"
    assert track.name == "vid42"
    assert track.artists[0].name == "Unknown Artist"
    mapping = next(iter(track.provider_mappings))
    assert mapping.url == f"{ytm.YTM_DOMAIN}/watch?v=vid42"
    # The fixture leaves the quality toggle on, so the advertised format has to
    # match what bestaudio actually yields on a free account: Opus, not M4A.
    assert mapping.audio_format.content_type == ContentType.OPUS


# ---------------------------------------------------------------------------
# Advertised catalog format (must not contradict the resolved stream)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefer_quality", "expected"),
    [(True, ContentType.OPUS), (False, ContentType.M4A)],
)
def test_catalog_audio_format_follows_the_quality_toggle(provider, prefer_quality, expected):
    """The mapping has to advertise whatever the selector will actually pick.

    Quality mode asks for ``bestaudio`` and gets Opus on a free account;
    compatibility mode filters to ``[ext=m4a]`` and gets AAC. Hardcoding M4A
    made every quality-mode mapping disagree with its own stream.
    """
    provider._prefer_quality = prefer_quality
    assert provider._catalog_audio_format().content_type == expected


@pytest.mark.parametrize("prefer_quality", [True, False])
def test_parsed_and_minimal_tracks_agree_on_advertised_format(provider, prefer_quality):
    """Both track builders must advertise the same thing, not drift apart."""
    provider._prefer_quality = prefer_quality
    parsed = provider._parse_track(
        {"videoId": "vid42", "title": "Song", "artists": [{"id": "a1", "name": "A"}]}
    )
    minimal = provider._minimal_track("vid42")
    expected = provider._catalog_audio_format().content_type
    assert next(iter(parsed.provider_mappings)).audio_format.content_type == expected
    assert next(iter(minimal.provider_mappings)).audio_format.content_type == expected


def test_advertised_format_matches_the_resolved_stream(provider):
    """Close the loop: the advertisement and the real stream must not diverge.

    This is the regression the hardcoded M4A represented. It asserts against
    the actual stream resolution rather than restating the constant, so a
    future change to either side that breaks the agreement fails here.
    """
    import test_stream_quality as tsq

    for prefer_quality in (True, False):
        module, _ = tsq._stub_yt_dlp_module(tsq.FREE_ACCOUNT_FORMATS)
        provider._yt_dlp_module = module
        provider._prefer_quality = prefer_quality
        details = asyncio.run(provider.get_stream_details("vid42", MediaType.TRACK))
        advertised = provider._catalog_audio_format().content_type
        assert advertised == details.audio_format.content_type, (
            f"catalog advertises {advertised} but the stream resolves as "
            f"{details.audio_format.content_type} (prefer_quality={prefer_quality})"
        )


# ---------------------------------------------------------------------------
# get_config_entries
# ---------------------------------------------------------------------------


def test_get_config_entries_returns_expected_keys():
    entries = asyncio.run(ytm.get_config_entries(mass=None))
    keys = [e.key for e in entries]
    assert keys == [
        ytm.CONF_AUTH_TYPE,
        ytm.CONF_COOKIE,
        ytm.CONF_BRAND_ACCOUNT,
        ytm.CONF_AUTH_USER,
        ytm.CONF_PREFER_AUDIO_QUALITY,
        ytm.CONF_FILTER_AI_MUSIC,
        ytm.CONF_AI_BLOCKLIST,
        ytm.CONF_AI_BLOCKLIST_URL,
    ]
    cookie_entry = next(e for e in entries if e.key == ytm.CONF_COOKIE)
    assert cookie_entry.depends_on == ytm.CONF_AUTH_TYPE
    assert cookie_entry.depends_on_value == ytm.AUTH_TYPE_COOKIE


def test_auth_user_entry_is_cookie_only_and_defaults_to_zero():
    entries = asyncio.run(ytm.get_config_entries(mass=None))
    entry = next(e for e in entries if e.key == ytm.CONF_AUTH_USER)
    assert entry.default_value == 0
    assert entry.depends_on == ytm.CONF_AUTH_TYPE
    assert entry.depends_on_value == ytm.AUTH_TYPE_COOKIE


def test_depends_on_value_is_a_scalar_everywhere():
    """Both MA 2.9's frontend and MA 2.10's server compare depends_on_value
    with plain equality, never containment, so a list can never match a
    selected option. Every gated entry must use the scalar form."""
    entries = asyncio.run(ytm.get_config_entries(mass=None))
    for entry in entries:
        assert not isinstance(entry.depends_on_value, list), entry.key


def test_instance_get_config_entries_matches_module_hook(provider):
    """MA 2.10 resolves the options page via the no-argument instance method;
    2.9 uses the module-level hook (issue #78). Both must return the same
    entries, and the instance method must be callable with no arguments,
    exactly as the 2.10 config controller calls it."""
    from_instance = asyncio.run(provider.get_config_entries())
    from_module = asyncio.run(ytm.get_config_entries(mass=None))
    # Full-object equality, not just keys: the stub ConfigEntry is a dataclass,
    # so this pins every field (type, options, depends_on gating, defaults) to
    # stay identical between the 2.9 and 2.10 paths.
    assert list(from_instance) == list(from_module)


def test_instance_get_config_entries_needs_no_init(provider):
    """2.10 calls the instance method on every load, before handle_async_init,
    so it must work on a freshly constructed provider with no client, no
    config reads, and no async-init state."""
    provider._ytmusic = None
    entries = asyncio.run(provider.get_config_entries())
    assert len(entries) == 8


# ---------------------------------------------------------------------------
# Async dispatch
# ---------------------------------------------------------------------------


def _make_ytm_search_mock(results):
    mock = MagicMock()
    mock.search = MagicMock(return_value=results)
    return mock


def test_search_artist_dispatches_with_artists_filter(provider):
    captured = {}

    def _search(query, filter, limit):
        captured["filter"] = filter
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(provider.search("foo", [MediaType.ARTIST], limit=3))
    assert captured["filter"] == "artists"


def test_search_track_dispatches_with_songs_then_videos_filter(provider):
    """A track search runs the songs filter and then the videos filter: the
    songs tab only sees the YTM catalog, so live recordings and other plain
    YouTube uploads would otherwise never surface (issue #77)."""
    captured = []

    def _search(query, filter, limit):
        captured.append(filter)
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(provider.search("foo", [MediaType.TRACK], limit=3))
    assert captured == ["songs", "videos"]


def test_search_without_tracks_never_calls_videos_filter(provider):
    """The videos pass exists purely to widen track results; a search that
    asks for no tracks must not pay for it."""
    captured = []

    def _search(query, filter, limit):
        captured.append(filter)
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(
        provider.search("foo", [MediaType.ARTIST, MediaType.PLAYLIST], limit=3)
    )
    assert captured == ["artists", "playlists"]


def test_search_multi_type_runs_filtered_call_per_type(provider):
    """Multi-type search issues one filtered call per type, not one unfiltered
    call. An unfiltered YTM search skews to songs/videos, so artists and
    playlists rarely surface (issue #18). The extra videos pass for tracks
    (issue #77) always runs last, so catalog songs rank above videos in the
    merged track list."""
    captured = []

    def _search(query, filter, limit):
        captured.append(filter)
        return []

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    asyncio.run(
        provider.search(
            "foo",
            [MediaType.ARTIST, MediaType.ALBUM, MediaType.TRACK, MediaType.PLAYLIST],
            limit=3,
        )
    )
    assert captured == ["artists", "albums", "songs", "playlists", "videos"]
    assert None not in captured


def test_search_one_failing_filter_does_not_sink_others(provider):
    """A filter that raises is logged and skipped; the rest still return."""

    def _search(query, filter, limit):
        if filter == "artists":
            raise RuntimeError("boom")
        if filter != "songs":
            return []
        return [
            {
                "resultType": "song",
                "videoId": "vid1",
                "title": "Song",
                "artists": [{"id": "UCart", "name": "A"}],
            }
        ]

    mock = MagicMock()
    mock.search = _search
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.ARTIST, MediaType.TRACK]))
    assert len(results.artists) == 0
    assert len(results.tracks) == 1


def test_search_parses_returned_items_by_result_type(provider):
    # Each filtered call returns only its own category, as real YTM does. The
    # provider runs one call per type and merges them (issue #18).
    by_filter = {
        "artists": [{"resultType": "artist", "channelId": "UCart", "name": "Some Artist"}],
        "songs": [
            {
                "resultType": "song",
                "videoId": "vid1",
                "title": "Song",
                "artists": [{"id": "UCart", "name": "Some Artist"}],
            }
        ],
        "albums": [
            {
                "resultType": "album",
                "browseId": "MPREb_x",
                "title": "Album",
                "artists": [{"id": "UCart", "name": "Some Artist"}],
                "type": "Album",
            }
        ],
        "playlists": [{"resultType": "playlist", "browseId": "VLPLx", "title": "Playlist"}],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search(
            "foo",
            [MediaType.ARTIST, MediaType.TRACK, MediaType.ALBUM, MediaType.PLAYLIST],
        )
    )
    assert len(results.artists) == 1
    assert len(results.tracks) == 1
    assert len(results.albums) == 1
    assert len(results.playlists) == 1


def test_search_skips_invalid_items(provider):
    """An item missing a required field should be skipped, not crash the search."""
    songs = [
        # No videoId â€” should be silently skipped.
        {
            "resultType": "song",
            "title": "broken",
            "artists": [{"id": "UCart", "name": "A"}],
        },
        {
            "resultType": "song",
            "videoId": "good",
            "title": "ok",
            "artists": [{"id": "UCart", "name": "A"}],
        },
    ]
    mock = MagicMock()
    mock.search = MagicMock(
        side_effect=lambda query, filter, limit: songs if filter == "songs" else []
    )
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK]))
    assert len(results.tracks) == 1
    assert results.tracks[0].item_id == "good"


def test_search_merges_videos_below_catalog_songs(provider):
    """Video results surface in track search but rank below catalog songs,
    regardless of relevance ordering within each tab (issue #77)."""
    by_filter = {
        "songs": [
            {
                "resultType": "song",
                "videoId": "song1",
                "title": "Catalog Song",
                "artists": [{"id": "UCart", "name": "A"}],
            }
        ],
        "videos": [
            {
                "resultType": "video",
                "videoId": "vid1",
                "title": "Live in Madrid",
                "videoType": "MUSIC_VIDEO_TYPE_UGC",
                "artists": [{"id": None, "name": "Some Channel"}],
            }
        ],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK]))
    assert [t.item_id for t in results.tracks] == ["song1", "vid1"]


def test_search_dedupes_same_video_id_across_filters(provider):
    """The songs and videos tabs should never share an id, but the backend has
    handed out counterpart ids before (ytmusicapi #739); a repeat must not
    become a duplicate row."""
    same = {
        "resultType": "song",
        "videoId": "dup1",
        "title": "Song",
        "artists": [{"id": "UCart", "name": "A"}],
    }
    mock = MagicMock()
    mock.search = MagicMock(return_value=[same])
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK]))
    assert [t.item_id for t in results.tracks] == ["dup1"]


def test_search_skips_podcast_episodes_from_videos_filter(provider):
    """The videos filter forces resultType "video" onto the podcast episodes
    it returns; only videoType tells them apart. Episodes belong to the
    podcast domain, not in track results."""
    by_filter = {
        "songs": [],
        "videos": [
            {
                "resultType": "video",
                "videoId": "ep1",
                "title": "Some Episode",
                "videoType": "MUSIC_VIDEO_TYPE_PODCAST_EPISODE",
                "artists": [{"id": None, "name": "Some Show"}],
            },
            {
                "resultType": "video",
                "videoId": "vid1",
                "title": "Actual Video",
                "videoType": "MUSIC_VIDEO_TYPE_OMV",
                "artists": [{"id": "UCart", "name": "A"}],
            },
        ],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK]))
    assert [t.item_id for t in results.tracks] == ["vid1"]


def test_search_parses_video_without_duration(provider):
    """A live or ongoing upload shows no duration: ytmusicapi then returns
    "duration": None and omits "duration_seconds" entirely. The result must
    still parse instead of being dropped."""
    by_filter = {
        "songs": [],
        "videos": [
            {
                "resultType": "video",
                "videoId": "live1",
                "title": "Concert Stream",
                "videoType": "MUSIC_VIDEO_TYPE_UGC",
                "duration": None,
                "artists": [{"id": None, "name": "Some Channel"}],
            }
        ],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK]))
    assert [t.item_id for t in results.tracks] == ["live1"]
    assert results.tracks[0].duration == 0


def test_search_keeps_video_without_artists(provider):
    """A videos-tab result sometimes carries no artists key at all; those are
    exactly the plain-YouTube uploads issue #77 surfaces, so they must fall
    back to a placeholder artist instead of being dropped as malformed."""
    by_filter = {
        "songs": [],
        "videos": [
            {
                "resultType": "video",
                "videoId": "noart1",
                "title": "Bootleg Concert",
                "videoType": "MUSIC_VIDEO_TYPE_UGC",
            }
        ],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK]))
    assert [t.item_id for t in results.tracks] == ["noart1"]
    assert [a.name for a in results.tracks[0].artists] == ["Unknown Artist"]


def test_search_song_without_artists_still_skipped(provider):
    """The artist fallback is scoped to videos-tab results; a catalog song
    with no artists is malformed data and stays dropped."""
    songs = [
        {
            "resultType": "song",
            "videoId": "bad1",
            "title": "Broken Catalog Row",
        }
    ]
    mock = MagicMock()
    mock.search = MagicMock(
        side_effect=lambda query, filter, limit: songs if filter == "songs" else []
    )
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK]))
    assert results.tracks == []


def test_search_reserves_window_space_for_videos(provider):
    """ytmusicapi treats limit as a floor, so the songs tab alone fills MA's
    merged [:limit] window; without a reservation the appended videos would be
    truncated away before ranking and issue #77 would silently persist. Songs
    must be capped so at least one video survives inside the window."""

    def _song(i):
        return {
            "resultType": "song",
            "videoId": f"s{i}",
            "title": f"Song {i}",
            "artists": [{"id": "UCart", "name": "A"}],
        }

    by_filter = {
        "songs": [_song(i) for i in range(8)],
        "videos": [
            {
                "resultType": "video",
                "videoId": "v0",
                "title": "Live in Madrid",
                "videoType": "MUSIC_VIDEO_TYPE_UGC",
                "artists": [{"id": None, "name": "Some Channel"}],
            },
            {
                "resultType": "video",
                "videoId": "v1",
                "title": "Live in Berlin",
                "videoType": "MUSIC_VIDEO_TYPE_UGC",
                "artists": [{"id": None, "name": "Some Channel"}],
            },
        ],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(provider.search("foo", [MediaType.TRACK], limit=4))
    # limit 4 with 2 videos available reserves one slot: three songs, then the
    # first video, nothing beyond the window.
    assert [t.item_id for t in results.tracks] == ["s0", "s1", "s2", "v0"]


def _catalog_song(i, title="Song"):
    return {
        "resultType": "song",
        "videoId": f"s{i}",
        "title": f"{title} {i}",
        "artists": [{"id": "UCart", "name": "A"}],
    }


def _ugc_video(video_id, title):
    return {
        "resultType": "video",
        "videoId": video_id,
        "title": title,
        "videoType": "MUSIC_VIDEO_TYPE_UGC",
        "artists": [{"id": None, "name": "Some Channel"}],
    }


def test_search_promotes_video_matching_query_better_than_songs(provider):
    """The recording issue #77 is about, reproduced end to end.

    Ranking every video behind the songs hides it: Music Assistant merges the
    per-provider results into one capped window, so with a second provider
    enabled only the head of this list is ever seen. A video that covers the
    whole query while the catalog only fuzzy-matches two of its words has to
    come first."""
    by_filter = {
        "songs": [
            {
                "resultType": "song",
                "videoId": "s0",
                "title": "7 Seconds",
                "artists": [{"id": "UCart", "name": "Youssou N'Dour"}],
            },
            *[_catalog_song(i) for i in range(1, 8)],
        ],
        "videos": [
            _ugc_video(
                "0pquBdxfsXY",
                "PORRIDGE RADIO - 7 SECONDS. Live in Madrid (19-3-25). Sala Copernico Live.",
            )
        ],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("porridge radio 7 seconds madrid", [MediaType.TRACK], limit=4)
    )
    ids = [t.item_id for t in results.tracks]
    assert ids[0] == "0pquBdxfsXY"
    # the catalog still fills the rest of the window, it is only outranked
    assert ids == ["0pquBdxfsXY", "s0", "s1", "s2"]


def test_search_keeps_weakly_matching_videos_below_songs(provider):
    """Promotion is the exception. A video that matches the query no better
    than the catalog does stays below the songs, which is where it belongs for
    the ordinary case of the catalog holding the recording the user meant."""
    by_filter = {
        "songs": [_catalog_song(i, title="Catalog Song") for i in range(8)],
        "videos": [_ugc_video("v0", "Catalog Song 0 (Live in Madrid)")],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(provider.search("catalog song 0", [MediaType.TRACK], limit=4))
    assert [t.item_id for t in results.tracks] == ["s0", "s1", "s2", "v0"]


def test_search_promoted_videos_never_evict_every_song(provider):
    """Promotion may only take the window slots the songs were not going to
    use. A query where every video outranks the catalog must not turn a music
    search into a wall of YouTube uploads."""
    by_filter = {
        "songs": [_catalog_song(i) for i in range(8)],
        "videos": [_ugc_video(f"v{i}", f"Live in Madrid {i}") for i in range(5)],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(provider.search("live in madrid", [MediaType.TRACK], limit=4))
    ids = [t.item_id for t in results.tracks]
    assert ids[0] == "v0"
    assert ids[1:] == ["s0", "s1", "s2"]


def test_search_does_not_reorder_videos_when_no_songs_match(provider):
    """With no songs to outrank there is nothing to promote past, so the crude
    token score never gets to second-guess the order YTM returned."""
    by_filter = {
        "songs": [],
        "videos": [
            _ugc_video("v0", "Something Unrelated"),
            _ugc_video("v1", "Live in Madrid"),
        ],
    }
    mock = MagicMock()
    mock.search = MagicMock(side_effect=lambda query, filter, limit: by_filter.get(filter, []))
    provider._ytmusic = mock
    results = asyncio.run(provider.search("live in madrid", [MediaType.TRACK], limit=4))
    assert [t.item_id for t in results.tracks] == ["v0", "v1"]


def test_split_promoted_videos_requires_a_clear_margin(provider):
    """A video only outranks the songs when it covers the query substantially
    better, so a video that ties the best song keeps its place behind it."""
    songs = [provider._parse_track(_catalog_song(0, title="Live in Madrid"))]
    tie = provider._parse_track(_ugc_video("tie", "Live in Madrid"))
    better = provider._parse_track(_ugc_video("better", "Live in Madrid 0 Full Concert"))
    promoted, weaker = provider._split_promoted_videos("live in madrid 0", songs, [tie, better])
    assert [t.item_id for t in promoted] == []
    assert [t.item_id for t in weaker] == ["tie", "better"]


def test_query_match_score_treats_punctuation_as_separators(provider):
    """Upload titles are full of dashes and dots; they must not stop the words
    the user typed from matching."""
    track = provider._parse_track(_ugc_video("v0", "7 SECONDS. Live in Madrid (19-3-25)."))
    assert ytm._query_match_score({"madrid", "19"}, track) == 1.0
    assert ytm._query_match_score({"madrid", "berlin"}, track) == 0.5
    assert ytm._query_match_score(set(), track) == 0.0


# ---------------------------------------------------------------------------
# Search by pasted URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, expected",
    [
        # Single videos / songs
        ("https://music.youtube.com/watch?v=abc123", ("track", "abc123")),
        ("https://www.youtube.com/watch?v=abc123", ("track", "abc123")),
        ("https://m.youtube.com/watch?v=abc123", ("track", "abc123")),
        ("https://youtu.be/abc123", ("track", "abc123")),
        ("https://youtu.be/abc123?si=xyz", ("track", "abc123")),
        # v + list together resolves to the track, not the playlist
        ("https://music.youtube.com/watch?v=abc123&list=PLxyz", ("track", "abc123")),
        ("https://www.youtube.com/watch?v=abc123&t=42s&feature=share", ("track", "abc123")),
        # Playlists
        ("https://music.youtube.com/playlist?list=PLxyz", ("playlist", "PLxyz")),
        ("https://www.youtube.com/playlist?list=PLxyz", ("playlist", "PLxyz")),
        # Bare ?list= with no path still means a playlist
        ("https://www.youtube.com/?list=PLxyz", ("playlist", "PLxyz")),
        # Lenient: missing scheme
        ("youtu.be/abc123", ("track", "abc123")),
        ("music.youtube.com/watch?v=abc123", ("track", "abc123")),
        # Not URLs / not YouTube
        ("just a search query", None),
        ("https://example.com/watch?v=abc123", None),
        ("https://spotify.com/track/abc", None),
        ("", None),
        ("   ", None),
        # YouTube host but no resolvable id
        ("https://music.youtube.com/", None),
        ("https://www.youtube.com/watch", None),
    ],
)
def test_parse_youtube_url(provider, query, expected):
    assert provider._parse_youtube_url(query) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        # MA's search controller does: query.replace("/", " ").replace("'", "")
        # before calling the provider, destroying "://" and path separators.
        # These are the exact forms the provider actually receives.
        ("https:  www.youtube.com watch?v=S33tWZqXhnk", ("track", "S33tWZqXhnk")),
        ("https:  music.youtube.com watch?v=S33tWZqXhnk", ("track", "S33tWZqXhnk")),
        ("https:  m.youtube.com watch?v=S33tWZqXhnk", ("track", "S33tWZqXhnk")),
        # v + list together still resolves to the track
        ("https:  music.youtube.com watch?v=S33tWZqXhnk&list=PLabc", ("track", "S33tWZqXhnk")),
        ("https:  www.youtube.com watch?v=S33tWZqXhnk list=PLabc", ("track", "S33tWZqXhnk")),
        # mangled playlist URL
        ("https:  music.youtube.com playlist?list=PLabcdefghij", ("playlist", "PLabcdefghij")),
        ("https:  www.youtube.com playlist?list=PLabcdefghij", ("playlist", "PLabcdefghij")),
        # mangled youtu.be short link
        ("https:  youtu.be S33tWZqXhnk", ("track", "S33tWZqXhnk")),
        # a youtube host token but no valid id -> not a link
        ("a song called youtube.com is great", None),
        ("youtube.com", None),
        # ordinary text searches must not be hijacked
        ("the youtuber song", None),
        ("watch v in the dark", None),
    ],
)
def test_parse_youtube_url_handles_ma_mangled_query(provider, query, expected):
    """MA strips '/' from the query before calling search(); the parser must
    still recover the id from that sanitized form."""
    assert provider._parse_youtube_url(query) == expected


def test_search_with_ma_mangled_video_url_returns_track(provider):
    """End-to-end: the de-slashed query MA actually delivers still resolves."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={"videoDetails": {"videoId": "S33tWZqXhnk", "title": "x", "author": "a"}}
    )
    mock.search = MagicMock(return_value=[])
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https:  www.youtube.com watch?v=S33tWZqXhnk", [MediaType.TRACK])
    )
    assert results.tracks[0].item_id == "S33tWZqXhnk"


def test_search_with_video_url_returns_track_first(provider):
    """Pasting a watch URL resolves the video to a Track placed first."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "abc123",
                "title": "Pasted Song",
                "lengthSeconds": "180",
                "author": "Some Uploader",
                "thumbnail": {"thumbnails": []},
            }
        }
    )
    mock.search = MagicMock(return_value=[])
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://music.youtube.com/watch?v=abc123", [MediaType.TRACK])
    )
    assert results.tracks[0].item_id == "abc123"
    assert len(results.playlists) == 0


def test_search_with_video_url_runs_name_search_on_title(provider):
    """The other results come from a text search on the resolved video title."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "abc123",
                "title": "Pasted Song",
                "author": "Some Uploader",
            }
        }
    )

    def _search(query, filter=None, limit=5):  # noqa: A002
        # The text search must use the resolved title, never the raw URL.
        assert query == "Pasted Song"
        if filter == "songs":
            return [
                {
                    "resultType": "song",
                    "videoId": "related1",
                    "title": "Related",
                    "artists": [{"id": "UCx", "name": "A"}],
                }
            ]
        return []

    mock.search = MagicMock(side_effect=_search)
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://music.youtube.com/watch?v=abc123", [MediaType.TRACK])
    )
    mock.search.assert_called()
    assert results.tracks[0].item_id == "abc123"  # raw video first
    assert any(t.item_id == "related1" for t in results.tracks)


def test_search_with_video_url_dedupes_raw_video(provider):
    """The pasted video isn't listed twice if it also surfaces in name search."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={"videoDetails": {"videoId": "abc123", "title": "Song", "author": "a"}}
    )
    mock.search = MagicMock(
        return_value=[
            {
                "resultType": "song",
                "videoId": "abc123",
                "title": "Song",
                "artists": [{"id": "UCx", "name": "A"}],
            }
        ]
    )
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://music.youtube.com/watch?v=abc123", [MediaType.TRACK])
    )
    assert [t.item_id for t in results.tracks] == ["abc123"]


def test_search_with_url_ignores_media_types_filter(provider):
    """An explicit link resolves even when its type isn't in media_types."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={"videoDetails": {"videoId": "abc123", "title": "x", "author": "a"}}
    )
    mock.search = MagicMock(return_value=[])
    provider._ytmusic = mock
    # Searching only for ALBUM, but pasting a song link -> still returns the track.
    results = asyncio.run(
        provider.search("https://youtu.be/abc123", [MediaType.ALBUM])
    )
    assert len(results.tracks) == 1
    assert results.tracks[0].item_id == "abc123"


def test_search_with_non_music_video_falls_back_to_minimal_track(provider):
    """A plain youtube.com video whose get_song fails still yields a playable track."""
    mock = MagicMock()
    mock.get_song = MagicMock(side_effect=RuntimeError("not a music catalog item"))
    mock.search = MagicMock(side_effect=AssertionError("text search must not run for a URL"))
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://www.youtube.com/watch?v=randomvid", [MediaType.TRACK])
    )
    assert len(results.tracks) == 1
    assert results.tracks[0].item_id == "randomvid"


def test_search_with_playlist_url_returns_single_playlist(provider):
    """Pasting a playlist URL resolves it to one Playlist via get_playlist."""
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={
            "id": "PLxyz",
            "title": "Pasted Playlist",
            "owner": "Someone",
        }
    )
    mock.search = MagicMock(side_effect=AssertionError("text search must not run for a URL"))
    provider._ytmusic = mock
    results = asyncio.run(
        provider.search("https://music.youtube.com/playlist?list=PLxyz", [MediaType.PLAYLIST])
    )
    assert len(results.playlists) == 1
    assert results.playlists[0].item_id == "PLxyz"
    assert len(results.tracks) == 0


def test_search_with_url_resolution_failure_returns_empty(provider):
    """If URL resolution raises, search returns empty results rather than erroring."""
    mock = MagicMock()
    mock.get_playlist = MagicMock(side_effect=RuntimeError("boom"))
    provider._ytmusic = mock

    # yt-dlp fallback path also fails -> _search_by_url swallows and returns empty.
    async def _boom(_playlist_id, _seed=None):
        raise RuntimeError("boom")

    provider._get_playlist_via_ytdlp = _boom
    results = asyncio.run(
        provider.search("https://music.youtube.com/playlist?list=PLbad", [MediaType.PLAYLIST])
    )
    assert len(results.playlists) == 0
    assert len(results.tracks) == 0


# ---------------------------------------------------------------------------
# Trim timestamps (@start-end)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token, expected",
    [
        ("15", 15),
        ("0:15", 15),
        ("3:42", 222),
        ("1:02:03", 3723),
        ("1m30s", 90),
        ("2h", 7200),
        ("90s", 90),
        ("", None),
        ("   ", None),
        ("abc", None),
        ("1:2:3:4", None),
    ],
)
def test_parse_timestamp(token, expected):
    assert ytm._parse_timestamp(token) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        ("https://youtu.be/abc123 @15-222", ("https://youtu.be/abc123", 15, 222)),
        ("https://youtu.be/abc123 @0:15-3:42", ("https://youtu.be/abc123", 15, 222)),
        ("https://youtu.be/abc123 @15-", ("https://youtu.be/abc123", 15, None)),
        ("https://youtu.be/abc123 @-3:42", ("https://youtu.be/abc123", None, 222)),
        ("https://youtu.be/abc123@15-222", ("https://youtu.be/abc123", 15, 222)),
        # No spec / unparseable spec -> query untouched, no bounds.
        ("https://youtu.be/abc123", ("https://youtu.be/abc123", None, None)),
        ("an email a@b thing", ("an email a@b thing", None, None)),
        # start >= end is nonsensical -> bounds ignored, but the recognized
        # "@start-end" suffix is still stripped so URL resolution stays clean.
        ("https://youtu.be/abc123 @3:42-0:15", ("https://youtu.be/abc123", None, None)),
    ],
)
def test_split_trim_spec(query, expected):
    assert ytm._split_trim_spec(query) == expected


@pytest.mark.parametrize(
    "video_id, start, end, encoded",
    [
        ("abc12345678", None, None, "abc12345678"),
        ("abc12345678", 15, 222, "abc12345678@15-222"),
        ("abc12345678", 15, None, "abc12345678@15-"),
        ("abc12345678", None, 222, "abc12345678@-222"),
    ],
)
def test_encode_split_track_id_roundtrip(video_id, start, end, encoded):
    assert ytm._encode_track_id(video_id, start, end) == encoded
    assert ytm._split_track_id(encoded) == (video_id, start, end)


def test_split_track_id_plain():
    assert ytm._split_track_id("abc12345678") == ("abc12345678", None, None)


def test_get_similar_tracks_strips_trim_suffix(provider):
    """Song radio on a trimmed track must query YTM with the bare video id."""
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(return_value={"tracks": []})
    provider._ytmusic = mock
    asyncio.run(provider.get_similar_tracks("abc12345678@15-222"))
    assert mock.get_watch_playlist.call_args.kwargs["videoId"] == "abc12345678"


def test_parse_youtube_url_encodes_trim(provider):
    """A pasted link with a trim spec resolves to an encoded track id."""
    assert provider._parse_youtube_url("https://youtu.be/abc12345678 @15-222") == (
        "track",
        "abc12345678@15-222",
    )
    # Playlists ignore the trim spec.
    assert provider._parse_youtube_url(
        "https://music.youtube.com/playlist?list=PLabcdefghij @15-222"
    ) == ("playlist", "PLabcdefghij")


def test_get_track_with_trim_encodes_id_and_duration(provider):
    """get_track queries the bare id but returns an encoded, trimmed Track."""
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "abc12345678",
                "title": "Song",
                "lengthSeconds": "300",
                "author": "a",
            }
        }
    )
    provider._ytmusic = mock
    track = asyncio.run(provider.get_track("abc12345678@15-222"))
    # Bare id used for the API lookup.
    mock.get_song.assert_called_once_with("abc12345678")
    # Encoded id persists on the track and its provider mapping.
    assert track.item_id == "abc12345678@15-222"
    assert all(m.item_id == "abc12345678@15-222" for m in track.provider_mappings)
    # Watch URL still uses the bare id.
    assert all("watch?v=abc12345678" in m.url for m in track.provider_mappings)
    # Duration reflects the trimmed window.
    assert track.duration == 222 - 15


def test_minimal_track_with_trim_keeps_encoded_id(provider):
    track = provider._minimal_track("abc12345678@15-222")
    assert track.item_id == "abc12345678@15-222"
    assert all("watch?v=abc12345678" in m.url for m in track.provider_mappings)
    assert track.name == "abc12345678"


def test_get_stream_details_adds_trim_args(provider):
    async def _fmt(video_id):
        assert video_id == "abc12345678"  # bare id reaches yt-dlp
        return {"url": "https://stream.example/x", "ext": "m4a"}

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678@15-222", MediaType.TRACK))
    assert sd.item_id == "abc12345678@15-222"
    assert sd.extra_input_args == ["-ss", "15", "-t", str(222 - 15)]
    assert sd.duration == 222 - 15


def test_get_stream_details_open_ended_start_only(provider):
    async def _fmt(video_id):
        return {"url": "https://stream.example/x", "ext": "m4a"}

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678@15-", MediaType.TRACK))
    assert sd.extra_input_args == ["-ss", "15"]


def test_get_stream_details_no_trim_has_no_args(provider):
    async def _fmt(video_id):
        return {"url": "https://stream.example/x", "ext": "m4a"}

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678", MediaType.TRACK))
    assert sd.extra_input_args == []


# ---------------------------------------------------------------------------
# Bounded-range streaming
#
# googlevideo answers 403 to any fetch whose Range header is missing or
# open-ended, while a bounded range against the same URL answers 206 (the
# rqh=1 parameter; enforcement observed 2026-08-19, every install at once,
# extraction still green). ffmpeg only fetches the unbounded way, so the
# provider stopped handing it the URL: get_stream_details reports CUSTOM and
# get_audio_stream pulls the bytes itself in bounded chunks.
# ---------------------------------------------------------------------------


class _RangeContent:
    def __init__(self, body: bytes):
        self._body = body

    def iter_chunked(self, size: int):
        async def _gen():
            for i in range(0, len(self._body), size):
                yield self._body[i : i + size]

        return _gen()


class _RangeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None):
        self.status = status
        self.headers = headers or {}
        self.content = _RangeContent(body)

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeGoogleVideo:
    """Serve a blob the way googlevideo now does: bounded Range or nothing.

    ``requests`` records every (url, headers) pair so tests can assert what
    was actually sent, not just what came back.
    """

    def __init__(
        self,
        blob: bytes,
        *,
        ignore_range: bool = False,
        send_content_range: bool = True,
        truncate_first: int | None = None,
        max_range: int | None = None,
    ):
        self.blob = blob
        self.ignore_range = ignore_range
        self.send_content_range = send_content_range
        self.truncate_first = truncate_first
        # The size cap: ranges asking for more than this many bytes answer
        # 403, the way googlevideo does since 2026-08 (1 MiB passed, 2 MiB
        # did not, measured against a real ANDROID_VR URL).
        self.max_range = max_range
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, headers=None, **_kwargs):
        headers = dict(headers or {})
        self.requests.append((url, headers))
        if self.ignore_range:
            return _RangeResponse(200, self.blob)
        rng = headers.get("Range", "")
        if not rng.startswith("bytes=") or rng.endswith("-"):
            return _RangeResponse(403)
        start_s, end_s = rng[len("bytes=") :].split("-")
        start, end = int(start_s), int(end_s)
        if self.max_range is not None and (end - start + 1) > self.max_range:
            return _RangeResponse(403)
        if start >= len(self.blob):
            return _RangeResponse(416)
        chunk = self.blob[start : end + 1]
        if self.truncate_first is not None and len(self.requests) == 1:
            chunk = chunk[: self.truncate_first]
        response_headers = {}
        if self.send_content_range:
            response_headers["Content-Range"] = (
                f"bytes {start}-{start + len(chunk) - 1}/{len(self.blob)}"
            )
        return _RangeResponse(206, chunk, response_headers)


def _stream_all(provider, sd) -> bytes:
    async def _collect():
        return b"".join([part async for part in provider.get_audio_stream(sd)])

    return asyncio.run(_collect())


def _custom_stream_details(url="https://stream.example/x", headers=None):
    from music_assistant_models.streamdetails import StreamDetails

    return StreamDetails(
        provider="test", item_id="vid42", data={"url": url, "headers": headers or {}}
    )


def _mass_with_session(session):
    from types import SimpleNamespace

    return SimpleNamespace(http_session=session)


def test_get_stream_details_is_custom_and_carries_url_and_headers(provider):
    """The URL never reaches ffmpeg; it travels to get_audio_stream via data."""

    async def _fmt(video_id):
        return {
            "url": "https://stream.example/x",
            "ext": "m4a",
            "http_headers": {"User-Agent": "com.google.android.apps.youtube.vr"},
        }

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678", MediaType.TRACK))
    assert sd.stream_type == StreamType.CUSTOM
    assert sd.data == {
        "url": "https://stream.example/x",
        "headers": {"User-Agent": "com.google.android.apps.youtube.vr"},
    }
    # Kept for logging and the live canary, not for playback.
    assert sd.path == "https://stream.example/x"
    # Seeking stays with ffmpeg: a WebM stream started at a byte offset has no
    # container headers and cannot be parsed.
    assert sd.can_seek is False
    assert sd.allow_seek is True


def test_get_audio_stream_fetches_in_bounded_ranges(provider, monkeypatch):
    monkeypatch.setattr(ytm, "STREAM_CHUNK_SIZE", 5)
    blob = b"0123456789ab"
    session = _FakeGoogleVideo(blob)
    provider.mass = _mass_with_session(session)

    out = _stream_all(provider, _custom_stream_details())

    assert out == blob
    ranges = [h["Range"] for _, h in session.requests]
    # Every request bounded, contiguous, and capped to the Content-Range total.
    assert ranges == ["bytes=0-4", "bytes=5-9", "bytes=10-11"]


def test_get_audio_stream_sends_the_minting_clients_headers(provider, monkeypatch):
    """Every chunk goes out with yt-dlp's per-format headers plus the range."""
    monkeypatch.setattr(ytm, "STREAM_CHUNK_SIZE", 5)
    session = _FakeGoogleVideo(b"0123456789")
    provider.mass = _mass_with_session(session)

    _stream_all(
        provider, _custom_stream_details(headers={"User-Agent": "android-vr"})
    )

    assert session.requests, "no request was made at all"
    for _, headers in session.requests:
        assert headers["User-Agent"] == "android-vr"
        assert "Range" in headers


def test_get_audio_stream_raises_unplayable_on_403(provider):
    class _Deny:
        def get(self, url, headers=None, **_kwargs):
            return _RangeResponse(403)

    provider.mass = _mass_with_session(_Deny())
    with pytest.raises(UnplayableMediaError):
        _stream_all(provider, _custom_stream_details())


def test_get_audio_stream_handles_a_server_that_ignores_range(provider, monkeypatch):
    """A 200 means the whole body already streamed; asking again would replay it."""
    monkeypatch.setattr(ytm, "STREAM_CHUNK_SIZE", 5)
    blob = b"0123456789ab"
    session = _FakeGoogleVideo(blob, ignore_range=True)
    provider.mass = _mass_with_session(session)

    out = _stream_all(provider, _custom_stream_details())

    assert out == blob
    assert len(session.requests) == 1


def test_get_audio_stream_stops_without_content_range(provider, monkeypatch):
    """No total to trust, so a short chunk is the only end-of-file signal."""
    monkeypatch.setattr(ytm, "STREAM_CHUNK_SIZE", 5)
    blob = b"0123456789ab"
    session = _FakeGoogleVideo(blob, send_content_range=False)
    provider.mass = _mass_with_session(session)

    out = _stream_all(provider, _custom_stream_details())

    assert out == blob
    assert len(session.requests) == 3  # 5 + 5 + short 2, then stop


def test_get_audio_stream_treats_416_as_the_end(provider, monkeypatch):
    """A blob that is an exact multiple of the chunk size never answers short.

    Without a Content-Range total the loop only finds the end by asking past
    it, and googlevideo says 416 to that, not 206-with-nothing.
    """
    monkeypatch.setattr(ytm, "STREAM_CHUNK_SIZE", 5)
    blob = b"0123456789"  # exactly two chunks
    session = _FakeGoogleVideo(blob, send_content_range=False)
    provider.mass = _mass_with_session(session)

    out = _stream_all(provider, _custom_stream_details())

    assert out == blob
    assert [h["Range"] for _, h in session.requests] == [
        "bytes=0-4",
        "bytes=5-9",
        "bytes=10-14",
    ]


def test_get_audio_stream_resumes_a_truncated_chunk(provider, monkeypatch):
    """A connection drop mid-chunk resumes from the last byte received.

    Skipping the gap would splice two distant parts of the file together,
    which decodes as an audible glitch rather than an error.
    """
    monkeypatch.setattr(ytm, "STREAM_CHUNK_SIZE", 5)
    blob = b"0123456789ab"
    session = _FakeGoogleVideo(blob, truncate_first=3)
    provider.mass = _mass_with_session(session)

    out = _stream_all(provider, _custom_stream_details())

    assert out == blob
    assert [h["Range"] for _, h in session.requests][:2] == ["bytes=0-4", "bytes=3-7"]


def test_get_audio_stream_halves_the_chunk_when_the_size_cap_moves(provider, monkeypatch):
    """The cap has moved once already (10 MiB worked, then 403d).

    A server that refuses the configured chunk size must degrade playback to
    smaller requests, not kill it: the provider halves down to the floor and
    retries the same position, so nothing is skipped and nothing repeats.
    """
    monkeypatch.setattr(ytm, "STREAM_CHUNK_SIZE", 16)
    monkeypatch.setattr(ytm, "STREAM_CHUNK_FLOOR", 4)
    blob = bytes(range(24))
    session = _FakeGoogleVideo(blob, max_range=5)  # 16 -> 8 -> 4 allowed
    provider.mass = _mass_with_session(session)

    out = _stream_all(provider, _custom_stream_details())

    assert out == blob
    ranges = [h["Range"] for _, h in session.requests]
    # Two refused probes, then 4-byte chunks all the way from position 0.
    assert ranges[:3] == ["bytes=0-15", "bytes=0-7", "bytes=0-3"]
    assert all(r.startswith("bytes=") for r in ranges)
    assert len(ranges) == 2 + len(blob) // 4


def test_get_audio_stream_gives_up_only_below_the_floor(provider, monkeypatch):
    """A 403 that survives the floor is a dead URL, not a size problem."""
    monkeypatch.setattr(ytm, "STREAM_CHUNK_SIZE", 16)
    monkeypatch.setattr(ytm, "STREAM_CHUNK_FLOOR", 4)
    session = _FakeGoogleVideo(bytes(24), max_range=2)  # even the floor 403s
    provider.mass = _mass_with_session(session)

    with pytest.raises(UnplayableMediaError):
        _stream_all(provider, _custom_stream_details())
    assert [h["Range"] for _, h in session.requests] == [
        "bytes=0-15",
        "bytes=0-7",
        "bytes=0-3",
    ]


def test_content_range_total_parses_and_rejects():
    assert ytm._content_range_total("bytes 0-1023/16492447") == 16492447
    assert ytm._content_range_total("bytes 0-1023/*") is None
    assert ytm._content_range_total("") is None
    assert ytm._content_range_total(None) is None
    assert ytm._content_range_total("garbage") is None


# ---------------------------------------------------------------------------
# Pre-roll ad window (issue #51)
#
# YouTube serves some tracks behind a pre-roll ad, and the media URL it hands
# back is not valid until that window has passed: fetching early returns 403.
# yt-dlp reports the window as ``available_at`` and its own downloader sleeps
# until then. The provider hands the URL to Music Assistant instead, so it has
# to do the waiting itself or the fetch fails and the track is skipped.
# ---------------------------------------------------------------------------


def test_preroll_wait_is_zero_when_the_field_is_absent():
    """An older yt-dlp predating the field has to keep working."""
    assert ytm._preroll_wait_seconds({"url": "https://stream.example/x"}) == 0.0


@pytest.mark.parametrize("value", [None, 0, "", False])
def test_preroll_wait_is_zero_for_empty_values(value):
    assert ytm._preroll_wait_seconds({"available_at": value}) == 0.0


def test_preroll_wait_is_zero_when_the_window_has_passed():
    assert ytm._preroll_wait_seconds({"available_at": 990.0}, now=1000.0) == 0.0


def test_preroll_wait_is_zero_at_the_exact_boundary():
    assert ytm._preroll_wait_seconds({"available_at": 1000.0}, now=1000.0) == 0.0


def test_preroll_wait_returns_the_remaining_window():
    assert ytm._preroll_wait_seconds({"available_at": 1005.0}, now=1000.0) == 5.0


def test_preroll_wait_is_reported_uncapped():
    """The caller needs the real figure to tell a long wait from an absurd one."""
    wait = ytm._preroll_wait_seconds({"available_at": 1000.0 + 500}, now=1000.0)
    assert wait == 500.0
    assert wait > ytm.MAX_PREROLL_WAIT


@pytest.mark.parametrize("value", ["soon", 10**400, object(), [1], {"a": 1}])
def test_preroll_wait_survives_an_unparseable_value(value):
    """Never raise out of the stream path over a field we only advise on.

    ``10**400`` is the interesting one: ``float()`` raises OverflowError on it,
    which is an ArithmeticError rather than a ValueError and so needs catching
    explicitly.
    """
    assert ytm._preroll_wait_seconds({"available_at": value}, now=1000.0) == 0.0


def test_preroll_wait_takes_the_latest_of_a_merged_format():
    """Mirrors yt-dlp's own max() over ``requested_formats``.

    A merged format carries the timestamps on its parts, and the URL is only
    good once the latest-gated part is available.
    """
    fmt = {
        "requested_formats": [
            {"available_at": 1003.0},
            {"available_at": 1007.0},
        ]
    }
    assert ytm._preroll_wait_seconds(fmt, now=1000.0) == 7.0


def test_preroll_wait_ignores_junk_entries_in_requested_formats():
    fmt = {"requested_formats": [None, "nonsense", {"available_at": 1004.0}]}
    assert ytm._preroll_wait_seconds(fmt, now=1000.0) == 4.0


def test_get_stream_details_waits_out_the_preroll_window(provider, monkeypatch):
    """The actual issue #51 guard: without this the URL 403s on arrival."""
    slept = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ytm.asyncio, "sleep", _fake_sleep)

    async def _fmt(video_id):
        return {
            "url": "https://stream.example/x",
            "ext": "m4a",
            "available_at": time.time() + 5,
        }

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678", MediaType.TRACK))

    assert slept, (
        "the provider handed over the url without waiting for the pre-roll "
        "window, which is exactly the 403 in issue #51"
    )
    assert 4.0 < slept[0] <= 5.0
    assert sd.path == "https://stream.example/x"


def test_get_stream_details_does_not_wait_without_a_preroll(provider, monkeypatch):
    """The common path stays instant; a wait on every track would be a regression."""
    slept = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ytm.asyncio, "sleep", _fake_sleep)

    async def _fmt(video_id):
        # yt-dlp reports available_at ~= now for a track with no ad in front.
        return {
            "url": "https://stream.example/x",
            "ext": "m4a",
            "available_at": time.time(),
        }

    provider._get_stream_format = _fmt
    asyncio.run(provider.get_stream_details("abc12345678", MediaType.TRACK))

    assert slept == []


@pytest.mark.parametrize(
    ("version", "honours"),
    [
        # Field absent entirely on these, so the answer is moot: _preroll_wait_
        # seconds returns 0.0 either way. False falls out of a plain version
        # comparison and needs no special case.
        ("2025.01.12", False),
        ("2025.08.11", False),
        # The bad range: available_at exists but is a flat +6s on every format,
        # ad or not. Honouring it here would delay every single track.
        ("2025.08.20", False),
        ("2025.10.01", False),
        ("2025.11.12", False),
        # Ad-derived from here on, which is the behaviour the fix assumes.
        ("2025.12.08", True),
        ("2026.07.04", True),
        # Unreadable versions fail towards honouring, because a modern yt-dlp
        # only asks for a wait when there is really an ad.
        ("", True),
        (None, True),
        ("2026.07", True),
        ("nightly", True),
        ("2026.07.04.123456", True),
    ],
)
def test_ytdlp_preroll_support_by_version(version, honours):
    assert ytm._ytdlp_honours_preroll(version) is honours


@pytest.mark.parametrize(
    ("version", "can_stream"),
    [
        # Everything at or below stable 2026.07.04 defaults to android_vr,
        # whose URLs stop serving after ~1 MiB since 2026-08-17.
        ("2026.07.04", False),
        ("2025.12.08", False),
        # The nightly that dropped android_vr from the defaults, and anything
        # after it (nightlies carry a fourth timestamp segment).
        ("2026.08.18.122307", True),
        ("2026.08.18", True),
        ("2026.09.01", True),
        # Unreadable versions fail towards "fine": this check only gates a
        # warning, and the 403 still surfaces on its own.
        ("", True),
        (None, True),
        ("2026.08", True),
        ("nightly", True),
    ],
)
def test_ytdlp_streaming_support_by_version(version, can_stream):
    assert ytm._ytdlp_can_stream(version) is can_stream


def test_install_packages_requests_the_playback_floor(provider):
    """A bare "yt-dlp[default]" is a no-op on every existing install.

    pip does not upgrade an already-satisfied requirement, so the floor in
    _install_packages is the only mechanism that moves existing installs off
    the android_vr-era yt-dlp. See yt-dlp/yt-dlp#17461.
    """
    requested = []

    async def _record(pkg):
        requested.append(pkg)

    original = ytm.install_package
    ytm.install_package = _record
    try:
        asyncio.run(provider._install_packages())
    finally:
        ytm.install_package = original
    assert requested[0] == ytm.MIN_YTDLP_REQUIREMENT
    # The floor names a pre-release on purpose: PEP 440 only lets pip resolve
    # pre-releases when the specifier itself contains one, and the fixed
    # yt-dlp currently only exists as a nightly.
    assert ">=" in ytm.MIN_YTDLP_REQUIREMENT
    version_floor = ytm.MIN_YTDLP_REQUIREMENT.partition(">=")[2]
    parsed_floor = tuple(int(p) for p in version_floor.split(".")[:3])
    assert parsed_floor >= ytm.MIN_YTDLP_VERSION_FOR_PLAYBACK


def _yt_dlp_stub_reporting(version):
    """A fake yt_dlp module that reports ``version`` and resolves one format."""
    import types

    import yt_dlp as real_yt_dlp

    module = types.ModuleType("yt_dlp")
    module.utils = real_yt_dlp.utils
    if version is not None:
        module.version = types.SimpleNamespace(__version__=version)

    class _FakeYoutubeDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return {
                "formats": [
                    {
                        "url": "https://stream.example/x",
                        "vcodec": "none",
                        "acodec": "opus",
                        "ext": "webm",
                        "abr": 130,
                        "format_id": "251",
                    }
                ]
            }

        def build_format_selector(self, spec):
            return real_yt_dlp.YoutubeDL(
                {"quiet": True, "no_warnings": True, "simulate": True}
            ).build_format_selector(spec)

    module.YoutubeDL = _FakeYoutubeDL
    return module


@pytest.mark.parametrize(
    ("version", "expected"),
    [("2025.10.01", False), ("2026.07.04", True), (None, True)],
)
def test_get_stream_format_sets_the_preroll_gate_from_the_ytdlp_version(
    provider, version, expected
):
    """Binds the wiring, not just the pure predicate.

    ``_preroll_supported`` defaults to True, so deleting the assignment inside
    ``_extract`` left every other test green while the gate silently stopped
    working. Driving the real ``_get_stream_format`` is the only way to catch
    that.
    """
    provider._yt_dlp_module = _yt_dlp_stub_reporting(version)
    provider._preroll_supported = not expected  # must be actively overwritten

    asyncio.run(provider._get_stream_format("dQw4w9WgXcQ"))

    assert provider._preroll_supported is expected


def test_installed_ytdlp_is_new_enough_for_the_preroll_fix():
    """The version this repo tests against must be one where the fix applies.

    If CI ever pins a yt-dlp inside the flat-+6s range, the wait switches off
    and the live canary stops covering issue #51 without anything going red.
    """
    yt_dlp = pytest.importorskip("yt_dlp")
    version = yt_dlp.version.__version__
    assert ytm._ytdlp_honours_preroll(version), (
        f"installed yt-dlp {version} predates ad-derived available_at; the "
        "pre-roll wait is disabled and the live canary cannot see issue #51"
    )


def test_preroll_wait_is_skipped_on_a_yt_dlp_that_gets_it_wrong(provider, monkeypatch):
    """The +6s-on-everything releases must not delay every track."""
    slept = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ytm.asyncio, "sleep", _fake_sleep)

    async def _fmt(video_id):
        return {
            "url": "https://stream.example/x",
            "ext": "m4a",
            "available_at": time.time() + 6,
        }

    provider._get_stream_format = _fmt
    provider._preroll_supported = False
    asyncio.run(provider.get_stream_details("abc12345678", MediaType.TRACK))

    assert slept == []


def test_get_stream_details_waits_before_reading_the_url(provider, monkeypatch):
    """Ordering matters: the wait has to be over before the URL is handed on.

    Recording an ordered event log rather than a set of flags. Asserting only
    that both happened would pass just as happily if the sleep ran *after* the
    StreamDetails was built, which would fix nothing.
    """
    events = []

    async def _fake_sleep(seconds):
        events.append("slept")

    monkeypatch.setattr(ytm.asyncio, "sleep", _fake_sleep)

    async def _fmt(video_id):
        events.append("resolved")
        return {
            "url": "https://stream.example/x",
            "ext": "m4a",
            "available_at": time.time() + 3,
        }

    original_init = ytm.StreamDetails.__init__

    def _spy_init(self, *args, **kwargs):
        events.append("built")
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(ytm.StreamDetails, "__init__", _spy_init)

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678", MediaType.TRACK))

    assert events == ["resolved", "slept", "built"], (
        "the pre-roll wait has to complete before the StreamDetails carrying "
        "the url is built, or Music Assistant gets a url that is not valid yet"
    )
    assert sd.path == "https://stream.example/x"


def test_get_stream_details_refuses_an_absurd_preroll_instead_of_stalling(
    provider, monkeypatch
):
    """Past the cap, fail fast rather than sleep the cap and 403 anyway.

    Sleeping the maximum and then handing over a url we already know is still
    gated gives the user the silence *and* the failure.
    """
    slept = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ytm.asyncio, "sleep", _fake_sleep)

    async def _fmt(video_id):
        return {
            "url": "https://stream.example/x",
            "ext": "m4a",
            "available_at": time.time() + ytm.MAX_PREROLL_WAIT + 60,
        }

    provider._get_stream_format = _fmt
    sd = asyncio.run(provider.get_stream_details("abc12345678", MediaType.TRACK))

    assert slept == []
    assert sd.path == "https://stream.example/x"


def test_get_stream_details_still_waits_right_up_to_the_cap(provider, monkeypatch):
    """The boundary: a long-but-plausible pod is served, not refused."""
    slept = []

    async def _fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ytm.asyncio, "sleep", _fake_sleep)

    async def _fmt(video_id):
        return {
            "url": "https://stream.example/x",
            "ext": "m4a",
            "available_at": time.time() + ytm.MAX_PREROLL_WAIT - 5,
        }

    provider._get_stream_format = _fmt
    asyncio.run(provider.get_stream_details("abc12345678", MediaType.TRACK))

    assert slept and slept[0] <= ytm.MAX_PREROLL_WAIT


# ---------------------------------------------------------------------------
# AI-music filter (issue #53)
#
# Applies to auto-generated lists only: radio, mixes, similar tracks and the
# home feed. Search, library and hand-picked playlists are deliberately left
# alone, so a deliberate lookup still finds what the user asked for.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    async def text(self):
        return self._body


class _FakeGet:
    """Stands in for ``session.get(...)`` used as an async context manager."""

    def __init__(self, body=None, error=None):
        self._body = body
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._body)

    async def __aexit__(self, *exc_info):
        return False


def _http_session_returning(body):
    mass = MagicMock()
    mass.http_session.get = lambda url, timeout=None: _FakeGet(body=body)
    return mass


def _http_session_raising(error):
    mass = MagicMock()
    mass.http_session.get = lambda url, timeout=None: _FakeGet(error=error)
    return mass


def _blocklist_track(artist_name="Some Artist", artist_id="UC000000000000000000000a"):
    """A Track carrying one artist, shaped the way _parse_track builds them."""
    track = ytm.Track(
        item_id="vid00000001",
        provider="ytmusic_free",
        name="A Song",
    )
    track.artists = [
        ytm.ItemMapping(
            media_type=MediaType.ARTIST,
            item_id=artist_id,
            provider="ytmusic_free",
            name=artist_name,
        )
    ]
    return track


def test_parse_blocklist_reads_plain_text():
    ids, names = ytm._parse_blocklist(
        "# a comment\nSloppy Bot\nUC000000000000000000000a\n\n  Spaced   Name  \n"
    )
    assert ids == frozenset({"UC000000000000000000000a"})
    assert names == frozenset({"sloppy bot", "spaced name"})


def test_parse_blocklist_only_treats_a_leading_hash_as_a_comment():
    """The config field and README both promise "lines starting with #".

    Splitting on any "#" truncated names that legitimately contain one, so
    "Panic! At The # Disco" became a rule matching "panic! at the".
    """
    ids, names = ytm._parse_blocklist("# real comment\nPanic! At The # Disco\n")
    assert names == frozenset({"panic! at the # disco"})
    assert ids == frozenset()


def test_parse_blocklist_rejects_an_html_body():
    """Some hosts answer a missing file with a 200 and an error page.

    Without this guard every line of that page became a blocked artist name.
    """
    assert ytm._parse_blocklist(
        "<html>\n<body>\nNot Found\n</body>\n</html>"
    ) == (frozenset(), frozenset())


def test_parse_blocklist_reads_a_json_array():
    ids, names = ytm._parse_blocklist('["Sloppy Bot", "UC000000000000000000000a"]')
    assert ids == frozenset({"UC000000000000000000000a"})
    assert names == frozenset({"sloppy bot"})


def test_parse_blocklist_reads_a_json_object_with_an_artists_key():
    ids, names = ytm._parse_blocklist('{"artists": ["Sloppy Bot"], "note": "ignored"}')
    assert names == frozenset({"sloppy bot"})
    assert ids == frozenset()


def test_parse_blocklist_reads_json_objects_per_entry():
    raw = '[{"name": "Sloppy Bot"}, {"channel_id": "UC000000000000000000000a"}]'
    ids, names = ytm._parse_blocklist(raw)
    assert ids == frozenset({"UC000000000000000000000a"})
    assert names == frozenset({"sloppy bot"})


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "\n\n",
        "# only comments\n",
        "[1, 2, 3]",
        '{"unexpected": {"shape": true}}',
        '{\n  "totally": "different"\n}',
        "[]",
        "{}",
    ],
)
def test_parse_blocklist_yields_nothing_for_unusable_input(raw):
    """A list that changes shape must filter nothing, not break playback.

    The JSON cases matter most: valid JSON we do not understand must produce
    an empty list rather than falling through to the line parser, which would
    otherwise register every line of the document as an artist name.
    """
    assert ytm._parse_blocklist(raw) == (frozenset(), frozenset())


def test_parse_blocklist_treats_unparseable_text_as_plain_lines():
    """Not-quite-JSON is still a plain-text list as far as we are concerned."""
    ids, names = ytm._parse_blocklist("not json {\nReal Bot")
    assert "real bot" in names
    assert ids == frozenset()


def test_parse_blocklist_never_yields_an_empty_name():
    """An empty entry would match every track with a nameless artist."""
    _, names = ytm._parse_blocklist("Real Name\n   \n#\n")
    assert "" not in names


def test_ai_filter_is_a_no_op_when_disabled(provider):
    provider._ai_filter_enabled = False
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    tracks = [_blocklist_track("Sloppy Bot")]
    assert provider._drop_ai_tracks(tracks, "test") == tracks


def test_ai_filter_is_a_no_op_with_an_empty_blocklist(provider):
    """Enabling the toggle without a list must not silently filter anything."""
    provider._ai_filter_enabled = True
    provider._ai_blocked_names = frozenset()
    provider._ai_blocked_channel_ids = frozenset()
    tracks = [_blocklist_track("Sloppy Bot")]
    assert provider._drop_ai_tracks(tracks, "test") == tracks


def test_ai_filter_drops_a_blocked_name(provider):
    provider._ai_filter_enabled = True
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    kept = provider._drop_ai_tracks(
        [_blocklist_track("Sloppy Bot"), _blocklist_track("Real Band")], "test"
    )
    assert [t.artists[0].name for t in kept] == ["Real Band"]


def test_ai_filter_matches_names_case_and_space_insensitively(provider):
    provider._ai_filter_enabled = True
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    assert provider._drop_ai_tracks([_blocklist_track("  SLOPPY   Bot ")], "test") == []


def test_ai_filter_drops_a_blocked_channel_id(provider):
    provider._ai_filter_enabled = True
    provider._ai_blocked_channel_ids = frozenset({"UC000000000000000000000a"})
    kept = provider._drop_ai_tracks(
        [
            _blocklist_track("Innocent", artist_id="UC000000000000000000000a"),
            _blocklist_track("Innocent", artist_id="UC000000000000000000000b"),
        ],
        "test",
    )
    assert [t.artists[0].item_id for t in kept] == ["UC000000000000000000000b"]


def test_ai_filter_keeps_a_track_with_no_artists(provider):
    """Never drop something just because we could not read who made it."""
    provider._ai_filter_enabled = True
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    track = _blocklist_track()
    track.artists = []
    assert provider._drop_ai_tracks([track], "test") == [track]


def _configure_filter(provider, blocklist="", url="", enabled=True):
    provider.config = MagicMock()
    provider.config.get_value = lambda key: {
        ytm.CONF_FILTER_AI_MUSIC: enabled,
        ytm.CONF_AI_BLOCKLIST: blocklist,
        ytm.CONF_AI_BLOCKLIST_URL: url,
    }.get(key)
    provider._load_ai_filter_config()


def test_load_ai_filter_config_splits_semicolons_and_newlines(provider):
    _configure_filter(
        provider,
        blocklist="Sloppy Bot; Other Bot\nUC000000000000000000000a",
        url="  ",
    )

    assert provider._ai_filter_enabled is True
    assert provider._ai_blocked_names == frozenset({"sloppy bot", "other bot"})
    assert provider._ai_blocked_channel_ids == frozenset({"UC000000000000000000000a"})
    assert provider._ai_blocklist_url == ""


def test_load_ai_filter_config_keeps_commas_inside_an_artist_name(provider):
    """Splitting on commas turned one band into a rule blocking "Earth".

    That is the dangerous direction for this feature: a filter that removes
    more than the user asked for is deleting music they like.
    """
    _configure_filter(provider, blocklist="Earth, Wind & Fire")

    assert provider._ai_blocked_names == frozenset({"earth, wind & fire"})
    assert "earth" not in provider._ai_blocked_names


def test_remote_blocklist_merges_over_local_entries(provider):
    """A refresh must add to the user's own list, never replace it."""
    provider._ai_filter_enabled = True
    provider._ai_blocklist_url = "https://lists.example/ai.json"
    provider._ai_local_names = frozenset({"my own entry"})
    provider._ai_local_channel_ids = frozenset()
    provider.mass = _http_session_returning('["Remote Bot"]')

    asyncio.run(provider._refresh_remote_blocklist())

    assert provider._ai_blocked_names == frozenset({"my own entry", "remote bot"})
    assert provider._ai_blocklist_fetched_at > 0


def test_remote_blocklist_failure_keeps_the_previous_list(provider):
    """An unreachable list must not silently switch the filter off."""
    provider._ai_filter_enabled = True
    provider._ai_blocklist_url = "https://lists.example/ai.json"
    provider._ai_local_names = frozenset({"my own entry"})
    provider._ai_blocked_names = frozenset({"my own entry", "previously fetched"})
    provider.mass = _http_session_raising(OSError("connection refused"))

    asyncio.run(provider._refresh_remote_blocklist())

    assert provider._ai_blocked_names == frozenset({"my own entry", "previously fetched"})
    assert provider._ai_blocklist_refreshing is False


def test_remote_blocklist_refresh_clears_its_in_flight_flag_on_success(provider):
    provider._ai_blocklist_url = "https://lists.example/ai.json"
    provider._ai_local_names = frozenset()
    provider._ai_local_channel_ids = frozenset()
    provider.mass = _http_session_returning("Remote Bot")

    asyncio.run(provider._refresh_remote_blocklist())

    assert provider._ai_blocklist_refreshing is False
    # Proves the fetch actually ran. Without this the assertion above passes on
    # any early return, including aiohttp being absent, because False is the
    # attribute's default.
    assert provider._ai_blocked_names == frozenset({"remote bot"})


def test_remote_blocklist_refresh_does_not_reenter_while_one_is_in_flight(provider):
    """Two stale reads arriving together must not fire two fetches."""
    provider._ai_blocklist_url = "https://lists.example/ai.json"
    provider._ai_local_names = frozenset()
    provider._ai_local_channel_ids = frozenset()
    provider.mass = _http_session_returning("Remote Bot")
    provider._ai_blocklist_refreshing = True

    asyncio.run(provider._refresh_remote_blocklist())

    assert provider._ai_blocked_names == frozenset()
    assert provider._ai_blocklist_refreshing is True


def test_a_url_only_blocklist_retries_after_a_failed_first_fetch(provider):
    """The recovery path for a config with a URL and no local entries.

    The stale check used to sit behind the empty-blocklist early return, so if
    the one fetch at startup failed there was nothing to filter, nothing to
    schedule, and the feature stayed off for the life of the process.
    """
    _configure_filter(provider, blocklist="", url="https://lists.example/ai.json")
    provider.mass = _http_session_raising(OSError("connection refused"))
    asyncio.run(provider._refresh_remote_blocklist())
    assert provider._ai_blocked_names == frozenset()

    scheduled = []
    provider._schedule_blocklist_refresh_if_stale = lambda: scheduled.append(1)
    provider._drop_ai_tracks([_blocklist_track()], "radio")

    assert scheduled, (
        "a URL-only blocklist whose first fetch failed never retried, so the "
        "filter was off for the rest of the process"
    )


def test_a_failed_fetch_backs_off_instead_of_retrying_every_call(provider):
    """Without a backoff a dead URL fired a request per queue build."""
    _configure_filter(
        provider, blocklist="Local Bot", url="https://lists.example/ai.json"
    )
    provider.mass = _http_session_raising(OSError("connection refused"))
    asyncio.run(provider._refresh_remote_blocklist())

    attempts = []
    original = provider._refresh_remote_blocklist

    async def _counting_refresh():
        attempts.append(1)
        await original()

    provider._refresh_remote_blocklist = _counting_refresh
    for _ in range(20):
        provider._drop_ai_tracks([_blocklist_track()], "radio")

    assert attempts == [], (
        "a failed fetch re-armed immediately, so every filtered call refetched "
        "a dead URL and logged a warning"
    )


def test_the_backoff_expires_so_a_dead_url_is_eventually_retried(provider):
    _configure_filter(
        provider, blocklist="Local Bot", url="https://lists.example/ai.json"
    )
    provider.mass = _http_session_raising(OSError("connection refused"))
    asyncio.run(provider._refresh_remote_blocklist())

    # Wind the last attempt back past the retry window.
    provider._ai_blocklist_attempted_at = time.time() - ytm.AI_BLOCKLIST_RETRY_AFTER - 1

    scheduled = []
    provider._schedule_blocklist_refresh_if_stale = lambda: scheduled.append(1)
    provider._drop_ai_tracks([_blocklist_track()], "radio")
    assert scheduled


def test_similar_tracks_are_filtered(provider):
    """The path Music Assistant's own radio mode pulls from."""
    provider._ai_filter_enabled = True
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    mock = MagicMock()
    mock.get_watch_playlist.return_value = {
        "tracks": [
            {"videoId": "vid00000001", "title": "Good", "artists": [{"name": "Real Band", "id": "UC000000000000000000000b"}]},
            {"videoId": "vid00000002", "title": "Slop", "artists": [{"name": "Sloppy Bot", "id": "UC000000000000000000000a"}]},
        ]
    }
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_similar_tracks("vid00000001"))

    assert [t.name for t in tracks] == ["Good"]


def test_similar_tracks_are_untouched_when_the_filter_is_off(provider):
    provider._ai_filter_enabled = False
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    mock = MagicMock()
    mock.get_watch_playlist.return_value = {
        "tracks": [
            {"videoId": "vid00000001", "title": "Good", "artists": [{"name": "Real Band", "id": "UC000000000000000000000b"}]},
            {"videoId": "vid00000002", "title": "Slop", "artists": [{"name": "Sloppy Bot", "id": "UC000000000000000000000a"}]},
        ]
    }
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_similar_tracks("vid00000001"))

    assert [t.name for t in tracks] == ["Good", "Slop"]


def test_recommendations_are_filtered(provider):
    """The home feed is auto-generated too, so it is in scope for the filter."""
    provider._ai_filter_enabled = True
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    provider._authenticated = True
    mock = MagicMock()
    mock.get_home.return_value = [
        {
            "title": "Listen again",
            "contents": [
                {"videoId": "vid00000001", "title": "Good", "artists": [{"name": "Real Band", "id": "UC000000000000000000000b"}]},
                {"videoId": "vid00000002", "title": "Slop", "artists": [{"name": "Sloppy Bot", "id": "UC000000000000000000000a"}]},
            ],
        }
    ]
    provider._ytmusic = mock

    folders = asyncio.run(provider.recommendations())

    assert [i.name for f in folders for i in f.items] == ["Good"]


def test_recommendations_drop_a_folder_left_empty_by_the_filter(provider):
    """An all-slop shelf should disappear rather than render empty."""
    provider._ai_filter_enabled = True
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    provider._authenticated = True
    mock = MagicMock()
    mock.get_home.return_value = [
        {
            "title": "All slop",
            "contents": [
                {"videoId": "vid00000002", "title": "Slop", "artists": [{"name": "Sloppy Bot", "id": "UC000000000000000000000a"}]},
            ],
        }
    ]
    provider._ytmusic = mock

    assert asyncio.run(provider.recommendations()) == []


def test_radio_playlist_tracks_are_filtered(provider):
    """Mixes and song radio are the main way slop reaches a queue."""
    provider._ai_filter_enabled = True
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    mock = MagicMock()
    mock.get_watch_playlist.return_value = {
        "tracks": [
            {"videoId": "vid00000001", "title": "Good", "artists": [{"name": "Real Band", "id": "UC000000000000000000000b"}]},
            {"videoId": "vid00000002", "title": "Slop", "artists": [{"name": "Sloppy Bot", "id": "UC000000000000000000000a"}]},
        ]
    }
    provider._ytmusic = mock

    tracks = asyncio.run(provider._get_radio_playlist_tracks("RDdQw4w9WgXcQ"))

    assert [t.name for t in tracks] == ["Good"]


def test_search_results_are_never_filtered(provider):
    """A deliberate lookup must still find what the user asked for.

    This is the scope boundary of the feature: filtering search would make a
    blocked artist unfindable, which is a different and more surprising
    behaviour than keeping them out of auto-generated queues.
    """
    provider._ai_filter_enabled = True
    provider._ai_blocked_names = frozenset({"sloppy bot"})
    mock = MagicMock()
    mock.search.return_value = [
        {
            "videoId": "vid00000002",
            "title": "Slop",
            "resultType": "song",
            "artists": [{"name": "Sloppy Bot", "id": "UC000000000000000000000a"}],
        }
    ]
    provider._ytmusic = mock

    results = asyncio.run(provider.search("sloppy bot", [MediaType.TRACK]))

    assert [t.name for t in results.tracks] == ["Slop"]


# ---------------------------------------------------------------------------
# Playlist track caching (issue #56)
#
# Auto-generated mixes come from the watch endpoint, which regenerates the list
# on every call: two consecutive requests for the same song radio returned 147
# tracks each with no overlap. Uncached, that meant a playlist whose contents
# changed every time it was rendered, and a request to YouTube per render.
# ---------------------------------------------------------------------------


def test_playlist_tracks_are_cached_with_a_short_lifetime():
    """Binds the decorator, not just that the method still runs.

    Without an assertion on the recorded arguments, removing the decorator
    entirely would leave every other test green, because the stub in
    conftest.py is a pass-through by design.
    """
    from ytmusic_free import YoutubeMusicFreeProvider

    cache_args = getattr(YoutubeMusicFreeProvider.get_playlist_tracks, "__ma_cache__", None)
    assert cache_args is not None, (
        "get_playlist_tracks is no longer cached; browsing a mix will re-roll "
        "it on every render again (issue #56)"
    )
    assert cache_args["expiration"] == ytm.PLAYLIST_TRACKS_CACHE_TTL
    assert cache_args.get("allow_expired_cache") is True, (
        "without stale-while-revalidate an expiry blocks the browse on a "
        "fresh fetch instead of serving the previous list"
    )


def test_playlist_cache_ttl_is_short_enough_to_still_turn_over():
    """A mix that refreshes once a week is not a dynamic playlist.

    Long enough that browsing twice shows the same thing, short enough that
    the list still changes over a day.
    """
    assert 600 <= ytm.PLAYLIST_TRACKS_CACHE_TTL <= 6 * 3600


def test_cached_playlist_tracks_still_reach_the_caller(provider):
    """The decorator must not swallow or reshape the result."""
    mock = MagicMock()
    mock.get_watch_playlist.return_value = {
        "tracks": [
            {
                "videoId": "vid00000001",
                "title": "Good",
                "artists": [{"name": "Real Band", "id": "UC000000000000000000000b"}],
            }
        ]
    }
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_playlist_tracks("RDdQw4w9WgXcQ"))

    assert [t.name for t in tracks] == ["Good"]


# ---------------------------------------------------------------------------
# Podcasts (issue #52)
#
# Anonymous throughout: search, show detail and episode detail all answer
# without an account, and an episode is an ordinary YouTube video id so the
# existing stream path carries it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3 hr 46 min", 3 * 3600 + 46 * 60),
        ("2 hr 3 min", 2 * 3600 + 3 * 60),
        ("31 min", 31 * 60),
        ("1 hr", 3600),
        ("45 sec", 45),
        ("1 hour 2 minutes 3 seconds", 3723),
        ("1h 2m", 3720),
        # Tracks report a clock string; the same helper has to read both, since
        # which spelling arrives depends on the endpoint rather than the item.
        ("3:46", 226),
        ("1:02:03", 3723),
    ],
)
def test_parse_duration_words(raw, expected):
    assert ytm._parse_duration_words(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "unknown", "views"])
def test_parse_duration_words_returns_none_when_unreadable(raw):
    """None, not 0. An unset duration is not the same as a zero-length episode."""
    assert ytm._parse_duration_words(raw) is None


def test_episode_id_round_trips():
    item_id = ytm._episode_item_id("PLabc123", "vid00000001")
    assert item_id == "PLabc123|vid00000001"
    assert ytm._split_episode_id(item_id) == ("PLabc123", "vid00000001")


def test_split_episode_id_treats_a_bare_id_as_the_video():
    assert ytm._split_episode_id("vid00000001") == ("", "vid00000001")


def test_strip_podcast_browse_prefix():
    assert ytm._strip_podcast_browse_prefix("MPSPPLabc123") == "PLabc123"
    assert ytm._strip_podcast_browse_prefix("PLabc123") == "PLabc123"
    assert ytm._strip_podcast_browse_prefix("") == ""


def test_description_text_handles_both_shapes():
    """get_podcast returns a str; get_episode returns a Description object."""

    class _Description:
        text = "from the object"

        def __str__(self):  # pragma: no cover - would be the bug
            return "Description()"

    assert ytm._description_text("plain") == "plain"
    assert ytm._description_text(_Description()) == "from the object"
    assert ytm._description_text(None) is None
    assert ytm._description_text("") is None


def test_parse_podcast_strips_the_browse_prefix(provider):
    podcast = provider._parse_podcast(
        {"browseId": "MPSPPLabc123", "title": "A Show", "author": {"name": "A Publisher"}}
    )
    assert podcast.item_id == "PLabc123"
    assert podcast.name == "A Show"
    assert podcast.publisher == "A Publisher"


def test_parse_podcast_accepts_a_string_author(provider):
    podcast = provider._parse_podcast({"title": "S", "author": "Someone"}, "PLabc123")
    assert podcast.publisher == "Someone"


def test_parse_podcast_raises_without_an_id(provider):
    with pytest.raises(InvalidDataError):
        provider._parse_podcast({"title": "No id"})


def _podcast(provider):
    return provider._parse_podcast({"title": "A Show"}, "PLabc123")


def test_parse_podcast_episode_builds_a_composite_id(provider):
    episode = provider._parse_podcast_episode(
        {"videoId": "vid00000001", "title": "Ep 1", "duration": "31 min"},
        _podcast(provider),
        position=7,
    )
    assert episode.item_id == "PLabc123|vid00000001"
    assert episode.position == 7
    assert episode.duration == 31 * 60
    assert episode.podcast.item_id == "PLabc123"
    assert all("watch?v=vid00000001" in m.url for m in episode.provider_mappings)


def test_parse_podcast_episode_leaves_resume_state_unset(provider):
    """None tells Music Assistant to use its own resume point.

    YouTube's anonymous responses carry no playback position, so asserting one
    would be inventing it.
    """
    episode = provider._parse_podcast_episode(
        {"videoId": "vid00000001", "title": "Ep"}, _podcast(provider)
    )
    assert episode.fully_played is None
    assert episode.resume_position_ms is None


def test_parse_podcast_episode_ignores_the_date_field(provider):
    """"date" holds a view count on anonymous responses, not a date.

    Measured as "591K views" on every episode of every show checked. Parsing it
    as a release date would either raise or land a nonsense date on the item.
    """
    episode = provider._parse_podcast_episode(
        {"videoId": "vid00000001", "title": "Ep", "date": "591K views"},
        _podcast(provider),
    )
    assert getattr(episode.metadata, "release_date", None) is None


def test_parse_podcast_episode_raises_without_a_video_id(provider):
    with pytest.raises(InvalidDataError):
        provider._parse_podcast_episode({"title": "No id"}, _podcast(provider))


def test_get_podcast_episodes_numbers_them_when_index_is_absent(provider):
    """"index" is None on every anonymous response, so enumeration is all we have."""
    mock = MagicMock()
    mock.get_podcast.return_value = {
        "title": "A Show",
        "episodes": [
            {"videoId": "v1", "title": "One", "duration": "31 min"},
            {"videoId": "v2", "title": "Two", "duration": "1 hr"},
        ],
    }
    provider._ytmusic = mock

    async def _collect():
        return [e async for e in provider.get_podcast_episodes("PLabc123")]

    episodes = asyncio.run(_collect())
    assert [e.position for e in episodes] == [1, 2]
    assert [e.duration for e in episodes] == [31 * 60, 3600]


def test_get_podcast_episodes_skips_an_unparseable_entry(provider):
    """One malformed episode must not sink the whole show."""
    mock = MagicMock()
    mock.get_podcast.return_value = {
        "title": "A Show",
        "episodes": [{"title": "no video id"}, {"videoId": "v2", "title": "Two"}],
    }
    provider._ytmusic = mock

    async def _collect():
        return [e async for e in provider.get_podcast_episodes("PLabc123")]

    assert [e.name for e in asyncio.run(_collect())] == ["Two"]


def test_get_podcast_raises_media_not_found(provider):
    mock = MagicMock()
    mock.get_podcast.side_effect = KeyError("nope")
    provider._ytmusic = mock
    with pytest.raises(MediaNotFoundError):
        asyncio.run(provider.get_podcast("PLmissing"))


def test_get_podcast_episode_falls_back_to_a_stub_show(provider):
    """A failed show lookup must not make the episode unplayable."""
    mock = MagicMock()
    mock.get_episode.return_value = {
        "title": "Ep",
        "duration": "31 min",
        "author": {"name": "The Show", "id": "MPSPPLabc123"},
    }
    mock.get_podcast.side_effect = RuntimeError("show lookup failed")
    provider._ytmusic = mock

    episode = asyncio.run(provider.get_podcast_episode("PLabc123|vid00000001"))

    assert episode.item_id == "PLabc123|vid00000001"
    assert episode.podcast.name == "The Show"


def test_get_stream_details_resolves_a_podcast_episode_id(provider):
    """The show has to be stripped before yt-dlp sees the id."""
    seen = {}

    async def _fmt(video_id):
        seen["video_id"] = video_id
        return {"url": "https://stream.example/x", "ext": "m4a"}

    provider._get_stream_format = _fmt
    sd = asyncio.run(
        provider.get_stream_details("PLabc123|vid00000001", MediaType.PODCAST_EPISODE)
    )

    assert seen["video_id"] == "vid00000001"
    assert sd.item_id == "PLabc123|vid00000001"


def test_search_returns_podcasts(provider):
    mock = MagicMock()
    mock.search.return_value = [
        {
            "resultType": "podcast",
            "browseId": "MPSPPLabc123",
            "title": "A Show",
            "category": "Podcasts",
        }
    ]
    provider._ytmusic = mock

    results = asyncio.run(provider.search("a show", [MediaType.PODCAST]))

    assert [p.item_id for p in results.podcasts] == ["PLabc123"]


def test_podcast_lookups_are_cached():
    from ytmusic_free import YoutubeMusicFreeProvider

    for name in ("get_podcast", "get_podcast_episode"):
        args = getattr(getattr(YoutubeMusicFreeProvider, name), "__ma_cache__", None)
        assert args is not None, f"{name} is not cached"
        assert args["expiration"] == ytm.PODCAST_CACHE_TTL


def test_library_podcasts_is_an_authenticated_feature_only():
    """Subscribed shows need a cookie, so the feature belongs with the others.

    Declaring it was unsafe while there was no implementation behind it: Music
    Assistant deletes anything a completed sync did not return, so an empty
    answer is an instruction to unsubscribe from everything. It is safe now
    only because get_library_podcasts goes through the same guard as every
    other library method, which the next test pins.
    """
    feature = ytm.ProviderFeature.LIBRARY_PODCASTS
    assert feature in ytm.AUTHENTICATED_FEATURES
    assert feature not in ytm.BASE_FEATURES


def test_library_podcasts_fails_rather_than_reporting_no_subscriptions(provider):
    """The issue #55 guard, applied to the new media type."""
    provider._authenticated = False
    provider._auth_lapse_warned = False
    _cookie_configured(provider)

    with pytest.raises(RuntimeError, match="not active"):
        _consume(provider.get_library_podcasts())


def test_library_podcasts_skips_the_auto_generated_playlists(provider):
    """"New Episodes" and "Saved episodes" are not shows.

    YouTube returns them alongside real subscriptions. Syncing them would put
    two permanent pseudo-subscriptions in the library that cannot be removed.
    """
    provider._authenticated = True
    mock = MagicMock()
    mock.get_library_podcasts.return_value = [
        {"title": "New Episodes", "podcastId": "RDPN", "channel": {"id": None, "name": "Auto playlist"}},
        {"title": "Saved episodes", "podcastId": "SE", "channel": {"id": None, "name": "Auto playlist"}},
        {
            "title": "A Real Show",
            "podcastId": "PLabc123",
            "browseId": "MPSPPLabc123",
            "channel": {"id": "UCxyz", "name": "A Publisher"},
        },
    ]
    provider._ytmusic = mock

    shows = _consume(provider.get_library_podcasts())

    assert [s.item_id for s in shows] == ["PLabc123"]
    assert shows[0].publisher == "A Publisher"


def test_library_podcasts_counts_real_shows_for_the_empty_guard(provider):
    """The auto-playlists must not make a lapsed session look populated.

    YouTube returns them whether or not you subscribe to anything, so counting
    before filtering would mask exactly the empty sync the guard exists to
    catch.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_podcasts.return_value = [
        {"title": "New Episodes", "podcastId": "RDPN"},
        {"title": "Saved episodes", "podcastId": "SE"},
    ]
    mock.get_account_info = MagicMock(return_value={})  # logged-out shape
    provider._ytmusic = mock

    with pytest.raises(RuntimeError, match="cookie lapse"):
        _consume(provider.get_library_podcasts())


# ---------------------------------------------------------------------------
# Metadata cache and album year (issue #53)
#
# A track parsed from a playlist or search carries only {"id", "name"} for its
# album, so the release year has to be looked up. That is only affordable
# because the album lookup is cached for a month.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "ttl_attr"),
    [
        ("get_album", "CATALOG_CACHE_TTL"),
        ("get_album_tracks", "CATALOG_CACHE_TTL"),
        ("get_track", "CATALOG_CACHE_TTL"),
        ("get_artist", "ARTIST_CACHE_TTL"),
        ("get_artist_albums", "ARTIST_CACHE_TTL"),
        ("get_artist_toptracks", "ARTIST_CACHE_TTL"),
        ("get_similar_tracks", "SIMILAR_TRACKS_CACHE_TTL"),
        ("get_playlist", "PLAYLIST_TRACKS_CACHE_TTL"),
    ],
)
def test_metadata_lookups_are_cached(method, ttl_attr):
    """Every repeated catalogue lookup is cached.

    Without this the provider made a request per render, which is the
    bot-detection exposure raised in issue #53 and what made the album-year
    lookup unaffordable.
    """
    from ytmusic_free import YoutubeMusicFreeProvider

    args = getattr(getattr(YoutubeMusicFreeProvider, method), "__ma_cache__", None)
    assert args is not None, f"{method} is not cached"
    assert args["expiration"] == getattr(ytm, ttl_attr)
    assert args.get("allow_expired_cache") is True


@pytest.mark.parametrize("method", ["get_track", "get_album_tracks", "get_artist_toptracks"])
def test_track_caches_carry_the_parser_version(method):
    """Tracks written by an older parser become misses after an update.

    These entries last up to 30 days and Music Assistant serves expired ones
    once more before refreshing, so a parser fix such as the THUMB fallback
    for video uploads (issue #90) took a month to arrive without a checksum.
    """
    from ytmusic_free import YoutubeMusicFreeProvider

    args = getattr(YoutubeMusicFreeProvider, method).__ma_cache__
    assert args.get("cache_checksum") == ytm.TRACK_CACHE_VERSION


@pytest.mark.parametrize("method", ["get_album", "get_artist", "get_artist_albums"])
def test_album_and_artist_caches_survive_a_track_parser_bump(method):
    """The album-year lookup runs get_album up to 40 times per page (#53).

    A checksum there would send every user's lookups to YouTube cold after
    each release that only changed how tracks are parsed.
    """
    from ytmusic_free import YoutubeMusicFreeProvider

    args = getattr(YoutubeMusicFreeProvider, method).__ma_cache__
    assert "cache_checksum" not in args


def test_similar_tracks_cache_is_shorter_than_the_catalog_one():
    """Song radio is meant to differ each time; an album's year is not."""
    assert ytm.SIMILAR_TRACKS_CACHE_TTL < ytm.CATALOG_CACHE_TTL


def _track_with_album(provider, album_id="MPREb_x", year=None):
    track = ytm.Track(item_id="v1", provider="ytmusic_free", name="A Song")
    track.album = provider._get_item_mapping(MediaType.ALBUM, album_id, "An Album", year)
    return track


def test_item_mapping_carries_a_year_when_given_one(provider):
    mapping = provider._get_item_mapping(MediaType.ALBUM, "MPREb_x", "An Album", 1998)
    assert mapping.year == 1998


def test_item_mapping_year_defaults_to_none(provider):
    """Unknown must stay distinguishable from a real year."""
    mapping = provider._get_item_mapping(MediaType.ALBUM, "MPREb_x", "An Album")
    assert mapping.year is None


def test_album_tracks_carry_the_album_year_without_a_lookup(provider):
    """The album is already in hand here, so the year is free."""
    mock = MagicMock()
    mock.get_album.return_value = {
        "title": "An Album",
        "year": "1998",
        "tracks": [{"videoId": "v1", "title": "One", "artists": [{"name": "A", "id": "UCa"}]}],
    }
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_album_tracks("MPREb_x"))

    assert [t.album.year for t in tracks] == [1998]
    assert mock.get_album.call_count == 1


def test_attach_album_years_fills_the_year_from_one_lookup_per_album(provider):
    tracks = [_track_with_album(provider), _track_with_album(provider)]
    calls = []

    async def _get_album(album_id):
        calls.append(album_id)
        album = ytm.Album(item_id=album_id, provider="ytmusic_free", name="An Album")
        album.year = 1998
        return album

    provider.get_album = _get_album
    asyncio.run(provider._attach_album_years(tracks))

    assert [t.album.year for t in tracks] == [1998, 1998]
    assert calls == ["MPREb_x"], "two tracks on one album must cost one lookup"


def test_attach_album_years_skips_albums_that_already_have_one(provider):
    tracks = [_track_with_album(provider, year=1998)]
    provider.get_album = MagicMock(side_effect=AssertionError("must not be called"))
    asyncio.run(provider._attach_album_years(tracks))
    assert tracks[0].album.year == 1998


def test_attach_album_years_ignores_tracks_with_no_album(provider):
    """Radio tracks arrive with album=None, so there is nothing to look up."""
    track = ytm.Track(item_id="v1", provider="ytmusic_free", name="A Song")
    track.album = None
    provider.get_album = MagicMock(side_effect=AssertionError("must not be called"))
    asyncio.run(provider._attach_album_years([track]))


def test_attach_album_years_survives_a_failed_lookup(provider):
    """A missing year must never fail the playlist it came from."""
    tracks = [_track_with_album(provider)]

    async def _boom(album_id):
        raise RuntimeError("album lookup failed")

    provider.get_album = _boom
    asyncio.run(provider._attach_album_years(tracks))

    assert tracks[0].album.year is None


def test_attach_album_years_is_capped(provider):
    """One pathological playlist must not become hundreds of requests."""
    tracks = [
        _track_with_album(provider, album_id=f"MPREb_{i}")
        for i in range(ytm.ALBUM_YEAR_LOOKUP_LIMIT + 15)
    ]
    calls = []

    async def _get_album(album_id):
        calls.append(album_id)
        album = ytm.Album(item_id=album_id, provider="ytmusic_free", name="An Album")
        album.year = 2001
        return album

    provider.get_album = _get_album
    asyncio.run(provider._attach_album_years(tracks))

    assert len(calls) == ytm.ALBUM_YEAR_LOOKUP_LIMIT
    filled = [t for t in tracks if t.album.year is not None]
    assert len(filled) == ytm.ALBUM_YEAR_LOOKUP_LIMIT


def test_attach_album_years_bounds_concurrency(provider):
    """A cold cache must not arrive as a burst of parallel requests."""
    in_flight = 0
    peak = 0

    async def _get_album(album_id):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        album = ytm.Album(item_id=album_id, provider="ytmusic_free", name="An Album")
        album.year = 2001
        return album

    provider.get_album = _get_album
    tracks = [_track_with_album(provider, album_id=f"MPREb_{i}") for i in range(20)]
    asyncio.run(provider._attach_album_years(tracks))

    assert peak <= ytm.ALBUM_YEAR_LOOKUP_CONCURRENCY


def test_playlist_tracks_get_their_album_year(provider):
    """The path da-anda's queue filter actually reads from (issue #53)."""
    mock = MagicMock()
    mock.get_playlist.return_value = {
        "id": "PLabc123",
        "title": "A Playlist",
        "tracks": [
            {
                "videoId": "v1",
                "title": "One",
                "artists": [{"name": "A", "id": "UCa"}],
                "album": {"id": "MPREb_x", "name": "An Album"},
            }
        ],
    }
    provider._ytmusic = mock

    async def _get_album(album_id):
        album = ytm.Album(item_id=album_id, provider="ytmusic_free", name="An Album")
        album.year = 1998
        return album

    provider.get_album = _get_album

    tracks = asyncio.run(provider.get_playlist_tracks("PLabc123"))

    assert [t.album.year for t in tracks] == [1998], (
        "playlist tracks reached Music Assistant without a release year, which "
        "is the gap issue #53 reported"
    )


def test_search_tracks_get_their_album_year(provider):
    mock = MagicMock()
    mock.search.return_value = [
        {
            "resultType": "song",
            "videoId": "v1",
            "title": "One",
            "artists": [{"name": "A", "id": "UCa"}],
            "album": {"id": "MPREb_x", "name": "An Album"},
        }
    ]
    provider._ytmusic = mock

    async def _get_album(album_id):
        album = ytm.Album(item_id=album_id, provider="ytmusic_free", name="An Album")
        album.year = 2004
        return album

    provider.get_album = _get_album

    results = asyncio.run(provider.search("one", [MediaType.TRACK]))

    assert [t.album.year for t in results.tracks] == [2004]


def test_get_album_raises_when_not_found(provider):
    mock = MagicMock()
    mock.get_album = MagicMock(return_value=None)
    provider._ytmusic = mock
    with pytest.raises(MediaNotFoundError):
        asyncio.run(provider.get_album("MPREb_missing"))


def test_get_album_tracks_returns_empty_on_none(provider):
    mock = MagicMock()
    mock.get_album = MagicMock(return_value=None)
    provider._ytmusic = mock
    tracks = asyncio.run(provider.get_album_tracks("MPREb_missing"))
    assert tracks == []


def test_get_album_tracks_assigns_track_numbers(provider):
    mock = MagicMock()
    mock.get_album = MagicMock(
        return_value={
            "tracks": [
                {
                    "videoId": "v1",
                    "title": "First",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
                {
                    "videoId": "v2",
                    "title": "Second",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ]
        }
    )
    provider._ytmusic = mock
    tracks = asyncio.run(provider.get_album_tracks("MPREb_x"))
    assert [t.item_id for t in tracks] == ["v1", "v2"]
    assert [t.track_number for t in tracks] == [1, 2]


def test_get_track_falls_back_to_minimal_track_on_failure(provider):
    mock = MagicMock()
    mock.get_song = MagicMock(side_effect=RuntimeError("boom"))
    provider._ytmusic = mock
    track = asyncio.run(provider.get_track("vid_x"))
    assert track.item_id == "vid_x"
    assert track.name == "vid_x"


def test_get_track_normalizes_video_details(provider):
    mock = MagicMock()
    mock.get_song = MagicMock(
        return_value={
            "videoDetails": {
                "videoId": "vid_y",
                "title": "Some Song",
                "lengthSeconds": "200",
                "author": "Author",
                "thumbnail": {"thumbnails": []},
            }
        }
    )
    provider._ytmusic = mock
    track = asyncio.run(provider.get_track("vid_y"))
    assert track.item_id == "vid_y"
    assert track.name == "Some Song"
    assert track.duration == 200


def test_get_playlist_tracks_uses_ytdlp_when_ytmusicapi_is_partial(provider):
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={
            "trackCount": 3,
            "tracks": [
                {
                    "videoId": "ytm_1",
                    "title": "First",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
                {
                    "videoId": "ytm_2",
                    "title": "Second",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ],
        }
    )
    provider._ytmusic = mock

    seen = {}

    async def _fallback(playlist_id, seed_video_id=None):
        seen["playlist_id"] = playlist_id
        seen["seed"] = seed_video_id
        return [
            provider._minimal_track(track_id)
            for track_id in ("ytm_1", "ytm_2", "dlp_3")
        ]

    provider._get_playlist_tracks_via_ytdlp = _fallback

    tracks = asyncio.run(provider.get_playlist_tracks("PLpartial"))

    assert [track.item_id for track in tracks] == ["ytm_1", "ytm_2", "dlp_3"]
    assert [track.name for track in tracks] == ["First", "Second", "dlp_3"]
    # The fallback is handed the first already-parsed track as a seed, which is
    # the only thing that makes a radio id openable at all (issue #47).
    assert seen["playlist_id"] == "PLpartial"
    assert seen["seed"] == "ytm_1"


def test_get_playlist_tracks_keeps_ytmusicapi_when_complete(provider):
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={
            "trackCount": "2 songs",
            "tracks": [
                {
                    "videoId": "ytm_1",
                    "title": "First",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
                {
                    "videoId": "ytm_2",
                    "title": "Second",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ],
        }
    )
    provider._ytmusic = mock
    fallback = MagicMock(side_effect=AssertionError("yt-dlp fallback should not run"))
    provider._get_playlist_tracks_via_ytdlp = fallback

    tracks = asyncio.run(provider.get_playlist_tracks("PLcomplete"))

    assert [track.item_id for track in tracks] == ["ytm_1", "ytm_2"]


def test_get_playlist_tracks_skips_unavailable_tracks(provider):
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={
            "tracks": [
                {
                    "videoId": "gone",
                    "title": "Gone",
                    "artists": [{"id": "UC1", "name": "A"}],
                    "isAvailable": False,
                },
                {
                    "videoId": "available",
                    "title": "Available",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ],
        }
    )
    provider._ytmusic = mock

    tracks = asyncio.run(provider.get_playlist_tracks("PLavailable"))

    assert [track.item_id for track in tracks] == ["available"]


# ---------------------------------------------------------------------------
# Private playlists (issue #82): the yt-dlp fallback can never read them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("playlist_id", ["LM", "VLLM", "SE", "RDPN"])
def test_is_private_playlist_id_true(playlist_id):
    assert ytm._is_private_playlist_id(playlist_id)


@pytest.mark.parametrize("playlist_id", ["PLxxx123", "VLPLxxx123", "RDdQw4w9WgXcQ", "OLAK5uy_abc"])
def test_is_private_playlist_id_false(playlist_id):
    assert not ytm._is_private_playlist_id(playlist_id)


def test_get_playlist_tracks_lm_undercount_never_runs_ytdlp(provider):
    """YouTube's reported count for LM includes unavailable entries, so the
    undercount test is chronically true there. The fill-the-gap fallback must
    not run: unauthenticated yt-dlp cannot see a private playlist and only
    logs a misleading "playlist does not exist" (issue #82)."""
    mock = MagicMock()
    mock.get_playlist = MagicMock(
        return_value={
            "trackCount": 5,
            "tracks": [
                {
                    "videoId": "liked_1",
                    "title": "First",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
                {
                    "videoId": "liked_2",
                    "title": "Second",
                    "artists": [{"id": "UC1", "name": "A"}],
                },
            ],
        }
    )
    provider._ytmusic = mock
    fallback = MagicMock(side_effect=AssertionError("yt-dlp fallback should not run"))
    provider._get_playlist_tracks_via_ytdlp = fallback

    tracks = asyncio.run(provider.get_playlist_tracks("LM"))

    assert [track.item_id for track in tracks] == ["liked_1", "liked_2"]


def test_get_playlist_tracks_lm_failure_raises_instead_of_going_empty(provider):
    """When the authenticated read of a private playlist fails there is no
    working fallback. Raising keeps the cache decorator from storing an empty
    listing over the last good one; returning [] here is what made a removed
    like look like a wiped playlist (issue #82)."""
    mock = MagicMock()
    mock.get_playlist = MagicMock(side_effect=RuntimeError("HTTP 401: Unauthorized"))
    provider._ytmusic = mock
    fallback = MagicMock(side_effect=AssertionError("yt-dlp fallback should not run"))
    provider._get_playlist_tracks_via_ytdlp = fallback

    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(provider.get_playlist_tracks("LM"))


def test_get_playlist_tracks_non_private_failure_still_falls_back(provider):
    """The yt-dlp degradation stays in place for ordinary playlist ids."""
    mock = MagicMock()
    mock.get_playlist = MagicMock(side_effect=RuntimeError("boom"))
    provider._ytmusic = mock

    async def _fallback(playlist_id, seed_video_id=None):
        return [provider._minimal_track("dlp_1")]

    provider._get_playlist_tracks_via_ytdlp = _fallback

    tracks = asyncio.run(provider.get_playlist_tracks("PLbroken"))

    assert [track.item_id for track in tracks] == ["dlp_1"]


def test_get_playlist_lm_failure_raises_media_not_found(provider):
    mock = MagicMock()
    mock.get_playlist = MagicMock(side_effect=RuntimeError("HTTP 401: Unauthorized"))
    provider._ytmusic = mock
    fallback = MagicMock(side_effect=AssertionError("yt-dlp fallback should not run"))
    provider._get_playlist_via_ytdlp = fallback

    with pytest.raises(MediaNotFoundError):
        asyncio.run(provider.get_playlist("LM"))


def test_get_artist_unknown_prefix_returns_stub(provider):
    artist = asyncio.run(provider.get_artist("unknown_Foo Bar"))
    assert artist.name == "Foo Bar"
    assert artist.item_id == "unknown_Foo Bar"


def test_get_artist_bare_unknown_returns_stub_without_ytm_call(provider):
    """The artist-less placeholder id resolves to a stub instead of raising.

    Music Assistant resolves a track's artist mappings through get_artist when
    the artist page opens and when a track is added to the library, and treats
    MediaNotFoundError as "drop the artist" — which errored the artist page and
    made adding a track whose only artist is the placeholder fail with
    "Track is missing artist(s)"."""
    mock = MagicMock()
    mock.get_artist = MagicMock(side_effect=AssertionError("must not be called"))
    provider._ytmusic = mock
    artist = asyncio.run(provider.get_artist("unknown"))
    assert artist.item_id == "unknown"
    assert artist.name == "Unknown Artist"
    assert artist.provider_mappings
    mock.get_artist.assert_not_called()


def test_get_artist_non_channel_id_not_found_without_ytm_call(provider):
    """A non-channel id (e.g. one pulled from track metadata) must not be
    handed to YTM â€” it would return HTTP 400. We raise MediaNotFoundError
    without ever calling get_artist (issue #18)."""
    from music_assistant_models.errors import MediaNotFoundError

    mock = MagicMock()
    mock.get_artist = MagicMock(side_effect=AssertionError("must not be called"))
    provider._ytmusic = mock
    with pytest.raises(MediaNotFoundError):
        asyncio.run(provider.get_artist("MPLA_not_a_channel"))
    mock.get_artist.assert_not_called()


def test_get_artist_albums_non_channel_id_returns_empty(provider):
    """A non-channel id degrades to an empty album list instead of raising the
    raw HTTP 400 it would previously surface (issue #18)."""
    mock = MagicMock()
    mock.get_artist = MagicMock(side_effect=AssertionError("must not be called"))
    provider._ytmusic = mock
    assert asyncio.run(provider.get_artist_albums("not_a_channel")) == []
    mock.get_artist.assert_not_called()


def test_get_artist_albums_ytm_error_returns_empty(provider):
    """A YTM 400 on a channel-shaped id is caught and degrades to []."""
    mock = MagicMock()
    mock.get_artist = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 400: Bad Request")
    )
    provider._ytmusic = mock
    assert asyncio.run(provider.get_artist_albums("UCbroken")) == []


def test_get_artist_toptracks_non_channel_id_returns_empty(provider):
    """A non-channel id degrades to an empty track list (issue #18)."""
    mock = MagicMock()
    mock.get_artist = MagicMock(side_effect=AssertionError("must not be called"))
    provider._ytmusic = mock
    assert asyncio.run(provider.get_artist_toptracks("not_a_channel")) == []
    mock.get_artist.assert_not_called()


def test_get_artist_toptracks_ytm_error_returns_empty(provider):
    """A YTM 400 on a channel-shaped id is caught and degrades to []."""
    mock = MagicMock()
    mock.get_artist = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 400: Bad Request")
    )
    provider._ytmusic = mock
    assert asyncio.run(provider.get_artist_toptracks("UCbroken")) == []


def test_get_similar_tracks_watch_playlist_error_returns_empty(provider):
    """A ytmusicapi failure (e.g. KeyError 'endpoint') degrades to [] instead of
    propagating and failing the whole play_media command (issue #20)."""
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(side_effect=KeyError("endpoint"))
    provider._ytmusic = mock
    assert asyncio.run(provider.get_similar_tracks("vid_x")) == []
    mock.get_watch_playlist.assert_called_once()


def test_get_similar_tracks_parses_returned_tracks(provider):
    """A normal watch-playlist response still parses its tracks."""
    mock = MagicMock()
    mock.get_watch_playlist = MagicMock(
        return_value={
            "tracks": [
                {
                    "videoId": "vid1",
                    "title": "Song One",
                    "artists": [{"id": "UCart", "name": "An Artist"}],
                },
                # Missing videoId â€” must be skipped, not crash the radio fill.
                {"title": "broken", "artists": [{"id": "UCart", "name": "A"}]},
            ]
        }
    )
    provider._ytmusic = mock
    tracks = asyncio.run(provider.get_similar_tracks("vid_x"))
    assert len(tracks) == 1
    assert tracks[0].item_id == "vid1"


def test_library_methods_no_op_when_not_authenticated(provider):
    """Library generators should yield nothing when auth is off."""
    provider._authenticated = False

    async def _consume(generator):
        return [item async for item in generator]

    assert asyncio.run(_consume(provider.get_library_artists())) == []
    assert asyncio.run(_consume(provider.get_library_albums())) == []
    assert asyncio.run(_consume(provider.get_library_tracks())) == []
    assert asyncio.run(_consume(provider.get_library_playlists())) == []


def test_library_add_remove_short_circuit_when_not_authenticated(provider):
    provider._authenticated = False
    item = MagicMock()
    item.media_type = MediaType.ARTIST
    item.provider_mappings = []
    assert asyncio.run(provider.library_add(item)) is False
    assert asyncio.run(provider.library_remove("UC1", MediaType.ARTIST)) is False


def test_recommendations_empty_when_not_authenticated(provider):
    provider._authenticated = False
    result = asyncio.run(provider.recommendations())
    assert result == []


# ---------------------------------------------------------------------------
# library_add / library_remove â€” 403 no-op for user-owned items
# ---------------------------------------------------------------------------


def _make_authed_provider_with_rate_failure(provider, error: Exception):
    provider._authenticated = True
    mock = MagicMock()
    mock.rate_playlist = MagicMock(side_effect=error)
    mock.subscribe_artists = MagicMock(side_effect=error)
    mock.unsubscribe_artists = MagicMock(side_effect=error)
    provider._ytmusic = mock
    return mock


def _make_item(media_type, item_id):
    item = MagicMock()
    item.media_type = media_type
    mapping = MagicMock()
    mapping.provider_instance = provider_instance_id_for_tests
    mapping.item_id = item_id
    item.provider_mappings = [mapping]
    return item


provider_instance_id_for_tests = "test_instance"


def test_library_add_treats_403_on_playlist_as_no_op(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    item = _make_item(MediaType.PLAYLIST, "PL0OwTHSGw5kg_owned")
    assert asyncio.run(provider.library_add(item)) is True


def test_library_add_treats_403_on_album_as_no_op(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    item = _make_item(MediaType.ALBUM, "MPREb_owned")
    assert asyncio.run(provider.library_add(item)) is True


def test_library_add_non_403_error_returns_false(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 500: Internal Server Error.")
    )
    item = _make_item(MediaType.PLAYLIST, "PLsome")
    assert asyncio.run(provider.library_add(item)) is False


def test_library_add_403_on_artist_is_not_swallowed(provider):
    """The 403 no-op only applies to ALBUM/PLAYLIST â€” artist subscription failure is real."""
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    item = _make_item(MediaType.ARTIST, "UCsome")
    assert asyncio.run(provider.library_add(item)) is False


def test_library_remove_treats_403_on_playlist_as_no_op(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    assert (
        asyncio.run(provider.library_remove("PL0OwTHSGw5kg_owned", MediaType.PLAYLIST))
        is True
    )


def test_library_remove_treats_403_on_album_as_no_op(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    assert asyncio.run(provider.library_remove("MPREb_owned", MediaType.ALBUM)) is True


def test_library_remove_non_403_error_returns_false(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 500: Internal Server Error.")
    )
    assert asyncio.run(provider.library_remove("PLsome", MediaType.PLAYLIST)) is False


def test_library_remove_403_on_artist_is_not_swallowed(provider):
    _make_authed_provider_with_rate_failure(
        provider, RuntimeError("Server returned HTTP 403: Forbidden.")
    )
    assert asyncio.run(provider.library_remove("UCsome", MediaType.ARTIST)) is False


# ---------------------------------------------------------------------------
# library_add / library_remove for tracks — like / un-like the song (issue #82)
# ---------------------------------------------------------------------------


def test_library_tracks_edit_feature_declared():
    assert ProviderFeature.LIBRARY_TRACKS_EDIT in ytm.AUTHENTICATED_FEATURES


def _make_authed_provider_with_rate_song(provider):
    provider._authenticated = True
    mock = MagicMock()
    mock.rate_song = MagicMock(return_value=None)
    provider._ytmusic = mock
    return mock


def test_library_add_track_likes_the_song(provider):
    mock = _make_authed_provider_with_rate_song(provider)
    item = _make_item(MediaType.TRACK, "vid12345678")
    assert asyncio.run(provider.library_add(item)) is True
    mock.rate_song.assert_called_once_with("vid12345678", "LIKE")


def test_library_remove_track_unlikes_the_song(provider):
    """Deleting a liked track has to remove the like on YouTube too, or the
    next sync imports it right back (issue #82)."""
    mock = _make_authed_provider_with_rate_song(provider)
    assert asyncio.run(provider.library_remove("vid12345678", MediaType.TRACK)) is True
    mock.rate_song.assert_called_once_with("vid12345678", "INDIFFERENT")


def test_library_track_edit_strips_trim_window(provider):
    """Only the bare video id means anything to the rating endpoint."""
    mock = _make_authed_provider_with_rate_song(provider)
    assert (
        asyncio.run(provider.library_remove("vid12345678@30-90", MediaType.TRACK))
        is True
    )
    mock.rate_song.assert_called_once_with("vid12345678", "INDIFFERENT")


def test_library_remove_track_error_returns_false(provider):
    provider._authenticated = True
    mock = MagicMock()
    mock.rate_song = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 500: Internal Server Error.")
    )
    provider._ytmusic = mock
    assert asyncio.run(provider.library_remove("vid12345678", MediaType.TRACK)) is False


# ---------------------------------------------------------------------------
# Cookie sanity warning (issue #6 follow-up)
# ---------------------------------------------------------------------------


import logging as _logging


class _CaptureHandler(_logging.Handler):
    """Logging handler that stores records for later assertion."""

    def __init__(self):
        super().__init__(level=_logging.DEBUG)
        self.records: list[_logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


def _attach_capture(provider):
    handler = _CaptureHandler()
    logger = _logging.getLogger(f"ytmusic_free_capture_{id(handler)}")
    logger.handlers = [handler]
    logger.setLevel(_logging.DEBUG)
    logger.propagate = False
    provider.logger = logger
    return handler


def test_build_auth_headers_warns_when_recommended_cookies_missing(provider, monkeypatch):
    _forbid_open(monkeypatch)
    handler = _attach_capture(provider)

    # Has the hard requirement but none of the recommended session cookies.
    provider._build_auth_headers("__Secure-3PAPISID=onlythis; SAPISID=foo")

    messages = handler.messages()
    assert any("missing recommended" in m for m in messages), messages
    joined = " ".join(messages)
    assert "__Secure-1PSID" in joined
    assert "__Secure-3PSID" in joined


def test_build_auth_headers_no_warning_when_full_cookie_present(provider, monkeypatch):
    _forbid_open(monkeypatch)
    handler = _attach_capture(provider)

    cookie = (
        "__Secure-3PAPISID=a; SAPISID=b; "
        "__Secure-1PSID=c; __Secure-3PSID=d; HSID=e"
    )
    provider._build_auth_headers(cookie)

    assert not any("missing recommended" in m for m in handler.messages())


def test_build_auth_headers_substring_only_does_not_satisfy_recommendation(provider, monkeypatch):
    """A bare mention like '__Secure-1PSID-other=v' must not count as having that cookie."""
    _forbid_open(monkeypatch)
    handler = _attach_capture(provider)
    # The cookie names parsed are the bit before '=' â€” make sure we match exactly.
    cookie = "__Secure-3PAPISID=a; __Secure-1PSID-typo=oops; SAPISID=b"
    provider._build_auth_headers(cookie)
    joined = " ".join(handler.messages())
    assert "__Secure-1PSID" in joined  # listed as missing


# ---------------------------------------------------------------------------
# Auth-lapse detection in library calls
# ---------------------------------------------------------------------------


def test_is_auth_lapse_detects_401(provider):
    assert provider._is_auth_lapse(RuntimeError("Server returned HTTP 401: Unauthorized")) is True


def test_is_auth_lapse_detects_unauthorized_text(provider):
    assert provider._is_auth_lapse(RuntimeError("Unauthorized access")) is True


def test_is_auth_lapse_ignores_non_auth_errors(provider):
    assert provider._is_auth_lapse(RuntimeError("Connection reset by peer")) is False
    assert provider._is_auth_lapse(RuntimeError("HTTP 500")) is False
    # 403 alone is intentionally not treated as auth lapse here â€” it has the
    # separate owned-playlist no-op path. Auth lapses surface as 401.
    assert provider._is_auth_lapse(RuntimeError("HTTP 403: Forbidden")) is False


@pytest.mark.parametrize(
    "message",
    [
        # Video ids and track titles pass through these messages routinely, and
        # "401" as a bare substring matched any of them. Telling someone their
        # cookie expired because a video id contained three digits sends them
        # to re-capture a cookie that was fine.
        "No formats found for abc401xyz",
        "get_song failed for 4012abcdefg",
        "Timeout after 401 seconds",
        "HTTP 500 while fetching 401k Podcast",
        "Playlist 'Top 401 of 2026' is unviewable",
    ],
)
def test_is_auth_lapse_does_not_fire_on_an_incidental_401(provider, message):
    assert provider._is_auth_lapse(RuntimeError(message)) is False


@pytest.mark.parametrize(
    "message",
    [
        "Server returned HTTP 401: Unauthorized",
        "401 Client Error: Unauthorized for url: https://music.youtube.com/...",
        "status_code=401",
        "status code: 401",
        "Unauthorized",
        "Please provide authentication before using this function",
        "authentication failed",
    ],
)
def test_is_auth_lapse_still_recognises_real_auth_errors(provider, message):
    assert provider._is_auth_lapse(RuntimeError(message)) is True


def test_library_error_warning_includes_refresh_hint_on_auth_lapse(provider):
    handler = _attach_capture(provider)
    provider._auth_lapse_warned = False
    provider._warn_library_error(
        "get_library_songs", RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    joined = " ".join(handler.messages())
    assert "refresh it" in joined.lower() or "cookie" in joined.lower()
    assert provider._auth_lapse_warned is True


def test_library_error_warning_does_not_spam_repeated_auth_errors(provider):
    handler = _attach_capture(provider)
    provider._auth_lapse_warned = False
    err = RuntimeError("Server returned HTTP 401: Unauthorized")
    provider._warn_library_error("get_library_songs", err)
    provider._warn_library_error("get_library_playlists", err)
    provider._warn_library_error("get_library_albums", err)
    warnings = [r for r in handler.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in handler.records]


def test_library_error_warning_uses_generic_message_for_non_auth_errors(provider):
    handler = _attach_capture(provider)
    provider._auth_lapse_warned = False
    provider._warn_library_error(
        "get_library_albums", RuntimeError("Connection timeout")
    )
    warnings = [r for r in handler.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "Connection timeout" in msg
    assert "cookie" not in msg.lower()
    assert provider._auth_lapse_warned is False


def test_get_library_playlists_propagates_auth_lapse_hint(provider):
    """The hint is logged, and the sync fails rather than reporting no playlists.

    It used to return an empty list here, which Music Assistant reads as an
    instruction to delete every playlist it holds for this provider (#64).
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_playlists = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    provider._ytmusic = mock

    with pytest.raises(RuntimeError, match="Unauthorized"):
        _consume(provider.get_library_playlists())

    joined = " ".join(handler.messages())
    assert "cookie" in joined.lower()


def test_get_library_albums_propagates_auth_lapse_hint(provider):
    """As above, for albums: the hint survives and the sync still fails."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_albums = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    provider._ytmusic = mock

    with pytest.raises(RuntimeError, match="Unauthorized"):
        _consume(provider.get_library_albums())

    joined = " ".join(handler.messages())
    assert "cookie" in joined.lower()


def test_recommendations_propagates_auth_lapse_hint(provider):
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_home = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    provider._ytmusic = mock

    result = asyncio.run(provider.recommendations())
    assert result == []
    joined = " ".join(handler.messages())
    assert "cookie" in joined.lower()


# ---------------------------------------------------------------------------
# Partial-auth empty-library detection (issue #10)
# ---------------------------------------------------------------------------


def test_probe_session_alive_true_when_account_info_has_name(provider):
    mock = MagicMock()
    mock.get_account_info = MagicMock(return_value={"accountName": "Someone"})
    provider._ytmusic = mock
    assert provider._probe_session_alive() is True


def test_probe_session_alive_false_when_account_info_missing_name(provider):
    mock = MagicMock()
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock
    assert provider._probe_session_alive() is False


def test_probe_session_alive_false_on_auth_lapse_error(provider):
    mock = MagicMock()
    mock.get_account_info = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 401: Unauthorized")
    )
    provider._ytmusic = mock
    assert provider._probe_session_alive() is False


def test_probe_session_alive_none_on_transient_error(provider):
    """A non-auth error must NOT be treated as a definite lapse signal."""
    mock = MagicMock()
    mock.get_account_info = MagicMock(side_effect=RuntimeError("Connection reset"))
    provider._ytmusic = mock
    assert provider._probe_session_alive() is None


def test_probe_session_alive_none_when_method_unavailable(provider):
    """Older ytmusicapi without get_account_info â€” undetermined, never False."""
    provider._ytmusic = object()  # bare object, no methods
    assert provider._probe_session_alive() is None


def test_probe_session_alive_none_when_ytmusic_unset(provider):
    provider._ytmusic = None
    assert provider._probe_session_alive() is None


def _consume(generator):
    async def _drain():
        return [item async for item in generator]

    return asyncio.run(_drain())


def _track_dict(video_id: str, title: str = "x") -> dict:
    return {
        "videoId": video_id,
        "title": title,
        "artists": [{"id": "UC1", "name": "A"}],
    }


def test_first_empty_library_sync_is_verified_then_accepted(provider):
    """A brand-new account with no liked songs still syncs to empty silently.

    It is now *verified* empty rather than assumed empty. The guard used to
    skip the probe unless the category had been seen populated earlier in this
    process, and that state resets on every init, so after a restart a lapsed
    cookie was indistinguishable from a new account and Music Assistant deleted
    the library to match. Issue #55.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(return_value=[])
    mock.get_account_info = MagicMock(return_value={"accountName": "Real User"})
    provider._ytmusic = mock

    result = _consume(provider.get_library_tracks())

    assert result == []
    assert mock.get_account_info.call_count == 1, (
        "an empty library must be verified against the session, not assumed"
    )
    warnings = [r for r in handler.records if r.levelname == "WARNING"]
    assert warnings == []


def test_empty_library_sync_raises_on_the_very_first_sync_after_a_lapse(provider):
    """The exact issue #55 shape: restart, then a first sync that is empty.

    No prior populated state exists in memory, so this is the case the old
    guard let through.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_songs = MagicMock(return_value=[])
    mock.get_account_info = MagicMock(return_value={})  # logged-out shape
    provider._ytmusic = mock

    with pytest.raises(RuntimeError, match="cookie lapse"):
        _consume(provider.get_library_tracks())


def test_repeated_empty_library_sync_stays_silent_while_the_session_is_alive(provider):
    """Empty â†’ empty on a live session must stay silent."""
    provider._authenticated = True
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(return_value=[])
    mock.get_account_info = MagicMock(return_value={"accountName": "x"})
    provider._ytmusic = mock

    _consume(provider.get_library_tracks())
    _consume(provider.get_library_tracks())

    # Probed each time. One request per empty sync is a trivial cost next to
    # the alternative, which is deleting someone's library on a bad guess.
    assert mock.get_account_info.call_count == 2
    assert [r for r in handler.records if r.levelname == "WARNING"] == []


def test_populated_then_empty_triggers_probe_and_raises_on_lapse(provider):
    """Once we've seen items, a later empty sync must probe and raise on lapse."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    # First call returns items, second returns empty (the lapse).
    mock.get_library_songs = MagicMock(side_effect=[[_track_dict("v1")], []])
    mock.get_account_info = MagicMock(return_value={})  # logged-out shape
    provider._ytmusic = mock

    first = _consume(provider.get_library_tracks())
    assert len(first) == 1

    with pytest.raises(RuntimeError, match="cookie lapse"):
        _consume(provider.get_library_tracks())

    assert mock.get_account_info.call_count == 1
    joined = " ".join(handler.messages()).lower()
    assert "cookie" in joined  # warning text should hint at cookie refresh


def test_populated_then_empty_does_not_raise_when_probe_alive(provider):
    """Probe confirms session â€” treat empty as a real empty library, no raise."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(side_effect=[[_track_dict("v1")], []])
    mock.get_account_info = MagicMock(return_value={"accountName": "Someone"})
    provider._ytmusic = mock

    _consume(provider.get_library_tracks())
    # Probe says alive â€” generator returns empty without raising.
    result = _consume(provider.get_library_tracks())
    assert result == []
    assert mock.get_account_info.call_count == 1
    assert [r for r in handler.records if r.levelname == "WARNING"] == []


def test_populated_then_empty_does_not_raise_on_undetermined_probe(provider):
    """Transient probe error must not raise â€” that would invent a false alarm.

    It does warn, though. This is the one remaining path that can still let a
    library be deleted, so it should not pass in silence.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(side_effect=[[_track_dict("v1")], []])
    mock.get_account_info = MagicMock(side_effect=RuntimeError("Connection timeout"))
    provider._ytmusic = mock

    _consume(provider.get_library_tracks())
    result = _consume(provider.get_library_tracks())
    assert result == []
    warnings = " ".join(
        r.getMessage() for r in handler.records if r.levelname == "WARNING"
    ).lower()
    assert "inconclusive" in warnings


def test_partial_auth_guard_covers_get_library_albums(provider):
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_albums = MagicMock(
        side_effect=[[{"browseId": "MPREb_x", "title": "A"}], []]
    )
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock

    _consume(provider.get_library_albums())
    with pytest.raises(RuntimeError, match="cookie lapse"):
        _consume(provider.get_library_albums())
    joined = " ".join(handler.messages()).lower()
    assert "cookie" in joined


def test_partial_auth_guard_covers_get_library_playlists(provider):
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_playlists = MagicMock(
        side_effect=[[{"id": "PL1", "title": "P"}], []]
    )
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock

    _consume(provider.get_library_playlists())
    with pytest.raises(RuntimeError, match="cookie lapse"):
        _consume(provider.get_library_playlists())


def test_partial_auth_guard_covers_get_library_artists(provider):
    """Artists generator combines subscriptions + library artists; guard sees total."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_subscriptions = MagicMock(
        side_effect=[[{"channelId": "UC1", "name": "A"}], []]
    )
    mock.get_library_artists = MagicMock(side_effect=[[], []])
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock

    first = _consume(provider.get_library_artists())
    assert len(first) == 1
    with pytest.raises(RuntimeError, match="cookie lapse"):
        _consume(provider.get_library_artists())


def test_get_library_artists_parses_browse_id_and_artist_keys(provider):
    """Subscriptions/library artists keyed browseId/artist parse without pre-mapping."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_subscriptions = MagicMock(
        return_value=[{"browseId": "UCsub", "artist": "Subscribed Artist"}]
    )
    mock.get_library_artists = MagicMock(
        return_value=[{"browseId": "UClib", "artist": "Library Artist"}]
    )
    mock.get_account_info = MagicMock(return_value={})
    provider._ytmusic = mock

    artists = _consume(provider.get_library_artists())
    assert {a.item_id: a.name for a in artists} == {
        "UCsub": "Subscribed Artist",
        "UClib": "Library Artist",
    }


def _cookie_configured(provider, auth_type=None):
    """Point provider.config at a cookie-auth configuration."""
    from ytmusic_free import AUTH_TYPE_COOKIE

    cfg = MagicMock()
    cfg.get_value = lambda key, *a, **k: (
        (auth_type or AUTH_TYPE_COOKIE) if key == ytm.CONF_AUTH_TYPE else None
    )
    provider.config = cfg


@pytest.mark.parametrize(
    ("method", "category"),
    [
        ("get_library_tracks", "tracks"),
        ("get_library_albums", "albums"),
        ("get_library_artists", "artists"),
        ("get_library_playlists", "playlists"),
        ("get_library_podcasts", "podcasts"),
    ],
)
def test_unauthenticated_library_sync_fails_instead_of_reporting_empty(
    provider, method, category
):
    """Issue #55, the larger half: an unauthenticated sync must not report empty.

    Music Assistant treats a completed sync as authoritative and deletes
    anything it held that the provider did not return, unfavouriting whatever
    is left unclaimed. The provider declares its library features
    unconditionally at setup, so a failed cookie does not stop MA asking. The
    old silent early return therefore answered "you have nothing", and MA
    obediently emptied the library.
    """
    provider._authenticated = False
    provider._auth_lapse_warned = False
    _cookie_configured(provider)

    with pytest.raises(RuntimeError, match="not active"):
        _consume(getattr(provider, method)())


def test_anonymous_instance_still_syncs_empty_without_raising(provider):
    """A deliberately anonymous instance has no library and never had one.

    Raising there would put a permanent error in the log of every anonymous
    install, for a deletion that cannot happen.
    """
    provider._authenticated = False
    _cookie_configured(provider, auth_type=ytm.AUTH_TYPE_NONE)

    assert _consume(provider.get_library_tracks()) == []


def test_unauthenticated_guard_warns_with_a_cookie_hint(provider):
    provider._authenticated = False
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    _cookie_configured(provider)

    with pytest.raises(RuntimeError):
        _consume(provider.get_library_tracks())

    joined = " ".join(handler.messages()).lower()
    assert "cookie" in joined


def test_guard_checks_every_empty_category_regardless_of_history(provider):
    """A category never seen populated is still checked when it comes back empty.

    This inverts the old behaviour deliberately. Gating on per-category history
    meant an account whose playlists happened to be empty on the sync before a
    lapse would have its playlists deleted without a single check. The state it
    gated on lived only in memory, so a restart put *every* category in that
    position, which is issue #55.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_songs = MagicMock(return_value=[_track_dict("v1")])
    mock.get_library_playlists = MagicMock(return_value=[])
    mock.get_account_info = MagicMock(return_value={})  # logged-out shape
    provider._ytmusic = mock

    # Tracks came back populated, so no probe for that category.
    _consume(provider.get_library_tracks())
    assert mock.get_account_info.call_count == 0

    # Playlists came back empty. Never populated in this process, and checked
    # anyway.
    with pytest.raises(RuntimeError, match="cookie lapse"):
        _consume(provider.get_library_playlists())
    assert mock.get_account_info.call_count == 1


# ---------------------------------------------------------------------------
# A failed library fetch fails the sync (issue #64)
#
# The #55 work closed two of the three routes to a completed-but-empty sync:
# an unauthenticated run, and an empty result the session probe blames on a
# lapsed cookie. The third stayed open. When the fetch itself raised, the
# generator logged and returned, which is a *successful* sync of zero items,
# and Music Assistant deletes everything a successful sync did not return.
# A rate limit therefore emptied the library exactly the way an expired cookie
# used to.
# ---------------------------------------------------------------------------


def _consume_until_error(generator):
    """Drain a generator, returning (items yielded, error raised or None).

    Needed because the point of several tests below is that *nothing* is
    yielded before the raise: pytest.raises alone cannot tell "raised without
    yielding" apart from "yielded half a library and then raised".
    """
    collected = []

    async def _drain():
        try:
            async for item in generator:
                collected.append(item)
        except Exception as err:  # noqa: BLE001 - the error is the assertion
            return err
        return None

    return collected, asyncio.run(_drain())


# (provider method, the ytmusicapi method it fetches through)
_LIBRARY_FETCHES = [
    ("get_library_tracks", "get_library_songs"),
    ("get_library_albums", "get_library_albums"),
    ("get_library_playlists", "get_library_playlists"),
    ("get_library_podcasts", "get_library_podcasts"),
    ("get_library_artists", "get_library_subscriptions"),
]


@pytest.mark.parametrize(("method", "client_method"), _LIBRARY_FETCHES)
def test_a_failed_library_fetch_fails_the_sync_instead_of_reporting_empty(
    provider, method, client_method
):
    """Every library category, one rule: a fetch we could not complete raises."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    setattr(
        mock,
        client_method,
        MagicMock(side_effect=RuntimeError("Server returned HTTP 503: Service Unavailable")),
    )
    provider._ytmusic = mock

    with pytest.raises(RuntimeError, match="503"):
        _consume(getattr(provider, method)())


@pytest.mark.parametrize(("method", "client_method"), _LIBRARY_FETCHES)
def test_a_failed_library_fetch_yields_nothing_before_it_raises(
    provider, method, client_method
):
    """Fail before the first yield, not halfway through.

    Music Assistant commits each item as it arrives, so a half-drained sync
    leaves writes behind for items it is about to disown. Fetching everything
    up front and raising before the loop keeps that from happening.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    setattr(mock, client_method, MagicMock(side_effect=RuntimeError("boom")))
    provider._ytmusic = mock

    items, err = _consume_until_error(getattr(provider, method)())

    assert items == []
    assert isinstance(err, RuntimeError)


def test_a_failed_library_fetch_logs_the_underlying_error_before_raising(provider):
    """Raising must not cost the diagnostic that says what actually broke."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(side_effect=RuntimeError("Connection timeout"))
    provider._ytmusic = mock

    with pytest.raises(RuntimeError, match="Connection timeout"):
        _consume(provider.get_library_tracks())

    joined = " ".join(handler.messages())
    assert "get_library_songs" in joined
    assert "Connection timeout" in joined
    # Not an auth error, so it must not be reported as an expired cookie.
    assert "cookie" not in joined.lower()
    assert provider._auth_lapse_warned is False


def test_a_failed_library_fetch_does_not_probe_the_session(provider):
    """A failed fetch is not an empty library, so the cookie is not the suspect.

    The probe exists to explain an empty result. Running it here would spend a
    request and could pin a network failure on the user's cookie.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_albums = MagicMock(side_effect=RuntimeError("boom"))
    provider._ytmusic = mock

    with pytest.raises(RuntimeError):
        _consume(provider.get_library_albums())

    assert mock.get_account_info.call_count == 0


def test_a_failed_library_fetch_does_not_mark_the_category_as_populated(provider):
    """A library we failed to read tells us nothing about whether it has items."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    provider._library_seen_nonempty = {}
    mock = MagicMock()
    mock.get_library_albums = MagicMock(side_effect=RuntimeError("boom"))
    provider._ytmusic = mock

    with pytest.raises(RuntimeError):
        _consume(provider.get_library_albums())

    assert provider._library_seen_nonempty == {}


def test_get_library_artists_fails_when_the_subscriptions_fetch_fails(provider):
    """Half a result is not a library.

    Artists come from two calls. The empty-library guard used to run on their
    combined length, so a failed subscriptions call sitting next to one
    successful library artist produced a non-zero count, a clean sync, and the
    deletion of every subscribed artist.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_subscriptions = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 429: Too Many Requests")
    )
    mock.get_library_artists = MagicMock(
        return_value=[{"browseId": "UC1", "artist": "Still Here"}]
    )
    provider._ytmusic = mock

    items, err = _consume_until_error(provider.get_library_artists())

    assert items == []
    assert isinstance(err, RuntimeError)
    assert "429" in str(err)


def test_get_library_artists_fails_when_the_library_artists_fetch_fails(provider):
    """The mirror case, so neither half of the artist sync is privileged."""
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_subscriptions = MagicMock(
        return_value=[{"browseId": "UC1", "artist": "Still Here"}]
    )
    mock.get_library_artists = MagicMock(
        side_effect=RuntimeError("Server returned HTTP 500: Internal Server Error")
    )
    provider._ytmusic = mock

    items, err = _consume_until_error(provider.get_library_artists())

    assert items == []
    assert isinstance(err, RuntimeError)
    assert "500" in str(err)


def test_get_library_artists_skips_the_second_fetch_once_the_first_has_failed(provider):
    """Whatever took out one call is about to take out the other.

    Its result would be discarded by the raise anyway, and retrying into a rate
    limit is how a short one becomes a long one.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_subscriptions = MagicMock(side_effect=RuntimeError("boom"))
    mock.get_library_artists = MagicMock(return_value=[])
    provider._ytmusic = mock

    with pytest.raises(RuntimeError):
        _consume(provider.get_library_artists())

    assert mock.get_library_artists.call_count == 0


def test_a_missing_ytmusicapi_library_method_fails_with_an_upgrade_hint(provider):
    """An ytmusicapi too old for podcasts must not read as "no subscriptions".

    The provider asks pip for `ytmusicapi` with no version, so an install that
    already had an older copy keeps it and never reaches the manifest floor.

    The hint has to name the restart as well as the upgrade. The module is
    resolved through ``importlib.import_module``, which hands back whatever is
    already in ``sys.modules``, so an in-place pip upgrade is invisible to the
    running process and a provider reload does not replace it either. A hint
    that stops at "upgrade" sends the user round the loop a second time.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    provider._ytmusic = MagicMock(spec=["get_library_songs"])

    with pytest.raises(RuntimeError, match="ytmusicapi"):
        _consume(provider.get_library_podcasts())

    joined = " ".join(handler.messages()).lower()
    assert "upgrade" in joined
    assert "restart" in joined


def test_a_library_fetch_of_an_unexpected_shape_fails_rather_than_being_counted(provider):
    """A shape we cannot count is a shape we cannot vouch for.

    len() of a dict counts its keys, so a changed response would clear the
    empty-library guard on a non-zero count and then yield nothing, which is
    precisely the deletion this all exists to prevent.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_albums = MagicMock(return_value={"contents": [], "continuation": "x"})
    provider._ytmusic = mock

    with pytest.raises(TypeError, match="expected a list"):
        _consume(provider.get_library_albums())


def test_a_library_fetch_returning_none_is_still_an_empty_library(provider):
    """None is how ytmusicapi spells "nothing here", not a failure.

    Regression guard for the `or []` that moved into the fetch helper: an
    account with no saved albums must still sync cleanly rather than raise.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_albums = MagicMock(return_value=None)
    mock.get_account_info = MagicMock(return_value={"accountName": "Someone"})
    provider._ytmusic = mock

    assert _consume(provider.get_library_albums()) == []


def test_a_cancelled_library_sync_is_not_logged_as_a_library_failure(provider):
    """Shutdown is not a library failure.

    Music Assistant cancels the sync task when it stops. CancelledError is a
    BaseException, so `except Exception` in the fetch helper lets it through
    untouched. This pins that: widening the catch would fill the log with
    library errors on every restart and would swallow the cancellation.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    handler = _attach_capture(provider)
    mock = MagicMock()
    mock.get_library_songs = MagicMock(side_effect=asyncio.CancelledError())
    provider._ytmusic = mock

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider._fetch_library("get_library_songs", limit=1))

    assert handler.messages() == []


def test_one_unparseable_library_item_does_not_fail_the_whole_sync(provider):
    """Per-item tolerance is deliberate and must survive this change.

    The fix is about a fetch that could not answer. An item the parser cannot
    read is a different thing: the rest of the library is still good, and
    failing the sync over one bad row would be the more destructive choice.
    """
    provider._authenticated = True
    provider._auth_lapse_warned = False
    mock = MagicMock()
    mock.get_library_songs = MagicMock(return_value=[_track_dict("v1"), {"title": "no id"}])
    provider._ytmusic = mock

    tracks = _consume(provider.get_library_tracks())

    assert [t.item_id for t in tracks] == ["v1"]
