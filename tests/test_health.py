from swissweather_fusion.health import SourceHealth, classify_exception


class _FakeAuthError(Exception):
    status = 401


class _FakeDataError(Exception):
    status = 500


class _FakePlainError(Exception):
    pass


def test_classify_exception():
    assert classify_exception(_FakeAuthError()) == "auth"
    assert classify_exception(_FakeDataError()) == "data"
    assert classify_exception(_FakePlainError()) == "data"  # no status -> safe default


def test_source_health_records_data_error():
    health = SourceHealth()
    kind = health.record_error(_FakeDataError("timeout"), duration_ms=1200.0)
    assert kind == "data"
    assert health.consecutive_failures == 1
    assert health.last_data_error == "timeout"
    assert health.last_auth_error is None


def test_source_health_records_auth_error_distinctly():
    health = SourceHealth()
    health.record_error(_FakeDataError("timeout"))
    kind = health.record_error(_FakeAuthError("expired key"))
    assert kind == "auth"
    assert health.consecutive_failures == 2  # both count toward the same streak
    assert health.last_auth_error == "expired key"
    assert health.last_data_error == "timeout"  # not overwritten by the auth error


def test_source_health_success_resets_streak_but_preserves_history():
    health = SourceHealth()
    health.record_error(_FakeDataError("timeout"))
    health.record_error(_FakeAuthError("expired key"))
    health.record_success(duration_ms=300.0)

    assert health.consecutive_failures == 0
    assert health.last_success_time is not None
    assert health.last_poll_duration_ms == 300.0
    # History is preserved, not cleared, so the UI can still show "last
    # time this happened" even after recovery.
    assert health.last_data_error == "timeout"
    assert health.last_auth_error == "expired key"
