"""Assert the provider still loads against the real Music Assistant server package.

Run inside the image built by ``Dockerfile``, which is
``FROM ghcr.io/music-assistant/server:<MA_VERSION>`` with the provider copied into
site-packages, so the interpreter there has the genuine server package::

    docker run --rm -i --entrypoint /app/venv/bin/python \\
        local-test-image:latest - < tests/verify_server_contract.py

Nothing else in CI executes these imports. ``tests/python`` replaces
``music_assistant`` with a stub in ``conftest.py``, and the models-contract job
installs only ``music_assistant_models``. The server package is not published on
PyPI at all (upstream ships it as a GitHub release asset), so this image is the
only cheap place a real import can happen. Issue #66.

Deliberately not folded into ``tests/ma_contract.py``. That table describes
``music_assistant_models`` and is imported by two pytest suites; this runs inside
the container, where the tests directory is not mounted. Staying self-contained
is what lets it be piped in on stdin.

Not named ``test_*.py`` on purpose: ``pytest.ini`` sets ``testpaths = tests/python``
and ``python_files = test_*.py``, and this must not be collected by a suite whose
whole premise is that the server package is stubbed out.
"""

from __future__ import annotations

import inspect

# Parameters of ``music_assistant.controllers.cache.use_cache`` that the provider
# passes. It decorates eleven methods, every one of them as
# ``@use_cache(<int>, allow_expired_cache=True)``, and the three long-lived
# lookups that return tracks also pass ``cache_checksum`` so a parser change can
# retire their 30-day entries (issue #90). Losing any of these names is a
# TypeError raised while the class body executes.
REQUIRED_USE_CACHE_PARAMS = ("expiration", "allow_expired_cache", "cache_checksum")

# Not passed by the provider, and that is exactly why it needs asserting here.
# ``get_playlist_tracks`` is only correct because Music Assistant bypasses the
# cache for playback and refill: browsing a mix gets the stable cached list while
# playing it gets a freshly rolled one. That behaviour is ``allow_bypass``
# defaulting to on, plus the ``BYPASS_CACHE`` context variable. If it went away,
# dynamic playlists would quietly freeze for three hours at a time, which is
# issue #56 reopened, and every other check here would stay green.
REQUIRED_BYPASS_NAMES = ("allow_bypass",)


def main() -> None:
    """Import the provider for real, then check what the import cannot prove."""
    import music_assistant.providers.ytmusic_free as provider

    # That import is most of the contract on its own, and it is worth being
    # explicit about how much it covers. Both feature sets are built at module
    # scope from ``ProviderFeature`` attribute access, and the ``@use_cache(...)``
    # decorators are evaluated while the class body executes, so a dropped enum
    # member or a changed decorator signature fails on this line rather than in
    # front of a user. It also covers the other four server symbols the provider
    # imports: infer_album_type, install_package, parse_title_and_version and
    # MusicProvider.
    print(f"provider imported from {provider.__file__}")

    from music_assistant.controllers.cache import BYPASS_CACHE, use_cache

    params = inspect.signature(use_cache).parameters
    missing = [
        name for name in (*REQUIRED_USE_CACHE_PARAMS, *REQUIRED_BYPASS_NAMES) if name not in params
    ]
    if missing:
        raise SystemExit(
            f"use_cache lost parameters the provider relies on: {missing}. "
            "See tests/verify_server_contract.py for why each one matters, and "
            "issue #66."
        )

    print(f"use_cache signature ok: {', '.join(params)}")
    print(f"BYPASS_CACHE ok: {BYPASS_CACHE!r}")

    check_auth_notice_contract()
    print("server contract ok")


def check_auth_notice_contract() -> None:
    """Check the server pieces the cookie auth notice stands on (issue #92).

    The provider guards every one of these with getattr, so losing one does not
    break loading. It silently brings back the failure the notice exists to
    end: cookie auth falls over and nothing in the UI says so. Hence a check
    here rather than a crash there.
    """
    from music_assistant_models.config_entries import ProviderConfig, ProviderError
    from music_assistant_models.enums import ProviderStatus, ProviderType
    from music_assistant_models.errors import LoginFailed, SetupFailedError

    from music_assistant.controllers.config import ConfigController
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.provider import Provider

    update = getattr(ConfigController, "update_provider_last_error", None)
    if not callable(update):
        raise SystemExit(
            "ConfigController.update_provider_last_error is gone; the cookie auth "
            "notice can no longer be shown. See issue #92."
        )
    # The provider calls it as update(instance_id, error); a TypeError there is
    # swallowed into a debug log.
    try:
        inspect.signature(update).bind(None, "instance_id", None)
    except TypeError as err:
        raise SystemExit(
            f"update_provider_last_error no longer takes (instance_id, error): {err}"
        ) from err
    # _sync_auth_notice checks the instance is still registered through this
    # property and skips the write without it.
    if not isinstance(inspect.getattr_static(MusicAssistant, "providers", None), property):
        raise SystemExit("MusicAssistant.providers is gone; the auth notice is never written.")
    if "data" not in inspect.signature(MusicAssistant.signal_event).parameters:
        raise SystemExit("MusicAssistant.signal_event lost its data parameter.")
    for hook in ("loaded_in_mass", "unload"):
        if not callable(getattr(Provider, hook, None)):
            raise SystemExit(f"Provider base lost {hook}(); the auth notice relies on it.")
    if "is_removed" not in inspect.signature(Provider.unload).parameters:
        raise SystemExit("Provider.unload lost its is_removed parameter.")
    # ``initialized`` is assigned in __init__, so it is checked in the source
    # rather than on the class.
    if "self.initialized" not in inspect.getsource(Provider.__init__):
        raise SystemExit("Provider base no longer sets self.initialized.")

    # The badge the notice is meant to carry. _provider_status is private, so a
    # rename only skips this part; a changed mapping fails it.
    try:
        from music_assistant.controllers.config.helpers import _provider_status
    except ImportError:
        print("auth notice: _provider_status not found, status mapping not checked")
    else:
        expected = {
            LoginFailed.error_code: ProviderStatus.AUTH_REQUIRED,
            # The startup notice that does not blame the cookie. ERROR is the
            # status the provider settings page offers its Reload button on.
            SetupFailedError.error_code: ProviderStatus.ERROR,
        }
        for code, wanted in expected.items():
            conf = ProviderConfig(
                values={},
                type=ProviderType.MUSIC,
                domain="ytmusic_free",
                instance_id="ytmusic_free--contract",
                last_error=ProviderError(error_code=code, message="notice"),
            )
            status = _provider_status(conf, True)
            if status != wanted:
                raise SystemExit(
                    f"a loaded provider with last_error code {code} now reads {status}, "
                    f"not {wanted}; revisit the error codes in _sync_auth_notice."
                )
    print("auth notice contract ok")


if __name__ == "__main__":
    main()
