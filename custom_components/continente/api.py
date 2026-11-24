"""Cliente mínimo do servidor MCP do Continente (JSON-RPC sobre HTTP, respostas em SSE).

Não usa a biblioteca `mcp`: o servidor só precisa de pedidos JSON-RPC simples, e assim
a integração não depende de versões de pacotes partilhadas com outras integrações.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from homeassistant.helpers import config_entry_oauth2_flow

from .const import MCP_URL

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"


class ContinenteError(Exception):
    """Erro ao falar com o Continente."""


class ContinenteAuthError(ContinenteError):
    """O login deixou de ser válido (é preciso voltar a autenticar)."""


class ContinenteApi:
    """Chama as ferramentas do MCP do Continente com o token da config entry."""

    def __init__(
        self,
        websession: ClientSession,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
    ) -> None:
        self._websession = websession
        self._oauth = oauth_session
        self._initialized = False
        self._next_id = 0

    async def _post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        try:
            await self._oauth.async_ensure_token_valid()
        except Exception as err:  # o HA levanta vários tipos conforme a versão
            raise ContinenteAuthError(f"Não foi possível renovar o login: {err}") from err
        token = self._oauth.token["access_token"]
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        try:
            async with self._websession.post(
                MCP_URL, json=message, headers=headers, timeout=ClientTimeout(total=60)
            ) as resp:
                if resp.status in (401, 403):
                    raise ContinenteAuthError(f"O Continente recusou o login ({resp.status})")
                if resp.status == 202:
                    return None
                body = await resp.text()
                if resp.status >= 400:
                    raise ContinenteError(f"HTTP {resp.status}: {body[:200]}")
                ctype = resp.headers.get("Content-Type", "")
        except ClientError as err:
            raise ContinenteError(f"Sem ligação ao Continente: {err}") from err

        if "text/event-stream" in ctype:
            # Cada evento traz uma linha "data: {...}"; a resposta é o último objeto com "id".
            payload = None
            for line in body.splitlines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data:
                        obj = json.loads(data)
                        if "id" in obj:
                            payload = obj
            return payload
        return json.loads(body) if body else None

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        reply = await self._post(msg)
        if reply is None:
            raise ContinenteError(f"Resposta vazia a {method}")
        if "error" in reply:
            raise ContinenteError(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result")

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "home-assistant-continente", "version": "0.1.0"},
            },
        )
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Chama uma ferramenta e devolve o JSON que ela põe no conteúdo de texto."""
        await self._ensure_initialized()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content", []))
            if "permission" in text.lower() or "logged-in" in text.lower():
                raise ContinenteAuthError(text[:300])
            raise ContinenteError(f"{name}: {text[:300]}")
        if "structuredContent" in result and isinstance(result["structuredContent"], dict):
            return result["structuredContent"]
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except ValueError:
                    return {"text": item["text"]}
        return {}

    # --- atalhos para as ferramentas usadas --------------------------------------

    async def search_product(self, query: str, limit: int = 10) -> dict[str, Any]:
        return await self.call_tool("search_product", {"query": query[:80], "limit": limit})

    async def get_product(self, sku: str) -> dict[str, Any]:
        return await self.call_tool("get_product", {"productId": sku})

    async def resolve_list(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Encontra produtos para vários artigos de uma vez, SEM mexer no carrinho."""
        return await self.call_tool("add_shopping_list", {"items": items, "dryRun": True})

    async def add_to_basket(self, sku: str, quantity: int) -> dict[str, Any]:
        return await self.call_tool(
            "add_to_basket", {"productId": sku, "quantity": max(1, min(99, int(quantity)))}
        )

    async def view_basket(self) -> dict[str, Any]:
        return await self.call_tool("view_basket")

    async def most_bought(self) -> list[dict[str, Any]]:
        return (await self.call_tool("list_most_bought")).get("products", [])

    async def favorites(self) -> list[dict[str, Any]]:
        data = await self.call_tool("list_favorites")
        return data.get("products") or data.get("favorites") or data.get("results") or []
