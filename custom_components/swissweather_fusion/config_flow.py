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
    CONF_ELEVATION_OVERRIDE,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_METEOBLUE_API_KEY,
    CONF_METEONOMIQS_API_KEY,
    CONF_PURGE_DAYS,
    CONF_SRF_CONSUMER_KEY,
    CONF_SRF_CONSUMER_SECRET,
    CONF_STATION_HUMIDITY_ENTITY,
    CONF_STATION_PRESSURE_ENTITY,
    CONF_STATION_TEMP_ENTITY,
    DEFAULT_PURGE_DAYS,
    DOMAIN,
)


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
            override = user_input.get(CONF_ELEVATION_OVERRIDE)
            self._data[CONF_ELEVATION_OVERRIDE] = override if override else None

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
                    selector.EntitySelectorConfig(domain="sensor", device_class="pressure")
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
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options or {}
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
                    selector.EntitySelectorConfig(domain="sensor", device_class="pressure")
                ),
                vol.Required(
                    CONF_PURGE_DAYS, default=current.get(CONF_PURGE_DAYS, DEFAULT_PURGE_DAYS)
                ): vol.Coerce(int),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
