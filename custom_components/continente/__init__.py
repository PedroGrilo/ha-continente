"""Integração Continente: preços da lista de compras e envio para o carrinho do Continente Online.

Fala com o servidor MCP oficial do Continente (mcp.continente.pt). Nunca encomenda nem paga:
o login não pede a permissão order:create.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_entry_oauth2_flow, config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ContinenteApi, ContinenteAuthError, ContinenteError
from .const import CONF_CLIENT_ID, CONF_TODO_ENTITY, DEFAULT_TODO_ENTITY, DOMAIN, LIST_DEBOUNCE
from .oauth import ContinenteImplementation
from .pricing import ListPricer, parse_item
from .tracker import PriceTracker

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class ContinenteData:
    api: ContinenteApi
    pricer: ListPricer
    list_coordinator: DataUpdateCoordinator
    basket_coordinator: DataUpdateCoordinator
    prices_coordinator: DataUpdateCoordinator
    tracker: PriceTracker
    todo_entity: str


type ContinenteConfigEntry = ConfigEntry[ContinenteData]


async def _todo_items(
    hass: HomeAssistant, entity_id: str, status: tuple[str, ...] = ("needs_action",)
) -> list[dict[str, Any]]:
    resp = await hass.services.async_call(
        "todo",
        "get_items",
        {"status": list(status)},
        target={"entity_id": entity_id},
        blocking=True,
        return_response=True,
    )
    return (resp or {}).get(entity_id, {}).get("items", [])


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ContinenteConfigEntry) -> bool:
    config_entry_oauth2_flow.async_register_implementation(
        hass, DOMAIN, ContinenteImplementation(hass, entry.data[CONF_CLIENT_ID])
    )
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(hass, entry)
    oauth = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    api = ContinenteApi(async_get_clientsession(hass), oauth)
    pricer = ListPricer(hass, api, entry.entry_id)
    await pricer.async_load()
    tracker = PriceTracker(hass, pricer, entry.entry_id)
    await tracker.async_load()
    todo_entity = entry.options.get(CONF_TODO_ENTITY, DEFAULT_TODO_ENTITY)

    async def _update_list() -> dict[str, Any]:
        try:
            items = await _todo_items(hass, todo_entity, ("needs_action", "completed"))
            await tracker.note_list(items)
            return await pricer.async_price(items)
        except ContinenteAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (ContinenteError, HomeAssistantError) as err:
            raise UpdateFailed(str(err)) from err

    async def _update_basket() -> dict[str, Any]:
        try:
            return await api.view_basket()
        except ContinenteAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ContinenteError as err:
            raise UpdateFailed(str(err)) from err

    async def _update_prices() -> dict[str, Any]:
        try:
            return await tracker.async_update()
        except ContinenteAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ContinenteError as err:
            raise UpdateFailed(str(err)) from err

    list_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=f"{DOMAIN} lista",
        update_method=_update_list,
        update_interval=timedelta(hours=3),  # os preços mudam; a lista é seguida ao vivo abaixo
        request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=LIST_DEBOUNCE, immediate=False),
    )
    basket_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=f"{DOMAIN} carrinho",
        update_method=_update_basket,
        update_interval=timedelta(minutes=30),
    )

    # Sem intervalo: corre no arranque e às 9h e 18h (os avisos de promoção não chegam de madrugada).
    prices_coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=f"{DOMAIN} precos",
        update_method=_update_prices,
    )

    try:
        await basket_coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        raise
    except Exception as err:
        raise ConfigEntryNotReady(str(err)) from err
    await list_coordinator.async_refresh()  # também regista as compras antes dos preços
    await prices_coordinator.async_refresh()

    entry.runtime_data = ContinenteData(
        api, pricer, list_coordinator, basket_coordinator, prices_coordinator, tracker, todo_entity
    )

    async def _scheduled_prices(now) -> None:
        await prices_coordinator.async_refresh()

    entry.async_on_unload(async_track_time_change(hass, _scheduled_prices, hour=[9, 18], minute=0, second=0))

    @callback
    def _list_changed(event: Event[EventStateChangedData]) -> None:
        hass.async_create_task(list_coordinator.async_request_refresh())

    @callback
    def _todo_call(event: Event) -> None:
        # O estado da lista é só o nº de artigos por comprar: renomear um artigo
        # ("Massa" -> "2x Massa") não o muda, por isso seguimos também as ações todo.*.
        data = event.data
        if data.get("domain") != "todo" or data.get("service") == "get_items":
            return
        targets = (data.get("service_data") or {}).get("entity_id")
        targets = [targets] if isinstance(targets, str) else (targets or [])
        if todo_entity in targets:
            hass.async_create_task(list_coordinator.async_request_refresh())

    entry.async_on_unload(async_track_state_change_event(hass, [todo_entity], _list_changed))
    entry.async_on_unload(hass.bus.async_listen("call_service", _todo_call))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ContinenteConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


# ------------------------------------------------------------------------------- ações


def _data(hass: HomeAssistant) -> ContinenteData:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    raise HomeAssistantError("A integração Continente não está configurada ou ligada")


def _register_services(hass: HomeAssistant) -> None:
    async def search_product(call: ServiceCall) -> ServiceResponse:
        d = _data(hass)
        try:
            return await d.api.search_product(call.data["query"], call.data["limit"])
        except ContinenteError as err:
            raise HomeAssistantError(str(err)) from err

    async def price_list(call: ServiceCall) -> ServiceResponse:
        d = _data(hass)
        await d.list_coordinator.async_refresh()
        if not d.list_coordinator.last_update_success:
            raise HomeAssistantError(f"Falhou o cálculo dos preços: {d.list_coordinator.last_exception}")
        return d.list_coordinator.data

    async def choose_product(call: ServiceCall) -> ServiceResponse:
        d = _data(hass)
        name, _ = parse_item(call.data["item"])
        try:
            if call.data.get("sku"):
                chosen = await d.pricer.choose(name, call.data["sku"])
            else:
                await d.pricer.forget(name)
                chosen = None
        except ContinenteError as err:
            raise HomeAssistantError(str(err)) from err
        await d.list_coordinator.async_refresh()
        return {"item": name, "product": chosen}

    async def send_to_basket(call: ServiceCall) -> ServiceResponse:
        """Põe no carrinho os produtos da lista (ou os indicados). Não encomenda."""
        d = _data(hass)
        if call.data.get("items"):
            lines = [{"sku": str(i["sku"]), "qty": int(i.get("quantity", 1)), "name": i.get("name", i["sku"])}
                     for i in call.data["items"]]
        else:
            await d.list_coordinator.async_refresh()
            rows = (d.list_coordinator.data or {}).get("items", [])
            lines = [
                {"sku": r["product"]["sku"], "qty": r["qty"], "name": r["name"]}
                for r in rows
                if r.get("product") and r["product"].get("available", True)
            ]
        if call.data["dry_run"]:
            return {"dry_run": True, "would_add": lines}
        added, failed = [], []
        for line in lines:
            try:
                res = await d.api.add_to_basket(line["sku"], line["qty"])
                (added if res.get("success", True) and res.get("status") != "failed" else failed).append(
                    {**line, "summary": res.get("summary")}
                )
            except ContinenteAuthError as err:
                d.list_coordinator.config_entry.async_start_reauth(hass)
                raise HomeAssistantError(f"É preciso voltar a fazer login no Continente: {err}") from err
            except ContinenteError as err:
                failed.append({**line, "error": str(err)})
        await d.basket_coordinator.async_refresh()
        basket = d.basket_coordinator.data or {}
        return {"added": added, "failed": failed, "basket_totals": basket.get("totals", {})}

    async def get_product(call: ServiceCall) -> ServiceResponse:
        """Resposta completa do Continente para um produto (para ver todos os campos)."""
        d = _data(hass)
        try:
            return await d.api.get_product(call.data["sku"])
        except ContinenteError as err:
            raise HomeAssistantError(str(err)) from err

    async def update_prices(call: ServiceCall) -> ServiceResponse:
        d = _data(hass)
        await d.list_coordinator.async_refresh()
        await d.prices_coordinator.async_refresh()
        if not d.prices_coordinator.last_update_success:
            raise HomeAssistantError(f"Falhou a leitura dos preços: {d.prices_coordinator.last_exception}")
        return d.prices_coordinator.data

    async def view_basket(call: ServiceCall) -> ServiceResponse:
        d = _data(hass)
        await d.basket_coordinator.async_refresh()
        return d.basket_coordinator.data or {}

    hass.services.async_register(
        DOMAIN, "search_product", search_product,
        schema=vol.Schema({vol.Required("query"): cv.string,
                           vol.Optional("limit", default=10): vol.All(vol.Coerce(int), vol.Range(1, 20))}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, "price_list", price_list, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, "choose_product", choose_product,
        schema=vol.Schema({vol.Required("item"): cv.string, vol.Optional("sku"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "send_to_basket", send_to_basket,
        schema=vol.Schema({
            vol.Optional("dry_run", default=False): cv.boolean,
            vol.Optional("items"): [vol.Schema({vol.Required("sku"): cv.string,
                                                vol.Optional("quantity", default=1): vol.All(vol.Coerce(int), vol.Range(1, 99)),
                                                vol.Optional("name"): cv.string})],
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "view_basket", view_basket, supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, "get_product", get_product,
        schema=vol.Schema({vol.Required("sku"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, "update_prices", update_prices, supports_response=SupportsResponse.OPTIONAL)
