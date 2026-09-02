"""Config flow for SwissWeather Fusion.

Split into logical steps rather than one large form, per HA UX convention:
  1. location — coordinates (defaulting to HA's home location) + optional
     precise/DGPS elevation override. Coordinates only, never a postal
     code — see DEVELOPER.md for why (a zip code covers an area wider than
     the microclimate variation this whole project exists to correct for).
  2. station — local sensor entity selection (temperature/humidity/
     pressure). Rain/wind intentionally absent until the station gains
     those sensors.
  3. credentials — SRF, meteoblue, and Meteonomiqs API credentials. All
     masked password-style fields; never written to a YAML file.

All fields are re-editable later via the options flow, not just at setup.
"""
from __future__ import annotations

import math
from typing import Any, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .clients.open_meteo import OpenMeteoClient
from .const import (
    CONF_DIAGNOSTIC_LOGGING_ENABLED,
    CONF_ELEVATION_OVERRIDE,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_METEOBLUE_API_KEY,
    CONF_METEONOMIQS_API_KEY,
    CONF_OPEN_METEO_API_KEY,
    CONF_PURGE_DAYS,
    CONF_SRF_CONSUMER_KEY,
    CONF_SRF_CONSUMER_SECRET,
    CONF_STATION_HUMIDITY_ENTITY,
    CONF_CLEAR_OPEN_METEO_API_KEY,
    CONF_STATION_PRESSURE_ENTITY,
    CONF_STATION_PRESSURE_IS_SEA_LEVEL,
    CONF_STATION_TEMP_ENTITY,
    DEFAULT_DIAGNOSTIC_LOGGING_ENABLED,
    DEFAULT_PURGE_DAYS,
    DEFAULT_STATION_PRESSURE_IS_SEA_LEVEL,
    DOMAIN,
)

# v0.1.15: transient options-flow-only signal for clearing the elevation
# override (see SwissWeatherFusionOptionsFlow.async_step_init) — never
# read anywhere else, so it lives here rather than in const.py.
CONF_CLEAR_ELEVATION_OVERRIDE = "clear_elevation_override"


# ---------------------------------------------------------------------------
# Field validators (v0.1.24: P1-27, P1-28, P1-30)
# ---------------------------------------------------------------------------
def _finite_float(value: Any) -> float:
    """Coerce to float and reject NaN/Infinity.

    **v0.1.24 fix (P1-27).** The coordinate and elevation fields used bare
    vol.Coerce(float), which happily accepts the strings "nan", "inf" and
    "-inf" — every one of which produces a valid float and no error. A
    non-finite latitude propagates into the LV95 coordinate transform, the
    STAC query and every provider URL, failing far from where it was
    entered and in ways that look like a provider outage.
    """
    try:
        result = float(value)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("must be a number") from err
    if not math.isfinite(result):
        raise vol.Invalid("must be a finite number")
    return result


def _non_empty_str(value: Any) -> str:
    """Reject an empty or whitespace-only credential.

    **v0.1.24 fix (P1-30).** vol.Required only requires that the KEY be
    present in the submitted dict, not that its value be meaningful. An
    empty secret therefore saved cleanly and failed later at request
    time, surfacing as an authentication error rather than as the
    data-entry mistake it actually was.
    """
    if value is None:
        raise vol.Invalid("must not be empty")
    stripped = str(value).strip()
    if not stripped:
        raise vol.Invalid("must not be empty")
    return stripped


_LATITUDE_VALIDATOR = vol.All(_finite_float, vol.Range(min=-90.0, max=90.0))
_LONGITUDE_VALIDATOR = vol.All(_finite_float, vol.Range(min=-180.0, max=180.0))
# -430 m is roughly the Dead Sea shore, the lowest dry land on Earth;
# 9000 m is comfortably above Everest. Wide enough never to reject a real
# location, narrow enough to catch a typo or a unit mix-up.
_ELEVATION_VALIDATOR = vol.All(_finite_float, vol.Range(min=-430.0, max=9000.0))
# v0.1.24 fix (P1-28): purge_days was bare vol.Coerce(int) with no lower
# bound. RetentionCoordinator treats purge_days <= 0 as "keep forever", so
# a negative value silently became a second spelling of forever rather
# than the error it looks like.
_PURGE_DAYS_VALIDATOR = vol.All(vol.Coerce(int), vol.Range(min=0))


def _location_unique_id(latitude: float, longitude: float) -> str:
    """Stable identity for a configured location (v0.1.24, P2-13).

    Rounded to 4 decimal places (~11 m). Without rounding, floating-point
    noise between two submissions of "the same" coordinates produces two
    different unique IDs and defeats the duplicate check entirely — which
    is the failure mode this is meant to prevent, since each duplicate
    entry spins up its own full set of coordinators and independently
    consumes provider quota against the same account limits.
    """
    return f"{round(latitude, 4)}_{round(longitude, 4)}"


class SwissWeatherFusionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handles the multi-step setup flow."""

    # v0.1.24 (IND-05): bumped alongside the new
    # CONF_STATION_PRESSURE_IS_SEA_LEVEL key. See async_migrate_entry in
    # __init__.py — without a matching migration handler, Home Assistant
    # refuses to load an entry older than the flow's version.
    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data[CONF_LATITUDE] = user_input[CONF_LATITUDE]
            self._data[CONF_LONGITUDE] = user_input[CONF_LONGITUDE]

            # v0.1.24 fix (P2-13): nothing previously stopped the same
            # physical location being added twice as two independent
            # config entries, each starting its own eleven coordinators
            # and independently spending real provider quota against the
            # same account limits. Set as soon as coordinates are known,
            # which is the earliest point the identity exists.
            #
            # Note on the audit finding this comes from: its claim that
            # entities lacked a durable unique_id did NOT hold up —
            # _BaseSensor, weather.py and binary_sensor.py all anchor on
            # entry.entry_id already. Config-entry-level duplication was
            # the real half of that finding.
            await self.async_set_unique_id(
                _location_unique_id(
                    self._data[CONF_LATITUDE], self._data[CONF_LONGITUDE]
                )
            )
            self._abort_if_unique_id_configured()
            # v0.1.15 fix: this used to be `override if override else None`,
            # which treats 0.0 (a legitimate sea-level override) as falsy
            # and silently discards it — confirmed by an outside code
            # review. An explicit `is not None` check is required here:
            # `.get()` already returns None for a field the user left
            # blank, so there's no ambiguity to resolve with truthiness in
            # the first place.
            override = user_input.get(CONF_ELEVATION_OVERRIDE)
            self._data[CONF_ELEVATION_OVERRIDE] = override

            # Auto-lookup via Open-Meteo's free Elevation API — this is why
            # the override above is optional, not required.
            session = async_get_clientsession(self.hass)
            client = OpenMeteoClient(session)
            try:
                looked_up = await client.async_fetch_elevation(
                    latitude=self._data[CONF_LATITUDE],
                    longitude=self._data[CONF_LONGITUDE],
                )
            except Exception:  # noqa: BLE001 - surfaced to the user as a form error
                looked_up = None
                errors["base"] = "elevation_lookup_failed"

            if looked_up is None and self._data[CONF_ELEVATION_OVERRIDE] is None:
                errors["base"] = "elevation_lookup_failed"
            else:
                self._data["elevation_looked_up"] = looked_up
                return await self.async_step_station()

        default_lat = self.hass.config.latitude
        default_lon = self.hass.config.longitude

        schema = vol.Schema(
            {
                vol.Required(CONF_LATITUDE, default=default_lat): _LATITUDE_VALIDATOR,
                vol.Required(CONF_LONGITUDE, default=default_lon): _LONGITUDE_VALIDATOR,
                vol.Optional(CONF_ELEVATION_OVERRIDE): _ELEVATION_VALIDATOR,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        """Change coordinates and elevation for an existing installation.

        **v0.1.24 fix (P1-26).** Latitude and longitude were captured only
        during initial setup and never exposed again anywhere. A relocated
        installation had no supported path except remove-and-re-add, which
        discards all learned bucket_stats and every accumulated
        storm_event — a genuinely destructive workaround for what should
        be a routine edit.

        Reuses async_step_user's schema, validators and elevation lookup
        rather than reimplementing them, and updates the EXISTING entry in
        place rather than creating a second, competing one.

        Coordinates are written to entry.data specifically, because
        diagnostics.py resolves them from entry.data for its
        coordinate-redaction pass. Writing them to options instead would
        silently stop that redaction from matching.
        """
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(
            self.context.get("entry_id", "")
        )
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            latitude = user_input[CONF_LATITUDE]
            longitude = user_input[CONF_LONGITUDE]
            override = user_input.get(CONF_ELEVATION_OVERRIDE)

            session = async_get_clientsession(self.hass)
            client = OpenMeteoClient(session)
            try:
                looked_up = await client.async_fetch_elevation(
                    latitude=latitude, longitude=longitude
                )
            except Exception:  # noqa: BLE001 - surfaced as a form error
                looked_up = None
                errors["base"] = "elevation_lookup_failed"

            if looked_up is None and override is None:
                errors["base"] = "elevation_lookup_failed"
            else:
                new_data = dict(entry.data)
                new_data[CONF_LATITUDE] = latitude
                new_data[CONF_LONGITUDE] = longitude
                new_data[CONF_ELEVATION_OVERRIDE] = override
                new_data["elevation_looked_up"] = looked_up
                # Keep the entry's identity consistent with its new
                # location so the P2-13 duplicate check keeps working
                # after a move. Deliberately NOT followed by
                # _abort_if_unique_id_configured(): here the matching
                # entry is this same entry, and aborting on it would make
                # relocating impossible — the exact problem this step
                # exists to solve.
                await self.async_set_unique_id(
                    _location_unique_id(latitude, longitude)
                )
                self.hass.config_entries.async_update_entry(
                    entry, data=new_data, unique_id=self.unique_id
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        current_override = entry.data.get(CONF_ELEVATION_OVERRIDE)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LATITUDE, default=entry.data.get(CONF_LATITUDE)
                ): _LATITUDE_VALIDATOR,
                vol.Required(
                    CONF_LONGITUDE, default=entry.data.get(CONF_LONGITUDE)
                ): _LONGITUDE_VALIDATOR,
                vol.Optional(
                    CONF_ELEVATION_OVERRIDE,
                    default=current_override if current_override is not None else 0.0,
                ): _ELEVATION_VALIDATOR,
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    async def async_step_station(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        if user_input is not None:
            self._data[CONF_STATION_TEMP_ENTITY] = user_input[CONF_STATION_TEMP_ENTITY]
            self._data[CONF_STATION_HUMIDITY_ENTITY] = user_input[
                CONF_STATION_HUMIDITY_ENTITY
            ]
            self._data[CONF_STATION_PRESSURE_ENTITY] = user_input[
                CONF_STATION_PRESSURE_ENTITY
            ]
            self._data[CONF_STATION_PRESSURE_IS_SEA_LEVEL] = user_input.get(
                CONF_STATION_PRESSURE_IS_SEA_LEVEL,
                DEFAULT_STATION_PRESSURE_IS_SEA_LEVEL,
            )
            return await self.async_step_credentials()

        schema = vol.Schema(
            {
                vol.Required(CONF_STATION_TEMP_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
                vol.Required(CONF_STATION_HUMIDITY_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="humidity")
                ),
                vol.Required(CONF_STATION_PRESSURE_ENTITY): selector.EntitySelector(
                    # Fixed in v0.1.1: was device_class="pressure" only,
                    # which silently excluded sensors using the more
                    # specific atmospheric_pressure class (e.g. Netatmo) —
                    # both classes cover the same weather-pressure sensors,
                    # just categorized under HA's newer, more specific
                    # taxonomy for some integrations.
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class=["pressure", "atmospheric_pressure"]
                    )
                ),
                # v0.1.24 (P1-22): asked explicitly because it genuinely
                # cannot be inferred. Netatmo publishes BOTH a
                # sea-level-normalised "Pressure" and a raw
                # "AbsolutePressure", and Home Assistant gives both the
                # same atmospheric_pressure device class — so the
                # selector above cannot tell them apart and neither can
                # any runtime heuristic. Default False (station-level),
                # the physically honest reading.
                vol.Required(
                    CONF_STATION_PRESSURE_IS_SEA_LEVEL,
                    default=DEFAULT_STATION_PRESSURE_IS_SEA_LEVEL,
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="station", data_schema=schema)

    async def async_step_credentials(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        if user_input is not None:
            self._data[CONF_SRF_CONSUMER_KEY] = user_input[CONF_SRF_CONSUMER_KEY]
            self._data[CONF_SRF_CONSUMER_SECRET] = user_input[CONF_SRF_CONSUMER_SECRET]
            self._data[CONF_METEOBLUE_API_KEY] = user_input[CONF_METEOBLUE_API_KEY]
            self._data[CONF_METEONOMIQS_API_KEY] = user_input[CONF_METEONOMIQS_API_KEY]
            # Optional (v0.1.3) — free tier needs none of this at all.
            self._data[CONF_OPEN_METEO_API_KEY] = user_input.get(CONF_OPEN_METEO_API_KEY) or None
            self._data[CONF_PURGE_DAYS] = DEFAULT_PURGE_DAYS

            return self.async_create_entry(
                title="SwissWeather Fusion", data=self._data
            )

        text_password = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SRF_CONSUMER_KEY): vol.All(
                    text_password, _non_empty_str
                ),
                vol.Required(CONF_SRF_CONSUMER_SECRET): vol.All(
                    text_password, _non_empty_str
                ),
                vol.Required(CONF_METEOBLUE_API_KEY): vol.All(
                    text_password, _non_empty_str
                ),
                vol.Required(CONF_METEONOMIQS_API_KEY): vol.All(
                    text_password, _non_empty_str
                ),
                vol.Optional(CONF_OPEN_METEO_API_KEY): text_password,
                # v0.1.24 fix (IND-06): retention is now asked at setup
                # instead of being silently hard-coded. It defaulted to 0
                # — "keep forever" — and the only place it appeared was
                # the options flow, which a user has no reason to open.
                # Every installation therefore ran unbounded SQLite
                # growth on SD-card class hardware by default.
                vol.Required(
                    CONF_PURGE_DAYS, default=DEFAULT_PURGE_DAYS
                ): _PURGE_DAYS_VALIDATOR,
            }
        )
        return self.async_show_form(step_id="credentials", data_schema=schema)

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Triggered when a credential is rotated (e.g. SRF consumer
        key/secret regenerated on their portal) — separate from the
        automatic 7-day bearer-token refresh, which needs no user action.
        """
        self._data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data[CONF_SRF_CONSUMER_KEY] = user_input[CONF_SRF_CONSUMER_KEY]
            self._data[CONF_SRF_CONSUMER_SECRET] = user_input[CONF_SRF_CONSUMER_SECRET]
            existing_entry = self.hass.config_entries.async_get_entry(
                self.context["entry_id"]
            )
            if existing_entry is not None:
                # v0.1.24 fix (P1-02): also clear any stale copy of these
                # keys from entry.options.
                #
                # Every runtime credential is resolved options-first —
                # options.get(KEY, data[KEY]) — so if the same key had
                # ever been set through the options flow, that older value
                # kept winning at runtime even after the UI reported
                # "reauth successful". The user sees success, the
                # integration keeps using the revoked credential, and
                # nothing explains why. Unrelated option entries are
                # preserved.
                new_options = dict(existing_entry.options)
                new_options.pop(CONF_SRF_CONSUMER_KEY, None)
                new_options.pop(CONF_SRF_CONSUMER_SECRET, None)
                self.hass.config_entries.async_update_entry(
                    existing_entry, data=self._data, options=new_options
                )
                await self.hass.config_entries.async_reload(existing_entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            errors["base"] = "unknown"

        text_password = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SRF_CONSUMER_KEY): vol.All(
                    text_password, _non_empty_str
                ),
                vol.Required(CONF_SRF_CONSUMER_SECRET): vol.All(
                    text_password, _non_empty_str
                ),
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "SwissWeatherFusionOptionsFlow":
        return SwissWeatherFusionOptionsFlow(config_entry)


class SwissWeatherFusionOptionsFlow(config_entries.OptionsFlow):
    """Lets every field from setup be changed later, without reinstalling —
    poll intervals, purge window, and sensor selection specifically (plan
    doc §6 requirement)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        current = self._config_entry.options or {}
        # v0.1.15: options-first, data-fallback — same pattern used for
        # every other field here — but note current.get(key, fallback)
        # correctly distinguishes "key present with value None" (the user
        # explicitly cleared it before) from "key never set" (falls
        # through to entry.data), since dict.get only uses its fallback
        # when the key is absent, not when its value is None.
        current_elevation_override = current.get(
            CONF_ELEVATION_OVERRIDE, self._config_entry.data.get(CONF_ELEVATION_OVERRIDE)
        )

        if user_input is not None:
            # v0.1.2 fix: credential fields were entirely missing from
            # this flow — there was no way to view or change them after
            # initial setup at all, exactly the gap reported after
            # deployment. They're optional here and mean "leave blank to
            # keep the existing value" (a masked secret can't be
            # pre-filled for the user to see, and typing over it with a
            # blank shouldn't erase a working credential by accident).
            result = dict(user_input)
            for key in (
                CONF_SRF_CONSUMER_KEY,
                CONF_SRF_CONSUMER_SECRET,
                CONF_METEOBLUE_API_KEY,
                CONF_METEONOMIQS_API_KEY,
                CONF_OPEN_METEO_API_KEY,
            ):
                if not result.get(key):
                    existing = current.get(key, self._config_entry.data.get(key))
                    if existing:
                        result[key] = existing
                    else:
                        result.pop(key, None)

            # v0.1.15 fix (addendum finding): elevation override had no
            # options-flow field at all — the only way to change or clear
            # it was to reinstall the integration. Unlike the credential
            # fields above, "leave blank to keep existing" isn't the right
            # semantic here — an explicit checkbox controls clearing
            # instead, since a numeric field can't unambiguously
            # distinguish "the user cleared this" from "the user left the
            # pre-filled value alone" the way a blank password field can.
            # Explicit `is not None` throughout — 0.0 is a legitimate
            # override (sea level) and must never be treated as "unset".
            # v0.1.24 fix (P1-29): the "blank means keep existing" loop
            # above is correct and necessary for the four REQUIRED
            # credentials, since a masked password field cannot be
            # pre-filled. Applied uniformly it also made the ONE
            # genuinely optional credential impossible to remove, so a
            # user could never return to Open-Meteo's free tier. Checked
            # AFTER the backfill loop so it explicitly overrides whatever
            # that loop just restored.
            if result.pop(CONF_CLEAR_OPEN_METEO_API_KEY, False):
                result.pop(CONF_OPEN_METEO_API_KEY, None)

            if result.pop(CONF_CLEAR_ELEVATION_OVERRIDE, False):
                result[CONF_ELEVATION_OVERRIDE] = None
            # else: result[CONF_ELEVATION_OVERRIDE] already holds whatever
            # numeric value the form submitted, including 0.0.

            return self.async_create_entry(title="", data=result)

        text_password = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_STATION_TEMP_ENTITY,
                    default=current.get(
                        CONF_STATION_TEMP_ENTITY,
                        self._config_entry.data.get(CONF_STATION_TEMP_ENTITY),
                    ),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
                vol.Required(
                    CONF_STATION_HUMIDITY_ENTITY,
                    default=current.get(
                        CONF_STATION_HUMIDITY_ENTITY,
                        self._config_entry.data.get(CONF_STATION_HUMIDITY_ENTITY),
                    ),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="humidity")
                ),
                vol.Required(
                    CONF_STATION_PRESSURE_ENTITY,
                    default=current.get(
                        CONF_STATION_PRESSURE_ENTITY,
                        self._config_entry.data.get(CONF_STATION_PRESSURE_ENTITY),
                    ),
                ): selector.EntitySelector(
                    # Same fix as the initial setup step — see there for
                    # the full explanation.
                    selector.EntitySelectorConfig(
                        domain="sensor", device_class=["pressure", "atmospheric_pressure"]
                    )
                ),
                vol.Optional(CONF_SRF_CONSUMER_KEY): text_password,
                vol.Optional(CONF_SRF_CONSUMER_SECRET): text_password,
                vol.Optional(CONF_METEOBLUE_API_KEY): text_password,
                vol.Optional(CONF_METEONOMIQS_API_KEY): text_password,
                vol.Optional(CONF_OPEN_METEO_API_KEY): text_password,
                vol.Required(
                    CONF_PURGE_DAYS, default=current.get(CONF_PURGE_DAYS, DEFAULT_PURGE_DAYS)
                ): _PURGE_DAYS_VALIDATOR,
                vol.Required(
                    CONF_DIAGNOSTIC_LOGGING_ENABLED,
                    default=current.get(
                        CONF_DIAGNOSTIC_LOGGING_ENABLED, DEFAULT_DIAGNOSTIC_LOGGING_ENABLED
                    ),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_ELEVATION_OVERRIDE,
                    default=current_elevation_override
                    if current_elevation_override is not None
                    else 0.0,
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_CLEAR_ELEVATION_OVERRIDE,
                    default=current_elevation_override is None,
                ): selector.BooleanSelector(),
                # v0.1.24 (P1-29): see the handler above.
                vol.Optional(
                    CONF_CLEAR_OPEN_METEO_API_KEY, default=False
                ): selector.BooleanSelector(),
                # v0.1.24 (P1-22): editable after setup, since a user may
                # switch between Netatmo's normalised "Pressure" and its
                # raw "AbsolutePressure" entity at any time.
                vol.Required(
                    CONF_STATION_PRESSURE_IS_SEA_LEVEL,
                    default=current.get(
                        CONF_STATION_PRESSURE_IS_SEA_LEVEL,
                        self._config_entry.data.get(
                            CONF_STATION_PRESSURE_IS_SEA_LEVEL,
                            DEFAULT_STATION_PRESSURE_IS_SEA_LEVEL,
                        ),
                    ),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
