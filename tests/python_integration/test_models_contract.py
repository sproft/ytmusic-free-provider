"""Check the real music_assistant_models against tests/ma_contract.py.

These assertions are the ones the unit suite structurally cannot make, because
it replaces the package with stubs before importing the provider. Everything
here is about upstream behaviour the provider silently depends on; a failure
means Music Assistant changed under us, and both the provider and the stubs in
``tests/python/conftest.py`` need attention.
"""

from __future__ import annotations

import dataclasses

import pytest

import ma_contract


def test_content_type_has_the_members_the_provider_uses(real_models):
    content_type = real_models.enums.ContentType
    missing = [
        name
        for name in ma_contract.REQUIRED_CONTENT_TYPE_MEMBERS
        if not hasattr(content_type, name)
    ]
    assert not missing, f"upstream ContentType lost members the provider uses: {missing}"


def test_content_type_still_has_no_webm_member(real_models):
    """The reason ``_get_stream_format`` falls back to the codec.

    yt-dlp hands us Opus inside a WebM container. If upstream ever adds WEBM,
    the fallback becomes unnecessary and this test is the reminder to drop it.
    """
    content_type = real_models.enums.ContentType
    present = [
        name
        for name in ma_contract.FORBIDDEN_CONTENT_TYPE_MEMBERS
        if hasattr(content_type, name)
    ]
    assert not present, (
        f"upstream ContentType gained {present}; the codec fallback in "
        "_get_stream_format can probably be simplified now"
    )


def test_unknown_sentinel_value_is_unchanged(real_models):
    assert real_models.enums.ContentType.UNKNOWN.value == ma_contract.UNKNOWN_VALUE


@pytest.mark.parametrize(
    ("raw", "expected_member"), sorted(ma_contract.TRY_PARSE_EXPECTATIONS.items())
)
def test_try_parse_matches_the_contract(real_models, raw, expected_member):
    """Every container/codec string the provider can produce, parsed for real."""
    content_type = real_models.enums.ContentType
    assert content_type.try_parse(raw) is getattr(content_type, expected_member)


def test_audio_format_is_importable_from_the_path_the_provider_uses(real_models):
    """The provider imports AudioFormat from media_items, StreamDetails from
    streamdetails. Upstream re-exports AudioFormat from both, and the stub
    only registers it on media_items, so pin the arrangement we rely on.
    """
    assert real_models.media_items.AudioFormat is real_models.streamdetails.AudioFormat


def test_audio_format_exposes_the_fields_the_provider_sets(real_models):
    fields = {f.name for f in dataclasses.fields(real_models.media_items.AudioFormat)}
    missing = set(ma_contract.REQUIRED_AUDIO_FORMAT_FIELDS) - fields
    assert not missing, f"upstream AudioFormat lost fields the provider sets: {missing}"


def test_audio_format_accepts_the_values_the_provider_assigns(real_models):
    """Construct one the way the provider does, with a real Opus stream's data."""
    audio_format = real_models.media_items.AudioFormat(
        content_type=real_models.enums.ContentType.OPUS,
    )
    audio_format.sample_rate = 48000
    audio_format.channels = 2
    audio_format.bit_rate = 160
    assert audio_format.content_type is real_models.enums.ContentType.OPUS
    assert audio_format.bit_rate == 160


def test_media_type_has_the_members_the_provider_uses(real_models):
    media_type = real_models.enums.MediaType
    missing = [
        name for name in ma_contract.REQUIRED_MEDIA_TYPE_MEMBERS if not hasattr(media_type, name)
    ]
    assert not missing, f"upstream MediaType lost members the provider uses: {missing}"


def test_stream_type_has_the_members_the_provider_uses(real_models):
    """CUSTOM is what all playback stands on since the bounded-range fix.

    googlevideo refuses the unbounded fetches ffmpeg makes, so the provider
    streams audio itself through ``get_audio_stream``, and Music Assistant
    only calls that hook when the stream_type is CUSTOM.
    """
    stream_type = real_models.enums.StreamType
    missing = [
        name for name in ma_contract.REQUIRED_STREAM_TYPE_MEMBERS if not hasattr(stream_type, name)
    ]
    assert not missing, f"upstream StreamType lost members the provider uses: {missing}"


def test_provider_feature_has_the_members_the_provider_declares(real_models):
    """The one contract failure here that would take the whole provider down.

    ``BASE_FEATURES`` and ``AUTHENTICATED_FEATURES`` resolve these at module
    scope, so a member dropped upstream is an AttributeError raised before the
    provider class is even defined: no degraded feature, no provider.

    ``hasattr`` on the name rather than a lookup by value, because
    ``ProviderFeature._missing_`` answers UNKNOWN for anything it does not
    recognise and a value-based check could never fail. See issue #65.
    """
    provider_feature = real_models.enums.ProviderFeature
    missing = [
        name
        for name in ma_contract.REQUIRED_PROVIDER_FEATURE_MEMBERS
        if not hasattr(provider_feature, name)
    ]
    assert not missing, (
        f"upstream ProviderFeature lost members the provider declares: {missing}; "
        "importing ytmusic_free would now raise AttributeError at module scope"
    )


# Deliberately not asserted here: that each of those members still carries the
# same ``value`` string. ``UNKNOWN_VALUE`` is pinned because "?" is surprising
# enough that a stub author writing from memory gets it wrong, which is what
# issue #41 was. ProviderFeature has no such trap; every member the provider
# names is mechanically ``name.lower()``. And the provider never reads a
# feature's value: it puts members in a set and hands the set to the
# constructor, so a re-valuing upstream cannot change what it does. Pinning them
# would report somebody else's refactor as our failure.


def test_item_mapping_exposes_the_fields_the_provider_sets(real_models):
    """``year`` in particular: the album-year feature depends on it existing.

    A track's album arrives as an id and a name only, so the provider looks the
    year up and assigns it here. If upstream drops the field that assignment
    would silently go nowhere. See issue #53.
    """
    fields = {f.name for f in dataclasses.fields(real_models.media_items.ItemMapping)}
    missing = set(ma_contract.REQUIRED_ITEM_MAPPING_FIELDS) - fields
    assert not missing, f"upstream ItemMapping lost fields the provider sets: {missing}"


def test_podcast_exposes_the_fields_the_provider_sets(real_models):
    fields = {f.name for f in dataclasses.fields(real_models.media_items.Podcast)}
    missing = set(ma_contract.REQUIRED_PODCAST_FIELDS) - fields
    assert not missing, f"upstream Podcast lost fields the provider sets: {missing}"


def test_podcast_episode_exposes_the_fields_the_provider_sets(real_models):
    fields = {f.name for f in dataclasses.fields(real_models.media_items.PodcastEpisode)}
    missing = set(ma_contract.REQUIRED_PODCAST_EPISODE_FIELDS) - fields
    assert not missing, f"upstream PodcastEpisode lost fields the provider sets: {missing}"


# Deliberately not asserted here: that ``position`` and ``podcast`` are
# *mandatory* upstream. The source on the models repo's main branch declares
# them without a default, but ``dataclasses.fields()`` on the released 1.1.186
# reports ``position.default is None``, so an assertion written from the source
# failed against the package users actually run. The provider sets both fields
# explicitly on every episode it builds and the unit suite pins that, which is
# the property worth protecting; how strict upstream chooses to be about it is
# upstream's business.


def test_podcast_episode_resume_state_is_still_nullable(real_models):
    """None means "provider does not know", and MA then uses its own resume point.

    The provider leaves these unset because YouTube's anonymous responses carry
    no reliable position. That is only correct while None keeps this meaning.
    """
    episode_fields = {f.name: f for f in dataclasses.fields(real_models.media_items.PodcastEpisode)}
    for name in ma_contract.NULLABLE_PODCAST_EPISODE_FIELDS:
        assert episode_fields[name].default is None, (
            f"PodcastEpisode.{name} no longer defaults to None; the provider "
            "would now be asserting a resume position it does not have"
        )


def test_stream_details_exposes_the_fields_the_provider_sets(real_models):
    fields = {f.name for f in dataclasses.fields(real_models.streamdetails.StreamDetails)}
    missing = set(ma_contract.REQUIRED_STREAM_DETAILS_FIELDS) - fields
    assert not missing, f"upstream StreamDetails lost fields the provider sets: {missing}"


def test_stream_details_still_cannot_express_delayed_availability(real_models):
    """Why ``get_stream_details`` blocks instead of handing over a timestamp.

    YouTube serves some tracks behind a pre-roll ad and the media url 403s
    until that window passes (issue #51). yt-dlp reports it as ``available_at``
    and sleeps; the provider has to sleep too, because there is no field on
    StreamDetails that would let Music Assistant schedule the wait instead.

    If upstream adds one, this test fails and the sleep in
    ``get_stream_details`` should move behind that field, so the provider call
    stops blocking.
    """
    fields = {f.name for f in dataclasses.fields(real_models.streamdetails.StreamDetails)}
    present = sorted(fields.intersection(ma_contract.FORBIDDEN_STREAM_DETAILS_FIELDS))
    assert not present, (
        f"upstream StreamDetails gained {present}; the pre-roll sleep in "
        "get_stream_details can now be handed to the server instead"
    )


def _image_pair(real_models):
    image_type = real_models.enums.ImageType
    image = real_models.media_items.MediaItemImage
    kwargs = {
        "path": "https://i.ytimg.com/vi/x/maxresdefault.jpg",
        "provider": "p",
        "remotely_accessible": True,
    }
    return image(type=image_type.LANDSCAPE, **kwargs), image(type=image_type.THUMB, **kwargs)


def test_image_equality_includes_the_type(real_models):
    """``_parse_thumbnails`` adds a THUMB with the same path as a LANDSCAPE (#90).

    If equality ever dropped the type, UniqueList would deduplicate that THUMB
    away and players would show the Music Assistant logo again.
    """
    landscape, thumb = _image_pair(real_models)
    assert landscape != thumb
    assert len(real_models.media_items.UniqueList([landscape, thumb])) == 2


def test_media_item_image_only_returns_a_thumb(real_models):
    """Why the THUMB fallback exists: player artwork ignores LANDSCAPE (#90)."""
    media_items = real_models.media_items
    landscape, thumb = _image_pair(real_models)
    track = media_items.Track(item_id="x", provider="p", name="Mix", provider_mappings=set())
    track.metadata.images = media_items.UniqueList([landscape])
    assert track.image is None
    track.metadata.images = media_items.UniqueList([landscape, thumb])
    assert track.image == thumb


def test_bit_rate_defaults_to_none_so_an_unset_value_is_distinguishable(real_models):
    """The provider only sets bit_rate when yt-dlp reported one.

    That is only meaningful if "not set" is representable. A numeric default
    would make every stream claim a bitrate it never measured.
    """
    assert real_models.media_items.AudioFormat().bit_rate is None


def test_event_type_has_the_members_the_provider_signals(real_models):
    event_type = real_models.enums.EventType
    missing = [
        name for name in ma_contract.REQUIRED_EVENT_TYPE_MEMBERS if not hasattr(event_type, name)
    ]
    assert not missing, f"upstream EventType lost members the provider signals: {missing}"


def test_provider_error_carries_the_fields_the_auth_notice_uses(real_models):
    """The auth notice (issue #92) is a ProviderError written as last_error."""
    provider_error = real_models.config_entries.ProviderError
    fields = {f.name for f in dataclasses.fields(provider_error)}
    missing = set(ma_contract.REQUIRED_PROVIDER_ERROR_FIELDS) - fields
    assert not missing, f"upstream ProviderError lost fields: {missing}"


def test_provider_error_keeps_a_custom_message_without_a_translation_key(real_models):
    """With no key set, serialising must leave the provider's own text alone.

    A translation key would swap the notice for Music Assistant's generic
    "Login failed" string, which says nothing about anonymous mode or the fix.
    """
    error = real_models.config_entries.ProviderError(
        error_code=ma_contract.LOGIN_FAILED_ERROR_CODE, message="custom notice"
    )
    assert error.translation_key is None
    assert error.to_dict()["message"] == "custom notice"


def test_login_failed_code_is_unchanged(real_models):
    """The server maps this code to the "Authentication required" status."""
    assert real_models.errors.LoginFailed.error_code == ma_contract.LOGIN_FAILED_ERROR_CODE


def test_setup_failed_code_is_unchanged(real_models):
    """The non-cookie startup notice uses it; the server maps it to ERROR."""
    assert real_models.errors.SetupFailedError.error_code == ma_contract.SETUP_FAILED_ERROR_CODE
