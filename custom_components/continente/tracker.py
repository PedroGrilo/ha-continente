"""Preços das últimas compras: guarda o preço de cada dia e compara com os últimos 7 e 30 dias.

O Continente só dá o preço de hoje, por isso o histórico é feito aqui: duas vezes por dia
(e no arranque) lê o preço dos produtos dos artigos comprados há menos tempo e guarda-o.

"Comprado" = artigo marcado como feito na lista de compras. A data fica registada quando
a integração vê o artigo passar a feito; os que já estavam feitos ao instalar ficam sem data.

Quando um produto fica a metade do preço (ou menos) dispara o evento `continente_promocao`,
uma vez por promoção, para uma automação mandar a notificação.
"""

from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION
from .pricing import ListPricer, norm, parse_item

_LOGGER = logging.getLogger(__name__)

EVENT_PROMOTION = f"{DOMAIN}_promocao"
HISTORY_DAYS = 40  # guarda um pouco mais do que os 30 dias mostrados
RECENT_LIMIT = 40  # quantos artigos comprados seguir
ALERT_DISCOUNT = 0.5  # 50 %


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _change(now: float | None, before: float | None) -> float | None:
    """Variação em % do preço de hoje face a antes (negativo = mais barato)."""
    if now is None or not before:
        return None
    return round((now / before - 1) * 100, 1)


class PriceTracker:
    """Últimas compras, histórico diário de preços e avisos de promoções."""

    def __init__(self, hass: HomeAssistant, pricer: ListPricer, entry_id: str) -> None:
        self.hass = hass
        self.pricer = pricer
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.precos")
        # norm(nome) -> {"name", "date" (AAAA-MM-DD ou None), "seq"}; seq maior = mais recente
        self.purchases: dict[str, dict[str, Any]] = {}
        self.completed_uids: set[str] | None = None  # None = ainda nunca vimos a lista
        self.history: dict[str, dict[str, float]] = {}  # sku -> {data: preço}
        self.alerted: dict[str, float] = {}  # sku -> preço com que já avisámos
        self._seq = 0
        self.result: dict[str, Any] = {"items": [], "promotions": 0, "updated": None}

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.purchases = data.get("purchases", {})
        uids = data.get("completed_uids")
        self.completed_uids = set(uids) if uids is not None else None
        self.history = data.get("history", {})
        self.alerted = data.get("alerted", {})
        self._seq = max((p.get("seq", 0) for p in self.purchases.values()), default=0)

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "purchases": self.purchases,
                "completed_uids": sorted(self.completed_uids or ()),
                "history": self.history,
                "alerted": self.alerted,
            }
        )

    # ------------------------------------------------------------------ compras

    async def note_list(self, todo_items: list[dict[str, Any]]) -> None:
        """Regista como comprados hoje os artigos que passaram a feitos desde a última vez."""
        completed = [i for i in todo_items if i.get("status") == "completed" and i.get("uid")]
        uids = {i["uid"] for i in completed}
        first_time = self.completed_uids is None
        new = [i for i in completed if first_time or i["uid"] not in self.completed_uids]
        if not new and uids == self.completed_uids:
            return
        today = dt_util.now().date().isoformat()
        # Na 1.ª vez a lista já tem o histórico de compras, sem datas: fica pela ordem da lista.
        for item in reversed(new) if first_time else new:
            name, _ = parse_item(item.get("summary", ""))
            key = norm(name)
            if not key:
                continue
            self._seq += 1
            prev = self.purchases.get(key, {})
            self.purchases[key] = {
                "name": name,
                "date": prev.get("date") if first_time else today,
                "seq": self._seq,
            }
        self.completed_uids = uids
        await self._save()

    def recent(self) -> list[tuple[str, dict[str, Any]]]:
        rows = sorted(self.purchases.items(), key=lambda kv: kv[1].get("seq", 0), reverse=True)
        return rows[:RECENT_LIMIT]

    # ------------------------------------------------------------------- preços

    async def async_update(self) -> dict[str, Any]:
        recent = self.recent()
        names = [p["name"] for _, p in recent]
        await self.pricer.refresh_choices(names)
        products = await self.pricer.async_products(names)

        today = dt_util.now().date()
        today_s = today.isoformat()
        oldest = (today - timedelta(days=HISTORY_DAYS)).isoformat()

        for product in products.values():
            if product and product.get("price") is not None:
                days = self.history.setdefault(product["sku"], {})
                days[today_s] = float(product["price"])
        for sku in list(self.history):
            days = {d: v for d, v in self.history[sku].items() if d >= oldest}
            if days:
                self.history[sku] = days
            else:
                del self.history[sku]

        items = []
        alerts = []
        for key, purchase in recent:
            product = products.get(key)
            items.append(self._row(purchase, product, today))
            row = items[-1]
            if product and row["discount"] is not None and row["discount"] >= ALERT_DISCOUNT * 100:
                sku = product["sku"]
                if sku not in self.alerted or row["price"] < self.alerted[sku]:
                    self.alerted[sku] = row["price"]
                    alerts.append(row)
            elif product:
                self.alerted.pop(product["sku"], None)  # a promoção acabou: pode voltar a avisar

        await self._save()
        for row in alerts:
            _LOGGER.info("Promoção: %s a %s € (antes %s €)", row["name"], row["price"], row["reference_price"])
            self.hass.bus.async_fire(
                EVENT_PROMOTION,
                {k: row[k] for k in ("name", "product", "sku", "price", "reference_price", "discount", "image")},
            )

        self.result = {
            "items": items,
            "promotions": sum(1 for r in items if (r["discount"] or 0) >= ALERT_DISCOUNT * 100),
            "updated": dt_util.now().isoformat(),
        }
        return self.result

    def _window(self, sku: str, today: date, days: int) -> list[float]:
        """Preços dos `days` dias antes de hoje (hoje não conta)."""
        start = (today - timedelta(days=days)).isoformat()
        end = today.isoformat()
        return [v for d, v in self.history.get(sku, {}).items() if start <= d < end]

    def _row(self, purchase: dict[str, Any], product: dict[str, Any] | None, today: date) -> dict[str, Any]:
        row: dict[str, Any] = {
            "name": purchase["name"],
            "bought": purchase.get("date"),
            "product": None,
            "sku": None,
            "image": None,
            "price": None,
            "price_7d": None,
            "price_30d": None,
            "change_7d": None,
            "change_30d": None,
            "reference_price": None,
            "discount": None,
        }
        if not product or product.get("price") is None:
            return row
        price = float(product["price"])
        last7 = self._window(product["sku"], today, 7)
        last30 = self._window(product["sku"], today, 30)
        # Referência para o desconto: o preço "normal" que o Continente indique, senão o
        # mais alto dos últimos 30 dias.
        reference = product.get("regular_price") or (max(last30) if last30 else None)
        discount = round((1 - price / reference) * 100) if reference and reference > price else None
        row.update(
            product=product.get("name"),
            sku=product["sku"],
            image=product.get("image"),
            price=price,
            price_7d=_mean(last7),
            price_30d=_mean(last30),
            reference_price=reference if discount else None,
            discount=discount,
        )
        row["change_7d"] = _change(price, row["price_7d"])
        row["change_30d"] = _change(price, row["price_30d"])
        return row
