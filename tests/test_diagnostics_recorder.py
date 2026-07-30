from swissweather_fusion.diagnostics_recorder import DiagnosticsRecorder


def test_disabled_by_default_records_nothing():
    recorder = DiagnosticsRecorder()
    assert recorder.enabled is False
    recorder.record(source="srf", event_type="poll_success", detail="ok")
    assert recorder.get_events() == []


def test_enabling_allows_recording():
    recorder = DiagnosticsRecorder()
    recorder.set_enabled(True)
    recorder.record(source="srf", event_type="poll_success", detail="ok", extra={"points": 5})
    events = recorder.get_events()
    assert len(events) == 1
    assert events[0]["source"] == "srf"
    assert events[0]["event_type"] == "poll_success"
    assert events[0]["extra"] == {"points": 5}
    assert "ts" in events[0]


def test_disabling_stops_recording_but_keeps_existing_events():
    recorder = DiagnosticsRecorder()
    recorder.set_enabled(True)
    recorder.record(source="srf", event_type="poll_success", detail="first")
    recorder.set_enabled(False)
    recorder.record(source="srf", event_type="poll_success", detail="second")
    events = recorder.get_events()
    assert len(events) == 1
    assert events[0]["detail"] == "first"


def test_bounded_buffer_drops_oldest():
    recorder = DiagnosticsRecorder(max_events=3)
    recorder.set_enabled(True)
    for i in range(5):
        recorder.record(source="srf", event_type="poll_success", detail=f"event-{i}")
    events = recorder.get_events()
    assert len(events) == 3
    # Oldest (event-0, event-1) dropped, newest 3 retained in order.
    assert [e["detail"] for e in events] == ["event-2", "event-3", "event-4"]


def test_clear_empties_the_buffer():
    recorder = DiagnosticsRecorder()
    recorder.set_enabled(True)
    recorder.record(source="srf", event_type="poll_success", detail="x")
    recorder.clear()
    assert recorder.get_events() == []
