"""Hold our own side of the contract to the same table as the real package.

``tests/python_integration/test_models_contract.py`` asserts that the genuine
``music_assistant_models`` matches ``tests/ma_contract.py``. This file asserts
that our side matches it too, which is the half that catches drift introduced
here rather than upstream.

Mostly that means the stand-ins in ``conftest.py``. Without those checks the
stubs can be edited into something more forgiving than reality while every
other test in this directory keeps passing. That is not hypothetical: a stub
with an invented ``ContentType.WEBM`` is why the suite was green while real
Music Assistant reported every Opus stream as ``"?"``.

The last two tests look at the provider rather than the stubs, and they live
here because this is the only suite that can import it at all: ``ytmusic_free``
imports the ``music_assistant`` *server* package, which the models-contract job
does not install. They are what stops ``REQUIRED_PROVIDER_FEATURE_MEMBERS`` from
going stale, so the job that does see real upstream is always checking the
features the provider really declares. See issue #65.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

import pytest

# Imports below are resolved against the stubs registered in conftest.py.
from music_assistant_models.config_entries import ProviderError
from music_assistant_models.enums import ContentType, EventType, MediaType, StreamType
from music_assistant_models.errors import LoginFailed

# Imported from media_items, not streamdetails, because that is the path the
# provider itself uses. Upstream re-exports the same class from both.
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    Podcast,
    PodcastEpisode,
)
from music_assistant_models.streamdetails import StreamDetails

# Resolves because conftest.py registers the stubs and puts the project root on
# sys.path before collection, the same way test_provider.py imports it.
import ytmusic_free as ytm

TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import ma_contract  # noqa: E402

# Every literal ``ProviderFeature.MEMBER`` in the provider source. Used only to
# find references the two feature sets do not account for, never to build the
# contract itself, which is what keeps it from failing silently: see
# test_the_provider_names_features_only_inside_its_two_sets.
_FEATURE_REFERENCE = re.compile(r"\bProviderFeature\.([A-Z][A-Z0-9_]*)")


def test_stub_is_actually_the_stub():
    """Guard against this file silently testing the real package instead."""
    assert getattr(sys.modules["music_assistant_models"], "__file__", None) is None, (
        "music_assistant_models resolved to a real installed package here; the "
        "unit suite is meant to run against the conftest stubs"
    )


def test_stub_has_the_required_members():
    missing = [
        name
        for name in ma_contract.REQUIRED_CONTENT_TYPE_MEMBERS
        if not hasattr(ContentType, name)
    ]
    assert not missing, f"stub ContentType is missing {missing}; see tests/ma_contract.py"


def test_stub_does_not_invent_members_upstream_lacks():
    """The exact failure that made the old suite lie."""
    present = [
        name
        for name in ma_contract.FORBIDDEN_CONTENT_TYPE_MEMBERS
        if hasattr(ContentType, name)
    ]
    assert not present, (
        f"stub ContentType invented {present}, which upstream does not have. A "
        "test double that is kinder than reality tests nothing."
    )


def test_stub_unknown_sentinel_matches():
    assert ContentType.UNKNOWN.value == ma_contract.UNKNOWN_VALUE


@pytest.mark.parametrize(
    ("raw", "expected_member"), sorted(ma_contract.TRY_PARSE_EXPECTATIONS.items())
)
def test_stub_try_parse_matches_the_contract(raw, expected_member):
    assert ContentType.try_parse(raw) is getattr(ContentType, expected_member)


def test_stub_audio_format_has_the_required_fields():
    fields = {f.name for f in dataclasses.fields(AudioFormat)}
    missing = set(ma_contract.REQUIRED_AUDIO_FORMAT_FIELDS) - fields
    assert not missing, f"stub AudioFormat is missing {missing}"


def test_stub_bit_rate_defaults_to_none():
    assert AudioFormat().bit_rate is None


def test_stub_media_type_has_the_required_members():
    missing = [
        name for name in ma_contract.REQUIRED_MEDIA_TYPE_MEMBERS if not hasattr(MediaType, name)
    ]
    assert not missing, f"stub MediaType is missing {missing}; see tests/ma_contract.py"


def test_stub_stream_type_has_the_required_members():
    missing = [
        name for name in ma_contract.REQUIRED_STREAM_TYPE_MEMBERS if not hasattr(StreamType, name)
    ]
    assert not missing, f"stub StreamType is missing {missing}; see tests/ma_contract.py"


# Deliberately not asserted: that the stub ``ProviderFeature`` carries every name
# in ``REQUIRED_PROVIDER_FEATURE_MEMBERS``, the way the checks above do for their
# own types. That test could only run in a world where it already passes. The
# provider builds both feature sets at module scope, so a stub missing a member
# raises AttributeError during ``import ytmusic_free``, which every module in
# this directory does at import time; the whole suite would fail to collect
# before any assertion ran. The two tests below cover the properties that are not
# already forced this way. If features ever stop being built at module scope, the
# plain stub check becomes worth adding.


def test_the_contract_lists_exactly_the_features_the_provider_declares():
    """Keep the hand-written tuple in step with the sets it describes.

    ``tests/python_integration`` checks those names against real upstream but
    cannot import the provider, because ``ytmusic_free`` imports the
    ``music_assistant`` server package and that job installs only
    ``music_assistant_models``. So the half that proves the tuple still
    describes the provider has to live here.

    Equality, not a subset, and the two directions are not equally serious. A
    feature declared but unpinned is the blind spot issue #65 is about: nothing
    would ever check it against real upstream, and the day it disappears the
    provider stops importing with a green CI behind it. A pin with nothing
    behind it is milder, but it can fail the contract job over a member we no
    longer use, and this table is only worth having while every line in it is
    still true.
    """
    declared = {feature.name for feature in ytm.BASE_FEATURES | ytm.AUTHENTICATED_FEATURES}
    pinned = set(ma_contract.REQUIRED_PROVIDER_FEATURE_MEMBERS)
    assert declared == pinned, (
        "the provider and tests/ma_contract.py have diverged. Declared but not "
        f"pinned, so never checked against real upstream: {sorted(declared - pinned)}; "
        f"pinned but no longer declared: {sorted(pinned - declared)}"
    )


def test_the_provider_names_features_only_inside_its_two_sets():
    """A member named anywhere else would be checked by nothing.

    The test above derives the contract from the two sets, which is exact for
    everything they contain and blind to a ``ProviderFeature.X`` written in a
    method body: that line never runs at import, so the member can be absent
    upstream and absent from the stub and still reach production as an
    AttributeError on one code path.

    A text sweep of the package covers that, and it is safe here in a way that
    parsing the file to *build* the contract would not be. This only looks for
    extras; the contract still comes from the imported sets. The first assertion
    is what makes that hold: if the sweep is ever defeated, by an aliased import
    or anything else, it stops seeing the members we know are declared and says
    so, instead of quietly finding nothing.
    """
    named: set[str] = set()
    for module in sorted(Path(ytm.__file__).parent.rglob("*.py")):
        named.update(_FEATURE_REFERENCE.findall(module.read_text(encoding="utf-8")))

    declared = {feature.name for feature in ytm.BASE_FEATURES | ytm.AUTHENTICATED_FEATURES}
    unseen = sorted(declared - named)
    assert not unseen, (
        f"scanning the provider source did not find {unseen}, which it demonstrably "
        "declares. The scan below is no longer reading the provider the way it is "
        "written, so it can no longer be trusted to spot a stray reference."
    )

    stray = sorted(named - declared)
    assert not stray, (
        f"the provider names {stray} outside BASE_FEATURES and AUTHENTICATED_FEATURES. "
        "Nothing checks those against real upstream, so add them to a feature set or "
        "pin them another way; a member that only exists in a method body still "
        "raises AttributeError if upstream drops it."
    )


def test_stub_item_mapping_has_the_required_fields():
    fields = {f.name for f in dataclasses.fields(ItemMapping)}
    missing = set(ma_contract.REQUIRED_ITEM_MAPPING_FIELDS) - fields
    assert not missing, f"stub ItemMapping is missing {missing}"


def test_stub_podcast_has_the_required_fields():
    fields = {f.name for f in dataclasses.fields(Podcast)}
    missing = set(ma_contract.REQUIRED_PODCAST_FIELDS) - fields
    assert not missing, f"stub Podcast is missing {missing}"


def test_stub_podcast_episode_has_the_required_fields():
    fields = {f.name for f in dataclasses.fields(PodcastEpisode)}
    missing = set(ma_contract.REQUIRED_PODCAST_EPISODE_FIELDS) - fields
    assert not missing, f"stub PodcastEpisode is missing {missing}"


def test_stub_stream_details_has_the_required_fields():
    fields = {f.name for f in dataclasses.fields(StreamDetails)}
    missing = set(ma_contract.REQUIRED_STREAM_DETAILS_FIELDS) - fields
    assert not missing, f"stub StreamDetails is missing {missing}"


def test_stub_stream_details_does_not_invent_a_delayed_availability_field():
    """The stub must not offer an escape hatch upstream does not have.

    ``get_stream_details`` blocks on a pre-roll ad window because there is no
    field to hand the timestamp to (issue #51). A stub that invented one would
    let a "cleaner" fix pass the unit suite and break in production.
    """
    fields = {f.name for f in dataclasses.fields(StreamDetails)}
    present = sorted(fields.intersection(ma_contract.FORBIDDEN_STREAM_DETAILS_FIELDS))
    assert not present, (
        f"stub StreamDetails invented {present}, which upstream does not have"
    )


def test_stub_event_type_has_the_required_members():
    missing = [
        name for name in ma_contract.REQUIRED_EVENT_TYPE_MEMBERS if not hasattr(EventType, name)
    ]
    assert not missing, f"stub EventType is missing {missing}; see tests/ma_contract.py"


def test_stub_provider_error_has_the_required_fields():
    fields = {f.name for f in dataclasses.fields(ProviderError)}
    missing = set(ma_contract.REQUIRED_PROVIDER_ERROR_FIELDS) - fields
    assert not missing, f"stub ProviderError is missing {missing}"


def test_stub_provider_error_leaves_the_translation_key_unset():
    assert ProviderError(error_code=1, message="x").translation_key is None


def test_stub_login_failed_code_matches():
    assert LoginFailed.error_code == ma_contract.LOGIN_FAILED_ERROR_CODE
