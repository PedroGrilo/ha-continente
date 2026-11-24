"""Configuração da integração Continente (login OAuth + escolha da lista de compras).

O Continente só aceita endereços de retorno em `localhost` (ou de plataformas de IA), por
isso o retorno automático do HA não serve. O login é feito assim:
  1. o HA regista-se como cliente (RFC 7591) e mostra o link de login, com PKCE;
  2. depois de iniciares sessão, o browser vai para http://localhost/... — a página não
     abre, e é isso que se espera;
  3. colas esse endereço no HA, que troca o código pelo token.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientError
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import LocalOAuth2ImplementationWithPkce
from yarl import URL

from .const import (
    AUTHORIZE_URL,
    CONF_CLIENT_ID,
    CONF_TODO_ENTITY,
    DEFAULT_TODO_ENTITY,
    DOMAIN,
    MCP_URL,
    SCOPES,
    TOKEN_URL,
)
from .oauth import async_register_client

_LOGGER = logging.getLogger(__name__)

REDIRECT_URI = "http://localhost/continente-ha"
CONF_RETURN_URL = "return_url"


class ContinenteFlowHandler(ConfigFlow, domain=DOMAIN):
    """Login no Continente."""

    VERSION = 1

    def __init__(self) -> None:
        self._client_id: str | None = None
        self._verifier: str | None = None
        self._state: str | None = None
        self._auth_url: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ContinenteOptionsFlow:
        return ContinenteOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self.async_step_login()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_login()

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if self._auth_url is None:
            try:
                self._client_id = await async_register_client(self.hass, REDIRECT_URI)
            except Exception:  # noqa: BLE001 - qualquer falha aqui impede o login
                return self.async_abort(reason="cannot_connect")
            self._verifier = LocalOAuth2ImplementationWithPkce.generate_code_verifier(96)
            self._state = secrets.token_urlsafe(16)
            self._auth_url = str(
                URL(AUTHORIZE_URL).with_query(
                    {
                        "response_type": "code",
                        "client_id": self._client_id,
                        "redirect_uri": REDIRECT_URI,
                        "scope": SCOPES,
                        "code_challenge": LocalOAuth2ImplementationWithPkce.compute_code_challenge(
                            self._verifier
                        ),
                        "code_challenge_method": "S256",
                        "resource": MCP_URL,
                        "state": self._state,
                        "ui_locales": "pt-PT",
                    }
                )
            )

        if user_input is not None:
            query = parse_qs(urlparse(user_input[CONF_RETURN_URL].strip()).query)
            code = (query.get("code") or [None])[0]
            if query.get("error"):
                errors["base"] = "login_refused"
            elif not code:
                errors["base"] = "no_code"
            elif (query.get("state") or [None])[0] != self._state:
                errors["base"] = "wrong_state"
            else:
                token = await _exchange_code(self.hass, self._client_id, code, self._verifier)
                if token is None:
                    errors["base"] = "token_failed"
                else:
                    data = {"auth_implementation": DOMAIN, "token": token, CONF_CLIENT_ID: self._client_id}
                    if self.source == "reauth":
                        return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
                    return self.async_create_entry(
                        title="Continente",
                        data=data,
                        options={CONF_TODO_ENTITY: _guess_todo_entity(self.hass)},
                    )

        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema({vol.Required(CONF_RETURN_URL): str}),
            description_placeholders={"url": self._auth_url},
            errors=errors,
        )


async def _exchange_code(hass: HomeAssistant, client_id: str, code: str, verifier: str) -> dict | None:
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": verifier,
                "resource": MCP_URL,
            },
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400 or "access_token" not in body:
                _LOGGER.error("O Continente recusou o código de login (%s): %s", resp.status, body)
                return None
    except (ClientError, ValueError) as err:
        _LOGGER.error("Falhou a troca do código de login: %s", err)
        return None
    body["expires_in"] = int(body.get("expires_in", 3600))
    body["expires_at"] = time.time() + body["expires_in"]
    return body


def _guess_todo_entity(hass: HomeAssistant) -> str:
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
