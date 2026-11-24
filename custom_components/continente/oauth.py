"""Login OAuth do Continente: registo automático do cliente (RFC 7591) + PKCE."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientError

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import AUTHORIZE_URL, DOMAIN, MCP_URL, REGISTER_URL, SCOPES, TOKEN_URL

_LOGGER = logging.getLogger(__name__)


class ContinenteImplementation(config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce):
    """Cliente público (sem segredo) com PKCE e o parâmetro `resource` do MCP."""

    def __init__(self, hass: HomeAssistant, client_id: str) -> None:
        super().__init__(hass, DOMAIN, client_id, AUTHORIZE_URL, TOKEN_URL)

    @property
    def name(self) -> str:
        return "Continente"

    @property
    def extra_authorize_data(self) -> dict:
        data: dict[str, Any] = {"scope": SCOPES, "resource": MCP_URL, "ui_locales": "pt-PT"}
        data.update(super().extra_authorize_data)
        return data

    @property
    def extra_token_resolve_data(self) -> dict:
        data: dict[str, Any] = {"resource": MCP_URL}
        data.update(super().extra_token_resolve_data)
        return data

    # A renovação usa a do HA: o Continente aceita-a sem `resource` e roda o refresh
    # token a cada vez; o HA junta o token novo ao antigo e grava-o na config entry.


async def async_register_client(hass: HomeAssistant, redirect_uri: str) -> str:
    """Regista este Home Assistant como cliente OAuth no Continente e devolve o client_id."""
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            REGISTER_URL,
            json={
                "client_name": "Home Assistant",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": SCOPES,
            },
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400 or "client_id" not in body:
                raise ValueError(f"registo recusado ({resp.status}): {body}")
            return body["client_id"]
    except (ClientError, ValueError) as err:
        _LOGGER.error("Falhou o registo do cliente OAuth no Continente: %s", err)
        raise
