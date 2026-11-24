"""Sensores: preço estimado da lista de compras e total do carrinho do Continente."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ContinenteConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ContinenteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    d = entry.runtime_data
    device = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Continente",
        manufacturer="Continente Online",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://www.continente.pt",
    )
    async_add_entities(
        [
            ListPriceSensor(d.list_coordinator, entry, device, d.todo_entity),
            BasketSensor(d.basket_coordinator, entry, device),
        ]
    )


class _Base(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, entry, device: DeviceInfo, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = device


class ListPriceSensor(_Base):
    """Total estimado da lista; cada artigo com produto, preço e alternativas nos atributos."""

    _attr_icon = "mdi:cart-variant"
    # A lista de artigos pode ser grande e muda muito: não vai para o histórico.
    _unrecorded_attributes = frozenset({"items", "not_found"})

    def __init__(self, coordinator, entry, device, todo_entity: str) -> None:
        super().__init__(coordinator, entry, device, "lista")
        self._todo_entity = todo_entity

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get("total")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        items = [
            {
                "uid": r["uid"],
                "name": r["name"],
                "qty": r["qty"],
                "source": r["source"],
                "line_total": r.get("line_total"),
                "product": r["product"],
                "alternatives": [
                    {k: a[k] for k in ("sku", "name", "price", "available")} for a in r["alternatives"]
                ],
            }
            for r in data.get("items", [])
        ]
        return {"todo_entity": self._todo_entity, "items": items, "not_found": data.get("not_found", [])}


class BasketSensor(_Base):
    """Total do carrinho do Continente (o mesmo da app e do site)."""

    _attr_icon = "mdi:cart"
    _unrecorded_attributes = frozenset({"items"})

    def __init__(self, coordinator, entry, device) -> None:
        super().__init__(coordinator, entry, device, "carrinho")

    @property
    def native_value(self) -> float | None:
        return ((self.coordinator.data or {}).get("totals") or {}).get("totalEur")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        totals = data.get("totals") or {}
        return {
            "total_items": totals.get("totalItems"),
            "min_order": totals.get("minOrderEur"),
            "delivery_fee": totals.get("deliveryFeeEur"),
            "items": [
                {"sku": i.get("productId") or i.get("sku"), "name": i.get("name"), "qty": i.get("quantity")}
                for i in data.get("items", [])
            ],
        }
