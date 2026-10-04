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
    print("server contract ok")


if __name__ == "__main__":
    main()
