"""Tests for diagnostics.py's redaction helpers.

diagnostics.py imports Home Assistant directly, but conftest.py's stub
setup already makes the specific symbols it needs (ConfigEntry,
HomeAssistant) importable without a real HA install — the same
infrastructure the rest of this test suite relies on.
"""
from swissweather_fusion.diagnostics import _health_summary, _redact_event, _redact_text

# Generic placeholder coordinates — never the real ones.
TEST_LAT, TEST_LON = 46.9480, 7.4474
TEST_SECRET = "sk_test_totallyFakeApiKeyValue123"


class _FakeHealth:
    """Minimal stand-in for storage's SourceHealth, just the fields
    _health_summary reads."""

    def __init__(self, *, last_data_error=None, last_auth_error=None):
        self.last_success_time = None
        self.last_poll_duration_ms = None
        self.last_data_error = last_data_error
        self.last_auth_error = last_auth_error
        self.consecutive_failures = 0


def test_redact_text_catches_real_url_leak():
    """Regression test for a real bug found in a downloaded diagnostics
    file: an Open-Meteo 503 error's message was the full request URL,
    which embeds latitude/longitude as query parameters — the first
    version of this module didn't redact error message strings at all,
    assuming they "needed no redacting" since they're not structured
    config. This is the exact real error message shape that leaked,
    with generic placeholder coordinates substituted for the real ones.
    """
    real_shaped_error = (
        f"503, message='Service Unavailable', "
        f"url='https://api.open-meteo.com/v1/forecast?latitude={TEST_LAT}"
        f"&longitude={TEST_LON}&hourly=temperature_2m&models=meteoswiss_icon_ch2'"
    )
    redacted = _redact_text(
        real_shaped_error, latitude=TEST_LAT, longitude=TEST_LON, secrets=[]
    )
    assert str(TEST_LAT) not in redacted
    assert str(TEST_LON) not in redacted
    assert "[LAT_REDACTED]" in redacted
    assert "[LON_REDACTED]" in redacted
    # The genuinely useful diagnostic content (status code, message,
    # which model failed) survives.
    assert "503" in redacted
    assert "Service Unavailable" in redacted
    assert "meteoswiss_icon_ch2" in redacted


def test_redact_text_catches_real_api_key_leak():
    """v0.1.20 regression test: Open-Meteo's client embeds its API key
    directly in the request URL (`url += f"&apikey={api_key}"` in
    clients/open_meteo.py) — a real credential-leak path, not just a
    location one, that the pre-v0.1.20 coordinate-only redaction here
    completely missed.
    """
    real_shaped_error = (
        f"403, message='Forbidden', "
        f"url='https://customer-api.open-meteo.com/v1/forecast?"
        f"latitude={TEST_LAT}&longitude={TEST_LON}&apikey={TEST_SECRET}'"
    )
    redacted = _redact_text(
        real_shaped_error, latitude=TEST_LAT, longitude=TEST_LON, secrets=[TEST_SECRET]
    )
    assert TEST_SECRET not in redacted
    assert "[SECRET_REDACTED]" in redacted
    assert "[LAT_REDACTED]" in redacted
    assert "[LON_REDACTED]" in redacted
    assert "403" in redacted


def test_redact_text_handles_falsy_secrets_without_crashing():
    """None/empty-string secrets (an unconfigured optional API key, e.g.
    Open-Meteo's is optional) must be skipped, not treated as a literal
    empty string to "redact" (which would corrupt every string).
    """
    text = "ordinary text with no secrets in it"
    redacted = _redact_text(text, latitude=TEST_LAT, longitude=TEST_LON, secrets=[None, "", TEST_SECRET])
    assert redacted == text


def test_health_summary_redacts_both_error_fields():
    health = _FakeHealth(
        last_data_error=f"error at lat={TEST_LAT} lon={TEST_LON} key={TEST_SECRET}",
        last_auth_error=f"auth failed for {TEST_LAT},{TEST_LON}",
    )
    summary = _health_summary(
        health, latitude=TEST_LAT, longitude=TEST_LON, secrets=[TEST_SECRET]
    )
    assert str(TEST_LAT) not in summary["last_data_error"]
    assert str(TEST_LAT) not in summary["last_auth_error"]
    assert str(TEST_LON) not in summary["last_data_error"]
    assert str(TEST_LON) not in summary["last_auth_error"]
    assert TEST_SECRET not in summary["last_data_error"]


def test_health_summary_handles_none_health():
    assert _health_summary(None, latitude=TEST_LAT, longitude=TEST_LON, secrets=[]) == {}


def test_redact_event_catches_leak_in_detail_field():
    """v0.1.20 regression test for the actual bug: diagnostics_events
    were never redacted at all before this fix, despite diagnostics.py's
    own docstring/note always having claimed otherwise.
    async_get_config_entry_diagnostics used to do
    `recorder.get_events() if recorder is not None else []` — passed
    straight through. Any `poll_failure` event (built from
    `str(exception)` at call sites scattered across every coordinator,
    not just SRF's) could carry a full request URL — coordinates, or for
    Open-Meteo specifically a real API key — completely unredacted into
    a file meant to be safe to share. Reproduces the exact SRF 400 error
    shape that surfaced this during a live debugging session.
    """
    event = {
        "ts": "2026-08-01T15:23:49.270000+00:00",
        "source": "srf",
        "event_type": "forecastpoint_fallback",
        "detail": (
            f"primary forecastpoint fetch failed: 400, message='Bad Request', "
            f"url='https://api.srgssr.ch/srf-meteo/v2/forecastpoint/{TEST_LAT},{TEST_LON}'"
        ),
        "extra": {},
    }
    redacted = _redact_event(event, latitude=TEST_LAT, longitude=TEST_LON, secrets=[TEST_SECRET])
    assert str(TEST_LAT) not in redacted["detail"]
    assert str(TEST_LON) not in redacted["detail"]
    assert "[LAT_REDACTED]" in redacted["detail"]
    assert "[LON_REDACTED]" in redacted["detail"]
    # Non-sensitive fields pass through unchanged.
    assert redacted["ts"] == event["ts"]
    assert redacted["source"] == "srf"
    assert redacted["event_type"] == "forecastpoint_fallback"


def test_redact_event_catches_leak_in_extra_string_values():
    event = {
        "ts": "2026-08-01T15:00:00+00:00",
        "source": "open_meteo",
        "event_type": "poll_failure",
        "detail": "fetch failed",
        "extra": {
            "raw_url": f"https://api.open-meteo.com/v1/forecast?apikey={TEST_SECRET}",
            "point_count": 0,
        },
    }
    redacted = _redact_event(event, latitude=TEST_LAT, longitude=TEST_LON, secrets=[TEST_SECRET])
    assert TEST_SECRET not in redacted["extra"]["raw_url"]
    assert "[SECRET_REDACTED]" in redacted["extra"]["raw_url"]
    # Non-string extra values pass through unchanged.
    assert redacted["extra"]["point_count"] == 0


def test_redact_event_handles_missing_fields_gracefully():
    """A minimal/malformed event dict shouldn't crash redaction —
    defensive, since this runs over whatever's actually in the buffer."""
    redacted = _redact_event({}, latitude=TEST_LAT, longitude=TEST_LON, secrets=[])
    assert redacted["detail"] == ""
    assert redacted["extra"] == {}


class _FakeCoordinator:
    def __init__(self, *, last_update_success=True, update_interval_minutes=5):
        from datetime import timedelta

        self.last_update_success = last_update_success
        self.update_interval = timedelta(minutes=update_interval_minutes)


def test_scheduling_summary_none_coordinator():
    from swissweather_fusion.diagnostics import _coordinator_scheduling_summary

    assert _coordinator_scheduling_summary(None) == {"present": False}


def test_scheduling_summary_not_overdue_when_recently_succeeded():
    from datetime import datetime, timedelta, timezone

    from swissweather_fusion.diagnostics import _coordinator_scheduling_summary

    coordinator = _FakeCoordinator(update_interval_minutes=5)
    recent = datetime.now(timezone.utc) - timedelta(minutes=1)
    summary = _coordinator_scheduling_summary(coordinator, last_success_time=recent)
    assert summary["present"] is True
    assert summary["overdue"] is False


def test_scheduling_summary_overdue_regression_case():
    """Regression test for the actual reported scenario: a coordinator
    whose last success is far older than its own configured interval
    would ever allow if running normally — a 5-minute-interval
    coordinator with a 2-hour-old last success should be flagged,
    unambiguously, in the diagnostics output itself rather than needing
    manual elapsed-time arithmetic every time this comes up.
    """
    from datetime import datetime, timedelta, timezone

    from swissweather_fusion.diagnostics import _coordinator_scheduling_summary

    coordinator = _FakeCoordinator(update_interval_minutes=5)
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    summary = _coordinator_scheduling_summary(coordinator, last_success_time=two_hours_ago)
    assert summary["overdue"] is True
    assert summary["seconds_since_last_success"] > 3600


def test_scheduling_summary_no_success_yet_does_not_crash():
    from swissweather_fusion.diagnostics import _coordinator_scheduling_summary

    coordinator = _FakeCoordinator()
    summary = _coordinator_scheduling_summary(coordinator, last_success_time=None)
    assert summary["present"] is True
    assert summary["overdue"] is None  # can't judge overdue-ness with nothing to compare
