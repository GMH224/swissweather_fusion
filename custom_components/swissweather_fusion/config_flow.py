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

from typing import Any, Optional

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
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
    CONF_STATION_PRESSURE_ENTITY,
    CONF_STATION_TEMP_ENTITY,
    DEFAULT_DIAGNOSTIC_LOGGING_ENABLED,
    DEFAULT_PURGE_DAYS,
    DOMAIN,
)

# v0.1.15: transient options-flow-only signal for clearing the elevation
# override (see SwissWeatherFusionOptionsFlow.async_step_init) — never
# read anywhere else, so it lives here rather than in const.py.
CONF_CLEAR_ELEVATION_OVERRIDE = "clear_elevation_override"


class SwissWeatherFusionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handles the multi-step setup flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: Optional[dict[str, Any]] = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data[CONF_LATITUDE] = user_input[CONF_LATITUDE]
            self._data[CONF_LONGITUDE] = user_input[CONF_LONGITUDE]
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
                vol.Required(CONF_LATITUDE, default=default_lat): vol.Coerce(float),
                vol.Required(CONF_LONGITUDE, default=default_lon): vol.Coerce(float),
                vol.Optional(CONF_ELEVATION_OVERRIDE): vol.Coerce(float),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

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
                vol.Required(CONF_SRF_CONSUMER_KEY): text_password,
                vol.Required(CONF_SRF_CONSUMER_SECRET): text_password,
                vol.Required(CONF_METEOBLUE_API_KEY): text_password,
                vol.Required(CONF_METEONOMIQS_API_KEY): text_password,
                vol.Optional(CONF_OPEN_METEO_API_KEY): text_password,
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
                self.hass.config_entries.async_update_entry(
                    existing_entry, data=self._data
                )
                await self.hass.config_entries.async_reload(existing_entry.entry_id)
                return self.async_abort(reason="reauth_successful")
            errors["base"] = "unknown"

        text_password = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SRF_CONSUMER_KEY): text_password,
                vol.Required(CONF_SRF_CONSUMER_SECRET): text_password,
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
                ): vol.Coerce(int),
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
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
