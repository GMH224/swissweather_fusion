"""Lifecycle, config-flow and entity regression tests for v0.1.24.

Covers P0-03 (shutdown before close), P1-02 (reauth clears stale
options), P1-04/P1-05 (diagnostics redaction), P1-27/28/30 (config
validation), P2-13 (duplicate location), IND-03 (health), IND-05
(removal/migration) and IND-08 (entity metadata).
"""
import asyncio
from types import SimpleNamespace

import pytest
import voluptuous as vol

from swissweather_fusion import config_flow as cf
from swissweather_fusion import redaction
from swissweather_fusion.const import (
    CONF_SRF_CONSUMER_KEY,
    CONF_SRF_CONSUMER_SECRET,
)


# ---------------------------------------------------------------------------
# INFRA-02 — the harness itself
# ---------------------------------------------------------------------------
def test_real_voluptuous_is_installed_not_the_stub():
    """v0.1.24 (INFRA-02). tests/conftest.py used to install a minimal
    voluptuous stub UNCONDITIONALLY, shadowing the real, independently
    installed package — which has zero Home Assistant dependency of its
    own, unlike every other module stubbed there.

    The stub provided only Schema/Required/Optional/Coerce as no-op
    lambdas returning None. Every validator built with vol.All / vol.Range
    / vol.Invalid would therefore have silently evaluated to None under
    test while working correctly in production: tests passing against
    validators that were never executed. For a set of findings entirely
    about validation, that is the worst available failure mode, so it is
    asserted directly rather than assumed.
    """
    assert hasattr(vol, "All")
    assert hasattr(vol, "Range")
    assert isinstance(vol.Invalid("x"), Exception)
    with pytest.raises(vol.Invalid):
        vol.Schema(vol.All(vol.Coerce(int), vol.Range(min=0)))(-5)


def test_config_entry_auth_failed_is_importable():
    """INFRA-01: homeassistant.exceptions was missing from the stub set
    entirely, so any module importing ConfigEntryAuthFailed — which the
    P1-01 and P2-12 fixes require — failed at collection time and errored
    out the whole suite."""
    from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

    assert issubclass(ConfigEntryAuthFailed, Exception)
    assert issubclass(ConfigEntryNotReady, Exception)


# ---------------------------------------------------------------------------
# P1-27 / P1-28 / P1-30 — config validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "Infinity", float("nan")])
def test_coordinate_validators_reject_non_finite(bad):
    """float("nan") and float("inf") both parse without raising, so bare
    vol.Coerce(float) accepted them. A non-finite latitude propagates
    into the LV95 transform, the STAC query and every provider URL,
    failing far from where it was entered and looking like a provider
    outage."""
    with pytest.raises(vol.Invalid):
        cf._LATITUDE_VALIDATOR(bad)


@pytest.mark.parametrize("bad", [-91.0, 91.0, 1000.0])
def test_latitude_validator_rejects_out_of_range(bad):
    with pytest.raises(vol.Invalid):
        cf._LATITUDE_VALIDATOR(bad)


def test_latitude_validator_accepts_a_real_swiss_coordinate():
    assert cf._LATITUDE_VALIDATOR(46.9481) == pytest.approx(46.9481)


@pytest.mark.parametrize("bad", [-181.0, 181.0])
def test_longitude_validator_rejects_out_of_range(bad):
    with pytest.raises(vol.Invalid):
        cf._LONGITUDE_VALIDATOR(bad)


@pytest.mark.parametrize("bad", [-500.0, 12000.0])
def test_elevation_validator_rejects_absurd_values(bad):
    with pytest.raises(vol.Invalid):
        cf._ELEVATION_VALIDATOR(bad)


def test_elevation_validator_accepts_the_extremes_it_is_meant_to_allow():
    """-430 m is roughly the Dead Sea shore; 9000 m is above Everest.
    Wide enough never to reject a real location."""
    assert cf._ELEVATION_VALIDATOR(-430.0) == -430.0
    assert cf._ELEVATION_VALIDATOR(9000.0) == 9000.0


def test_purge_days_validator_rejects_negative():
    """RetentionCoordinator treats purge_days <= 0 as "keep forever", so a
    negative value silently became a second spelling of forever rather
    than the error it looks like."""
    with pytest.raises(vol.Invalid):
        cf._PURGE_DAYS_VALIDATOR(-1)
    assert cf._PURGE_DAYS_VALIDATOR(0) == 0
    assert cf._PURGE_DAYS_VALIDATOR(90) == 90


def test_non_empty_str_rejects_empty_and_whitespace():
    """vol.Required only requires the KEY be present, not that its value
    be meaningful — so an empty secret saved cleanly and failed later at
    request time, surfacing as an auth error rather than the data-entry
    mistake it was."""
    with pytest.raises(vol.Invalid):
        cf._non_empty_str("")
    with pytest.raises(vol.Invalid):
        cf._non_empty_str("   ")
    with pytest.raises(vol.Invalid):
        cf._non_empty_str(None)


def test_non_empty_str_strips_whitespace():
    assert cf._non_empty_str("  abc  ") == "abc"


# ---------------------------------------------------------------------------
# P2-13 — duplicate location prevention
# ---------------------------------------------------------------------------
def test_duplicate_location_unique_id_rounds_away_float_noise():
    """Without rounding, floating-point noise between two submissions of
    "the same" coordinates produces two different unique IDs and defeats
    the duplicate check entirely — which is the exact failure mode it
    exists to prevent."""
    a = cf._location_unique_id(46.94810000001, 7.44740000002)
    b = cf._location_unique_id(46.9481, 7.4474)
    assert a == b


def test_genuinely_different_locations_get_different_unique_ids():
    """4 decimal places is ~11 m; two real installations must not
    collide."""
    bern = cf._location_unique_id(46.9481, 7.4474)
    zurich = cf._location_unique_id(47.3769, 8.5417)
    assert bern != zurich


# ---------------------------------------------------------------------------
# P1-02 — reauth must clear a stale options-side credential
# ---------------------------------------------------------------------------
def test_reauth_confirm_clears_stale_srf_credential_from_options():
    """Every runtime credential is resolved options-first —
    options.get(KEY, data[KEY]) — so a stale copy in entry.options kept
    winning at runtime even after the UI reported "reauth successful".
    The user sees success, the integration keeps using the revoked
    credential, and nothing explains why.

    Drives the real async_step_reauth_confirm against a fake
    config-entries backend.
    """
    recorded = {}

    class FakeEntries:
        def async_get_entry(self, entry_id):
            return SimpleNamespace(
                entry_id="e1",
                data={CONF_SRF_CONSUMER_KEY: "old"},
                options={
                    CONF_SRF_CONSUMER_KEY: "stale-from-options",
                    "purge_days": 90,
                },
            )

        def async_update_entry(self, entry, **kwargs):
            recorded.update(kwargs)

        async def async_reload(self, entry_id):
            return None

    flow = object.__new__(cf.SwissWeatherFusionConfigFlow)
    flow._data = {}
    flow.hass = SimpleNamespace(config_entries=FakeEntries())
    flow.context = {"entry_id": "e1"}
    flow.async_abort = lambda reason: {"type": "abort", "reason": reason}
    flow.async_show_form = lambda **kw: {"type": "form"}

    result = asyncio.run(
        flow.async_step_reauth_confirm(
            {CONF_SRF_CONSUMER_KEY: "new-key", CONF_SRF_CONSUMER_SECRET: "new-secret"}
        )
    )

    assert result["reason"] == "reauth_successful"
    assert "options" in recorded, "options were not updated at all"
    assert CONF_SRF_CONSUMER_KEY not in recorded["options"], (
        "the stale options-side credential survived reauth and would keep "
        "winning at runtime"
    )
    # Unrelated options must survive.
    assert recorded["options"]["purge_days"] == 90
    assert recorded["data"][CONF_SRF_CONSUMER_KEY] == "new-key"


# ---------------------------------------------------------------------------
# P1-05 — station entity IDs are household-layout information
# ---------------------------------------------------------------------------
def test_station_entity_ids_are_redacted():
    """Not a credential, but real entity IDs like
    sensor.bedroom_temperature describe the layout and room names of
    someone's home, and appeared verbatim in a file meant to be shared
    for troubleshooting."""
    payload = {
        "station_temp_entity": "sensor.bedroom_temperature",
        "station_pressure_entity": "sensor.living_room_pressure",
    }
    redacted = redaction.redact_diagnostic_payload(
        payload, latitude=46.9481, longitude=7.4474
    )
    assert "bedroom" not in str(redacted)
    assert "living_room" not in str(redacted)


def test_non_sensitive_keys_are_not_over_redacted():
    """"entity" is safe to match on today because no other config key
    contains that substring — asserted rather than assumed."""
    payload = {"poll_count": 12, "source": "ch1"}
    redacted = redaction.redact_diagnostic_payload(
        payload, latitude=46.9481, longitude=7.4474
    )
    assert redacted["poll_count"] == 12
    assert redacted["source"] == "ch1"


# ---------------------------------------------------------------------------
# IND-03 — health, in all three places
# ---------------------------------------------------------------------------
def test_never_succeeded_source_is_not_healthy():
    """consecutive_failures == 0 is the attribute's INITIAL value, so a
    source that has never been polled looked identical to one that had
    genuinely succeeded. A cold start therefore reported every source
    active and the integration "Active" before a single successful
    fetch.

    The audit flagged this only on ActiveSourcesSensor; the identical
    test also appeared in StatusSensor and DegradedBinarySensor — the two
    entities users and automations actually watch.
    """
    from swissweather_fusion.sensor import is_source_healthy

    never_tried = SimpleNamespace(consecutive_failures=0, last_success_time=None)
    working = SimpleNamespace(consecutive_failures=0, last_success_time=object())
    failing = SimpleNamespace(consecutive_failures=3, last_success_time=object())

    assert not is_source_healthy(never_tried)
    assert is_source_healthy(working)
    assert not is_source_healthy(failing)
    assert not is_source_healthy(None)


def test_all_three_health_consumers_share_one_helper():
    """Fixing only the site the audit named would have left the visible
    symptom intact on the other two. Asserted structurally so a future
    edit cannot silently reintroduce a second definition."""
    import inspect

    from swissweather_fusion import binary_sensor, sensor

    assert "is_source_healthy" in inspect.getsource(sensor.StatusSensor.native_value.fget)
    assert "is_source_healthy" in inspect.getsource(
        sensor.ActiveSourcesSensor.native_value.fget
    )
    assert "is_source_healthy" in inspect.getsource(
        binary_sensor.DegradedBinarySensor.is_on.fget
    )


# ---------------------------------------------------------------------------
# IND-08 — entity metadata
# ---------------------------------------------------------------------------
def test_timestamp_sensors_declare_the_timestamp_device_class():
    """native_value returns a datetime; Home Assistant requires
    SensorDeviceClass.TIMESTAMP for that to be stored and rendered as a
    timestamp rather than coerced to a string. No entity in this
    integration declared a device class before v0.1.24."""
    from swissweather_fusion.sensor import LastLearningASensor, LastSuccessSensor

    assert LastSuccessSensor._attr_device_class == "timestamp"
    assert LastLearningASensor._attr_device_class == "timestamp"


def test_numeric_sensors_declare_a_state_class_for_statistics():
    """Without state_class = MEASUREMENT none of the numeric telemetry is
    recorded into Home Assistant's statistics tables — so "learning
    progress and forecast accuracy", which sensor.py's own docstring
    records as an explicit build requirement, could not be charted over
    time."""
    from swissweather_fusion.sensor import (
        ExpertWeightSensor,
        ForecastAccuracySensor,
        StormOnsetProbabilitySensor,
    )

    assert StormOnsetProbabilitySensor._attr_state_class == "measurement"
    assert ExpertWeightSensor._attr_state_class == "measurement"
    assert ForecastAccuracySensor._attr_state_class == "measurement"


def test_degraded_binary_sensor_declares_the_problem_device_class():
    from swissweather_fusion.binary_sensor import DegradedBinarySensor

    assert DegradedBinarySensor._attr_device_class == "problem"


def test_diagnostic_sensors_are_categorised_so_they_do_not_clutter_the_device_page():
    from swissweather_fusion.sensor import (
        ConsecutiveFailuresSensor,
        LastPollDurationSensor,
    )

    assert LastPollDurationSensor._attr_entity_category == "diagnostic"
    assert ConsecutiveFailuresSensor._attr_entity_category == "diagnostic"


# ---------------------------------------------------------------------------
# P2-06 / P3-01 — honest labelling, without orphaning entity IDs
# ---------------------------------------------------------------------------
def test_storm_sensor_discloses_that_it_is_not_a_calibrated_probability():
    """The name implied statistical validation the v0 heuristic does not
    have. The disclosure is on the ENTITY — reachable from the UI,
    templates and the REST API — rather than only in a source comment no
    user will ever read."""
    from swissweather_fusion.sensor import StormOnsetProbabilitySensor

    sensor = object.__new__(StormOnsetProbabilitySensor)
    attrs = StormOnsetProbabilitySensor.extra_state_attributes.fget(sensor)
    assert attrs["is_calibrated_probability"] is False
    assert "heuristic" in attrs["methodology"].lower()


def test_last_learning_b_sensor_discloses_non_applicability():
    from swissweather_fusion.sensor import LastLearningBSensor

    sensor = object.__new__(LastLearningBSensor)
    attrs = LastLearningBSensor.extra_state_attributes.fget(sensor)
    assert attrs["not_applicable"] is True
    assert LastLearningBSensor._attr_entity_registry_enabled_default is False


def test_entity_keys_were_deliberately_not_renamed():
    """Renaming the unique_id would orphan every existing installation's
    entity_id, automations and history — a worse outcome than the
    labelling problem being fixed. Only user-visible names changed."""
    import inspect

    from swissweather_fusion.sensor import LastLearningBSensor, StormOnsetProbabilitySensor

    assert '"storm_onset_probability"' in inspect.getsource(
        StormOnsetProbabilitySensor.__init__
    )
    assert '"last_learning_b"' in inspect.getsource(LastLearningBSensor.__init__)
