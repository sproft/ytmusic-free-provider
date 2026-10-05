"""Pytest setup for the contract tests that run against the real package.

Everything under ``tests/python`` runs against hand-written stand-ins for
``music_assistant_models``. That keeps the unit suite fast, offline and
installable in CI, but it also means the suite is only ever as correct as the
stubs are. This directory is the counterweight: a small suite that imports the
genuine package and checks the handful of upstream facts the provider leans on.

It lives outside ``tests/python`` on purpose. That directory's ``conftest.py``
installs its stubs into ``sys.modules`` at import time, so any test collected
under it would get the stub no matter what it asked for.

``pytest.ini`` sets ``testpaths = tests/python``, so this suite is not part of
a default run. Invoke it explicitly:

    python -m pytest tests/python_integration

Set ``MA_MODELS_REQUIRED=1`` to turn "package not installed" from a skip into a
failure. CI sets it, so a broken install cannot quietly pass as a green skip.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


# ``tests/ma_contract.py`` is shared with the stub-side test under tests/python.
TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))


REQUIRE_REAL_MODELS = os.environ.get("MA_MODELS_REQUIRED") == "1"

_STUBBED_PACKAGE = "music_assistant_models"


def _installed_submodules() -> dict[str, object]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == _STUBBED_PACKAGE or name.startswith(f"{_STUBBED_PACKAGE}.")
    }


@pytest.fixture(scope="module")
def real_models():
    """Import the genuine ``music_assistant_models``, bypassing any stubs.

    Removes anything already registered under that name, imports fresh from
    site-packages, then restores the previous state on teardown so a mixed
    invocation (``pytest tests/``) cannot leave the unit suite without its
    stubs.
    """
    saved = _installed_submodules()
    for name in saved:
        del sys.modules[name]

    try:
        try:
            enums = importlib.import_module(f"{_STUBBED_PACKAGE}.enums")
            media_items = importlib.import_module(f"{_STUBBED_PACKAGE}.media_items")
            streamdetails = importlib.import_module(f"{_STUBBED_PACKAGE}.streamdetails")
            config_entries = importlib.import_module(f"{_STUBBED_PACKAGE}.config_entries")
            errors = importlib.import_module(f"{_STUBBED_PACKAGE}.errors")
        except ImportError as err:
            message = f"{_STUBBED_PACKAGE} is not installed: {err}"
            if REQUIRE_REAL_MODELS:
                pytest.fail(f"MA_MODELS_REQUIRED=1 but {message}")
            pytest.skip(message)

        # A module built by types.ModuleType has no __file__. If a stub reached
        # us anyway we would be asserting the stub against itself, which proves
        # exactly nothing, so refuse rather than report a false green.
        if not getattr(enums, "__file__", None):
            pytest.fail(
                f"{_STUBBED_PACKAGE}.enums has no __file__, so it is a stub "
                "rather than the real package. Run this suite in its own "
                "pytest invocation: python -m pytest tests/python_integration"
            )

        yield SimpleNamespace(
            enums=enums,
            media_items=media_items,
            streamdetails=streamdetails,
            config_entries=config_entries,
            errors=errors,
        )
    finally:
        for name in list(_installed_submodules()):
            del sys.modules[name]
        sys.modules.update(saved)
