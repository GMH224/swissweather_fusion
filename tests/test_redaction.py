from swissweather_fusion import redaction

# Generic placeholder coordinates for all tests here — never real ones.
TEST_LAT, TEST_LON = 46.9480, 7.4474


def test_redact_sensitive_keys_credentials():
    data = {"srf_consumer_key": "abc123", "srf_consumer_secret": "xyz789", "ok": "fine"}
    result = redaction.redact_sensitive_keys(data)
    assert result["srf_consumer_key"] == "[REDACTED]"
    assert result["srf_consumer_secret"] == "[REDACTED]"
    assert result["ok"] == "fine"


def test_redact_sensitive_keys_coordinates_and_elevation():
    data = {"latitude": 46.9480, "longitude": 7.4474, "elevation_effective": 480}
    result = redaction.redact_sensitive_keys(data)
    assert result["latitude"] == "[REDACTED]"
    assert result["longitude"] == "[REDACTED]"
    assert result["elevation_effective"] == "[REDACTED]"


def test_redact_sensitive_keys_nested_and_lists():
    data = {
        "outer": {
            "geolocation": {"lat": 46.9480, "lon": 7.4474},
            "items": [{"name": "somewhere"}, {"name": "elsewhere"}],
        }
    }
    result = redaction.redact_sensitive_keys(data)
    assert result["outer"]["geolocation"] == "[REDACTED]"  # "geolocation" itself matches
    assert result["outer"]["items"][0]["name"] == "[REDACTED]"
    assert result["outer"]["items"][1]["name"] == "[REDACTED]"


def test_redact_sensitive_keys_real_srf_response_structure():
    """The actual motivating case: a real captured SRF response embedding
    identifying location data in its own content, not just in this
    project's config. Confirms every one of the fields that prompted this
    feature gets caught. Uses the real *field names/shape* SRF's response
    actually has, but with generic placeholder place names — not the real
    location that was originally captured.
    """
    real_shaped_response = {
        "geolocation": {
            "id": "46.9480,7.4474",
            "lat": 46.9480,
            "lon": 7.4474,
            "station_id": "S00000",
            "timezone": "Europe/Zurich",
            "default_name": "Example",
            "alarm_region_id": "00",
            "alarm_region_name": "ExampleTown",
            "district": "ExampleDistrict",
            "geolocation_names": [
                {
                    "id": "0000000000000000000000000000000",
                    "location_id": "000000000",
                    "type": "city",
                    "name": "ExampleVillage",
                    "country": "Schweiz",
                    "province": "ExampleCanton",
                    "height": 500,
                    "description_short": "ExampleTown, ExampleCanton",
                    "description_long": "ExampleTown, ExampleCanton, 500 m ü.M.",
                    "district": "ExampleDistrict",
                }
            ],
        },
        "forecast": {"day": [{"TX_C": 34, "TN_C": 15}]},
    }
    result = redaction.redact_sensitive_keys(real_shaped_response)

    # The entire "geolocation" key is redacted wholesale (its own key name
    # matches), which also takes every nested identifying field with it.
    assert result["geolocation"] == "[REDACTED]"
    # The actual forecast data — the part we need for debugging — survives
    # untouched.
    assert result["forecast"]["day"][0]["TX_C"] == 34


def test_redact_coordinate_strings_catches_combined_id_format():
    """SRF's geolocationId is literally "lat,lon" as a plain string under
    an innocuous key name ("id") that key-based redaction alone wouldn't
    flag — this is exactly why the text-level pass exists.
    """
    text = f'{{"id": "{TEST_LAT},{TEST_LON}", "note": "unrelated"}}'
    redacted = redaction.redact_coordinate_strings(text, latitude=TEST_LAT, longitude=TEST_LON)
    assert str(TEST_LAT) not in redacted
    assert str(TEST_LON) not in redacted
    assert "[LAT_REDACTED]" in redacted
    assert "[LON_REDACTED]" in redacted
    assert "unrelated" in redacted  # untouched, unrelated content survives


def test_redact_coordinate_strings_various_decimal_precisions():
    """v0.1.19 regression test: before this fix, only str(value)/.4f/.2f
    were matched, so coordinates embedded at other precisions (3, 5, 6
    decimals — all plausible from a third-party API/JSON serializer)
    slipped through untouched.
    """
    for decimals in (2, 3, 5, 6, 8):
        text = f'{{"loc": "{TEST_LAT:.{decimals}f},{TEST_LON:.{decimals}f}"}}'
        redacted = redaction.redact_coordinate_strings(
            text, latitude=TEST_LAT, longitude=TEST_LON
        )
        assert f"{TEST_LAT:.{decimals}f}" not in redacted, f"leaked at {decimals} decimals"
        assert f"{TEST_LON:.{decimals}f}" not in redacted, f"leaked at {decimals} decimals"
        assert "[LAT_REDACTED]" in redacted
        assert "[LON_REDACTED]" in redacted


def test_redact_coordinate_strings_bracketed_format():
    """A bracketed [lat, lon] pair, as might appear in a GeoJSON-style
    payload or an error message — a format the original 3-variant list
    wouldn't have matched at all precisions.
    """
    text = f"location=[{TEST_LAT:.5f}, {TEST_LON:.5f}]"
    redacted = redaction.redact_coordinate_strings(
        text, latitude=TEST_LAT, longitude=TEST_LON
    )
    assert f"{TEST_LAT:.5f}" not in redacted
    assert f"{TEST_LON:.5f}" not in redacted
    assert "[LAT_REDACTED]" in redacted
    assert "[LON_REDACTED]" in redacted


def test_redact_coordinate_strings_does_not_clobber_unrelated_longer_number():
    """Guards against the substitution being too eager: a longer, unrelated
    number that merely contains the configured coordinate as a substring
    (e.g. a station ID or an elevation-adjacent figure) must survive
    intact — only an actual coordinate-boundary match should be redacted.
    """
    text = f"station_reading=146.9480 unrelated_id=7.44740001"
    redacted = redaction.redact_coordinate_strings(
        text, latitude=TEST_LAT, longitude=TEST_LON
    )
    # Neither of these is genuinely the configured coordinate — the first
    # is a different number that happens to end the same way, the second
    # has extra trailing digits — so both must be left alone.
    assert "146.9480" in redacted
    assert "7.44740001" in redacted


def test_redact_coordinate_strings_prefers_longest_match():
    """The most precise/longest representation must be matched before a
    shorter one that's a strict prefix of it, so redaction doesn't leave
    a mangled remainder like a stray ".9480" behind.
    """
    text = f"id={TEST_LAT:.4f},{TEST_LON:.4f}"
    redacted = redaction.redact_coordinate_strings(
        text, latitude=TEST_LAT, longitude=TEST_LON
    )
    assert redacted == "id=[LAT_REDACTED],[LON_REDACTED]"


def test_redact_diagnostic_payload_combined_pass_on_real_structure():
    """The actual function used at every diagnostic recording call site —
    confirms the combined key + coordinate pass fully cleans the real SRF
    response shape end to end, leaving only the genuinely useful forecast
    content behind.
    """
    real_shaped_response = {
        "geolocation": {
            "id": f"{TEST_LAT},{TEST_LON}",
            "lat": TEST_LAT,
            "lon": TEST_LON,
            "default_name": "SomeVillage",
            "district": "SomeDistrict",
        },
        "forecast": {"day": [{"TX_C": 30, "RRR_MM": 1.5}]},
    }
    result = redaction.redact_diagnostic_payload(
        real_shaped_response, latitude=TEST_LAT, longitude=TEST_LON
    )
    serialized = str(result)
    assert str(TEST_LAT) not in serialized
    assert str(TEST_LON) not in serialized
    assert "SomeVillage" not in serialized
    assert "SomeDistrict" not in serialized
    # The actual diagnostic value survives.
    assert result["forecast"]["day"][0]["TX_C"] == 30
    assert result["forecast"]["day"][0]["RRR_MM"] == 1.5


def test_redact_diagnostic_payload_handles_non_dict_input():
    """Should never crash regardless of what shape it's handed — a
    diagnostic recording call site shouldn't itself become a new source
    of crashes."""
    assert redaction.redact_diagnostic_payload("just a string", latitude=TEST_LAT, longitude=TEST_LON) is not None
    assert redaction.redact_diagnostic_payload(None, latitude=TEST_LAT, longitude=TEST_LON) is None
    assert redaction.redact_diagnostic_payload([1, 2, 3], latitude=TEST_LAT, longitude=TEST_LON) == [1, 2, 3]
