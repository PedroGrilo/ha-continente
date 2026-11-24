"""Configuração da integração Continente (login OAuth + escolha da lista de compras)."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .const import CONF_CLIENT_ID, CONF_TODO_ENTITY, DEFAULT_TODO_ENTITY, DOMAIN
from .oauth import ContinenteImplementation, async_register_client

_LOGGER = logging.getLogger(__name__)


class ContinenteFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Login no Continente."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._client_id: str | None = None

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ContinenteOptionsFlow:
        return ContinenteOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._async_start_login()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self._async_start_login()

    async def _async_start_login(self) -> ConfigFlowResult:
        """Regista este HA no Continente (uma vez por login) e abre a página de login."""
        redirect_uri = config_entry_oauth2_flow.async_get_redirect_uri(self.hass)
        try:
            self._client_id = await async_register_client(self.hass, redirect_uri)
        except Exception:  # noqa: BLE001 - qualquer falha aqui impede o login
            return self.async_abort(reason="cannot_connect")
        self.async_register_implementation(
            self.hass, ContinenteImplementation(self.hass, self._client_id)
        )
        return await self.async_step_pick_implementation(user_input={"implementation": DOMAIN})

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        data[CONF_CLIENT_ID] = self._client_id
        if self.source == "reauth":
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        return self.async_create_entry(
            title="Continente",
            data=data,
            options={CONF_TODO_ENTITY: _guess_todo_entity(self.hass)},
        )


def _guess_todo_entity(hass) -> str:
    ids = hass.states.async_entity_ids("todo")
    if DEFAULT_TODO_ENTITY in ids:
        return DEFAULT_TODO_ENTITY
    for eid in ids:
        if "compra" in eid or "shopping" in eid:
            return eid
    return ids[0] if ids else DEFAULT_TODO_ENTITY


class ContinenteOptionsFlow(OptionsFlowWithReload):
    """Escolher a lista de compras a que se mostram os preços."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_TODO_ENTITY, DEFAULT_TODO_ENTITY)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TODO_ENTITY, default=current): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="todo")
                    )
                }
            ),
        )
