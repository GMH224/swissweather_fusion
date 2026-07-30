"""Tests for diagnostics.py's redaction helpers.

diagnostics.py imports Home Assistant directly, but conftest.py's stub
setup already makes the specific symbols it needs (ConfigEntry,
HomeAssistant) importable without a real HA install — the same
infrastructure the rest of this test suite relies on.
"""
from swissweather_fusion.diagnostics import _health_summary, _redact_error_string

# Generic placeholder coordinates — never the real ones.
TEST_LAT, TEST_LON = 46.9480, 7.4474


class _FakeHealth:
    """Minimal stand-in for storage's SourceHealth, just the fields
    _health_summary reads."""

    def __init__(self, *, last_data_error=None, last_auth_error=None):
        self.last_success_time = None
        self.last_poll_duration_ms = None
        self.last_data_error = last_data_error
        self.last_auth_error = last_auth_error
        self.consecutive_failures = 0


def test_redact_error_string_catches_real_url_leak():
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
    redacted = _redact_error_string(real_shaped_error, latitude=TEST_LAT, longitude=TEST_LON)
    assert str(TEST_LAT) not in redacted
    assert str(TEST_LON) not in redacted
    assert "[LAT_REDACTED]" in redacted
    assert "[LON_REDACTED]" in redacted
    # The genuinely useful diagnostic content (status code, message,
    # which model failed) survives.
    assert "503" in redacted
    assert "Service Unavailable" in redacted
    assert "meteoswiss_icon_ch2" in redacted


def test_redact_error_string_handles_none():
    assert _redact_error_string(None, latitude=TEST_LAT, longitude=TEST_LON) is None


def test_health_summary_redacts_both_error_fields():
    health = _FakeHealth(
        last_data_error=f"error at lat={TEST_LAT} lon={TEST_LON}",
        last_auth_error=f"auth failed for {TEST_LAT},{TEST_LON}",
    )
    summary = _health_summary(health, latitude=TEST_LAT, longitude=TEST_LON)
    assert str(TEST_LAT) not in summary["last_data_error"]
    assert str(TEST_LAT) not in summary["last_auth_error"]
    assert str(TEST_LON) not in summary["last_data_error"]
    assert str(TEST_LON) not in summary["last_auth_error"]


def test_health_summary_handles_none_health():
    assert _health_summary(None, latitude=TEST_LAT, longitude=TEST_LON) == {}


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
