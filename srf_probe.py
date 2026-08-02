#!/usr/bin/env python3
"""Standalone SRF API probe — no Home Assistant, no third-party
packages, standard library only (urllib). Reproduces the exact same
auth -> geolocation -> forecastpoint/forecast flow as
clients/srf.py in the SwissWeather Fusion integration, and prints the
RAW response at every step so we can see exactly what SRF is actually
returning instead of guessing.

Run this from a machine that has network access to api.srgssr.ch —
this sandbox that built it does not, only your own network (or HA's
own host, e.g. via the SSH/terminal add-on) does.

Usage:
    python3 srf_probe.py --consumer-key KEY --consumer-secret SECRET \\
        --latitude 47.5536 --longitude 8.9120

Credentials are only ever used for the HTTP requests below — never
printed, never logged. Everything else IS printed, including the
geolocation search's full raw response, since that's the whole point.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://api.srgssr.ch/oauth/v1/accesstoken?grant_type=client_credentials"
GEOLOCATION_URL = "https://api.srgssr.ch/srf-meteo/geolocations"
FORECAST_URL_TEMPLATE = "https://api.srgssr.ch/srf-meteo/forecast/{geolocation_id}"
FORECASTPOINT_URL_TEMPLATE = "https://api.srgssr.ch/srf-meteo/v2/forecastpoint/{geolocation_id}"


def _print_header(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _request(url: str, *, method: str = "GET", headers: dict[str, str] | None = None):
    """Returns (status_code, body_text_or_None, error_or_None). Never
    raises — every failure mode is reported, not propagated, since the
    whole point of this script is to see what actually happens.
    """
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body, None
    except urllib.error.URLError as e:
        return None, None, str(e.reason)
    except Exception as e:  # noqa: BLE001
        return None, None, str(e)


def get_token(consumer_key: str, consumer_secret: str) -> str | None:
    _print_header("STEP 1: OAuth token")
    raw = f"{consumer_key}:{consumer_secret}".encode("utf-8")
    auth_header = "Basic " + base64.b64encode(raw).decode("ascii")
    status, body, err = _request(
        TOKEN_URL, method="POST", headers={"Authorization": auth_header}
    )
    if err:
        print(f"FAILED (network/transport error): {err}")
        return None
    print(f"HTTP {status}")
    if status != 200:
        print(f"Body: {body}")
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(f"Response wasn't valid JSON. Raw body: {body}")
        return None
    token = payload.get("access_token")
    if not token:
        print(f"No access_token in response. Full payload: {json.dumps(payload, indent=2)}")
        return None
    print(f"Got token (length {len(token)}, not printed in full: {token[:8]}...)")
    return token


def get_geolocation(token: str, latitude: float, longitude: float):
    _print_header("STEP 2: Geolocation search (by coordinates)")
    params = urllib.parse.urlencode({"latitude": latitude, "longitude": longitude})
    url = f"{GEOLOCATION_URL}?{params}"
    status, body, err = _request(url, headers={"Authorization": f"Bearer {token}"})
    if err:
        print(f"FAILED (network/transport error): {err}")
        return None
    print(f"HTTP {status}")
    if status != 200:
        print(f"Body: {body}")
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(f"Response wasn't valid JSON. Raw body: {body}")
        return None
    print("FULL raw response (this is the important part):")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    # Mirror the actual candidate extraction, but show ALL candidates,
    # not just the first — this is the "closest vs first result" question
    # from the integration's own code.
    if isinstance(payload, list):
        results = payload
    elif isinstance(payload, dict):
        results = payload.get("geolocations") or payload.get("results") or []
    else:
        results = []
    print(f"\n{len(results)} candidate(s) found:")
    for i, entry in enumerate(results):
        print(f"  [{i}] {entry!r}")
    return results


def extract_id(entry) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("geolocationId") or entry.get("id")
    return None


def try_forecastpoint(token: str, geolocation_id: str) -> None:
    url = FORECASTPOINT_URL_TEMPLATE.format(geolocation_id=geolocation_id)
    print(f"\n--- v2/forecastpoint for id={geolocation_id!r} ---")
    print(f"URL: {url}")
    status, body, err = _request(url, headers={"Authorization": f"Bearer {token}"})
    if err:
        print(f"FAILED (network/transport error): {err}")
        return
    print(f"HTTP {status}")
    if status == 200:
        try:
            payload = json.loads(body)
            keys = list(payload.keys()) if isinstance(payload, dict) else "non-dict"
            print(f"SUCCESS. Top-level keys: {keys}")
            if isinstance(payload, dict):
                for k in ("days", "three_hours", "hours"):
                    v = payload.get(k)
                    print(f"  {k}: {len(v) if isinstance(v, list) else v!r} entries")
        except json.JSONDecodeError:
            print(f"200 but not valid JSON: {body[:500]}")
    else:
        print(f"FAILED. Body: {body}")


def try_forecast_legacy(token: str, geolocation_id: str) -> None:
    url = FORECAST_URL_TEMPLATE.format(geolocation_id=geolocation_id)
    print(f"\n--- legacy /forecast for id={geolocation_id!r} (for comparison) ---")
    print(f"URL: {url}")
    status, body, err = _request(url, headers={"Authorization": f"Bearer {token}"})
    if err:
        print(f"FAILED (network/transport error): {err}")
        return
    print(f"HTTP {status}")
    if status == 200:
        print("SUCCESS (as expected — this is the one that's been working).")
    else:
        print(f"FAILED. Body: {body}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--consumer-key", required=True)
    parser.add_argument("--consumer-secret", required=True)
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    args = parser.parse_args()

    token = get_token(args.consumer_key, args.consumer_secret)
    if token is None:
        print("\nCan't continue without a token.")
        return 1

    results = get_geolocation(token, args.latitude, args.longitude)
    if not results:
        print("\nNo geolocation candidates returned — can't test forecastpoint.")
        return 1

    _print_header("STEP 3: Try forecastpoint + legacy forecast for EVERY candidate")
    print(
        "Testing every candidate returned above, not just the first — "
        "this integration currently always uses index [0], so if a "
        "later candidate is the one that actually works against "
        "forecastpoint, that's the bug (parse_geolocation_response "
        "should pick a different one)."
    )
    for i, entry in enumerate(results):
        geolocation_id = extract_id(entry)
        if geolocation_id is None:
            print(f"\n[{i}] Could not extract an ID from {entry!r}, skipping.")
            continue
        print(f"\n### Candidate [{i}]: raw entry = {entry!r}")
        try_forecast_legacy(token, geolocation_id)
        try_forecastpoint(token, geolocation_id)

    _print_header("STEP 4 (bonus): try forecastpoint with the comma percent-encoded")
    print(
        "Cheap thing to rule out: some API gateways treat a raw ',' in a "
        "path segment differently from a newer, stricter router. Testing "
        "candidate [0]'s ID with the comma swapped for %2C."
    )
    first_id = extract_id(results[0])
    if first_id and "," in first_id:
        encoded_id = first_id.replace(",", "%2C")
        try_forecastpoint(token, encoded_id)
    else:
        print("Candidate [0]'s ID doesn't contain a comma — nothing to test here.")

    _print_header("Done")
    print(
        "Send back everything printed above (it's already free of "
        "consumer_key/consumer_secret — those are never printed) and "
        "we'll know exactly which candidate/format actually works, if any."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
