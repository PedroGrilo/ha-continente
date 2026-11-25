"""Liga os artigos da lista de compras a produtos do Continente e calcula os preços.

Para cada artigo por comprar, o produto escolhido vem de (por esta ordem):
  1. escolha guardada: o produto que escolheste antes para esse nome;
  2. habituais: um dos teus "mais comprados" ou favoritos cujo nome contenha o artigo;
  3. pesquisa: a melhor correspondência do catálogo (add_shopping_list em pré-visualização,
     que nunca mexe no carrinho), com as alternativas para poderes trocar.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import ContinenteApi, ContinenteError
from .const import DOMAIN, LOOKUP_TTL, STORAGE_VERSION, USUALS_TTL

_LOGGER = logging.getLogger(__name__)

QTY_PREFIX = re.compile(r"^\s*(\d{1,3})\s*[xX×*]\s*")
QTY_SUFFIX = re.compile(r"\s+[xX×]\s*(\d{1,3})\s*$")
CATEGORY = re.compile(r"\s*\[[^\]]+\]\s*$")
TAG = re.compile(r"#[\w-]+")


def norm(text: str) -> str:
    """Minúsculas, sem acentos nem pontuação, espaços normalizados."""
    t = unicodedata.normalize("NFD", str(text or "")).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def parse_item(summary: str) -> tuple[str, int]:
    """'2x Massa [massaearroz]' -> ('Massa', 2); 'Granola x2' -> ('Granola', 2)."""
    qty = 1
    text = summary or ""
    if m := QTY_PREFIX.match(text):
        qty = int(m.group(1))
        text = text[m.end():]
    text = TAG.sub("", CATEGORY.sub("", text)).strip()
    if m := QTY_SUFFIX.search(text):
        qty = int(m.group(1))
        text = text[: m.start()].strip()
    return text or summary, max(1, min(99, qty))


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def matches_usual(item_name: str, product_name: str) -> bool:
    """O produto começa pela 1.ª palavra do artigo e contém as restantes (singular/plural).

    "Arroz" -> "Arroz Vaporizado Cigala" sim; "Picado" -> "Alho Picado Continente" não.
    """
    words = [_stem(w) for w in norm(item_name).split() if len(w) >= 3]
    prod = [_stem(w) for w in norm(product_name).split()]
    if not words or not prod or not prod[0].startswith(words[0]):
        return False
    return all(any(p.startswith(w) for p in prod) for w in words[1:])


# Campos onde o Continente pode pôr o preço "normal" de um produto em promoção.
REGULAR_PRICE_KEYS = (
    "regularPriceEur", "previousPriceEur", "oldPriceEur", "originalPriceEur", "listPriceEur",
    "wasPriceEur", "priceBeforeDiscountEur", "fullPriceEur",
)


def _regular_price(p: dict[str, Any]) -> float | None:
    """Preço sem promoção, se o Continente o indicar e for maior do que o atual."""
    price = p.get("priceEur", p.get("price"))
    sources = [p] + [p[k] for k in ("promotion", "promo", "discount") if isinstance(p.get(k), dict)]
    for src in sources:
        for key in REGULAR_PRICE_KEYS:
            try:
                value = float(src[key])
            except (KeyError, TypeError, ValueError):
                continue
            if price is None or value > float(price):
                return value
    return None


def _product(p: dict[str, Any] | None) -> dict[str, Any] | None:
    if not p or not p.get("sku"):
        return None
    out = {
        "sku": str(p["sku"]),
        "name": (p.get("name") or "").replace("\u00a0", " ").strip(),
        "price": p.get("priceEur"),
        "available": p.get("available", True),
        "image": p.get("imageUrl"),
    }
    if (regular := _regular_price(p)) is not None:
        out["regular_price"] = regular
    return out


def product_from_details(data: dict[str, Any], sku: str) -> dict[str, Any] | None:
    """Converte a resposta de get_product no mesmo formato dos outros produtos."""
    if data.get("found") is False:
        return None
    prod = data.get("product") if isinstance(data.get("product"), dict) else data
    return _product(
        {
            **prod,
            "sku": prod.get("sku") or prod.get("productId") or sku,
            "priceEur": prod.get("priceEur", prod.get("price")),
            "available": prod.get("available", True),
        }
    )


class ListPricer:
    """Mantém os preços da lista de compras e as escolhas de produto."""

    def __init__(self, hass: HomeAssistant, api: ContinenteApi, entry_id: str) -> None:
        self.hass = hass
        self.api = api
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self.choices: dict[str, dict[str, Any]] = {}  # norm(nome) -> produto escolhido
        self._lookup: dict[str, tuple[float, dict[str, Any]]] = {}  # norm(nome) -> (quando, resultado)
        self._usuals: list[dict[str, Any]] = []
        self._usuals_at = 0.0
        self._lock = asyncio.Lock()
        self.result: dict[str, Any] = {"items": [], "not_found": [], "total": None}

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.choices = data.get("choices", {})

    async def _save(self) -> None:
        await self._store.async_save({"choices": self.choices})

    async def _usual_products(self) -> list[dict[str, Any]]:
        if time.monotonic() - self._usuals_at < USUALS_TTL and self._usuals_at:
            return self._usuals
        usuals: list[dict[str, Any]] = []
        for getter in (self.api.most_bought, self.api.favorites):
            try:
                usuals += [p for p in map(_product, await getter()) if p]
            except ContinenteError as err:
                _LOGGER.debug("Sem habituais de %s: %s", getter.__name__, err)
        self._usuals, self._usuals_at = usuals, time.monotonic()
        return usuals

    async def choose(self, item_name: str, sku: str) -> dict[str, Any]:
        """Guarda o produto escolhido para um nome de artigo (e lê o preço atual)."""
        chosen = product_from_details(await self.api.get_product(sku), sku)
        if chosen is None:
            raise ContinenteError(f"O produto {sku} não existe")
        self.choices[norm(item_name)] = chosen
        self._lookup.pop(norm(item_name), None)
        await self._save()
        return chosen

    async def forget(self, item_name: str) -> None:
        if self.choices.pop(norm(item_name), None) is not None:
            await self._save()

    async def refresh_choices(self, names: list[str]) -> None:
        """Lê outra vez o preço dos produtos escolhidos à mão (guardados com o preço da altura)."""
        changed = False
        for key in {norm(n) for n in names} & self.choices.keys():
            sku = self.choices[key]["sku"]
            try:
                fresh = product_from_details(await self.api.get_product(sku), sku)
            except ContinenteError as err:
                _LOGGER.debug("Sem preço novo para %s: %s", sku, err)
                continue
            if fresh:
                self.choices[key] = fresh
                changed = True
        if changed:
            await self._save()

    async def async_products(self, names: list[str]) -> dict[str, dict[str, Any] | None]:
        """Produto (com preço) de cada nome, pela mesma ordem de escolha da lista: norm(nome) -> produto."""
        async with self._lock:
            now = time.monotonic()
            out: dict[str, dict[str, Any] | None] = {}
            to_search: dict[str, str] = {}
            usuals = None
            for name in names:
                key = norm(name)
                if not key or key in out:
                    continue
                if key in self.choices:
                    out[key] = self.choices[key]
                elif key in self._lookup and now - self._lookup[key][0] < LOOKUP_TTL:
                    out[key] = self._lookup[key][1]["product"]
                else:
                    if usuals is None:
                        usuals = await self._usual_products()
                    hit = next((p for p in usuals if matches_usual(name, p["name"])), None)
                    if hit:
                        others = [p for p in usuals if p is not hit and matches_usual(name, p["name"])][:3]
                        self._lookup[key] = (now, {"product": hit, "alternatives": others, "source": "habitual"})
                        out[key] = hit
                    else:
                        to_search[key] = name
            if to_search:
                await self._search(to_search, now)
                for key in to_search:
                    out[key] = self._lookup[key][1]["product"] if key in self._lookup else None
            return out

    async def async_price(self, todo_items: list[dict[str, Any]]) -> dict[str, Any]:
        """Calcula o produto e o preço de cada artigo por comprar."""
        async with self._lock:
            return await self._price(todo_items)

    async def _price(self, todo_items: list[dict[str, Any]]) -> dict[str, Any]:
        now = time.monotonic()
        rows: list[dict[str, Any]] = []
        to_search: dict[str, str] = {}  # norm -> nome original
        usuals = None

        for it in todo_items:
            if it.get("status") != "needs_action":
                continue
            name, qty = parse_item(it.get("summary", ""))
            key = norm(name)
            row: dict[str, Any] = {
                "uid": it.get("uid"),
                "summary": it.get("summary"),
                "name": name,
                "qty": qty,
                "product": None,
                "alternatives": [],
                "source": None,
            }
            if key in self.choices:
                row["product"], row["source"] = self.choices[key], "escolhido"
            elif key in self._lookup and now - self._lookup[key][0] < LOOKUP_TTL:
                cached = self._lookup[key][1]
                row.update(product=cached["product"], alternatives=cached["alternatives"], source=cached["source"])
            else:
                if usuals is None:
                    usuals = await self._usual_products()
                hit = next((p for p in usuals if matches_usual(name, p["name"])), None)
                if hit:
                    others = [p for p in usuals if p is not hit and matches_usual(name, p["name"])][:3]
                    row.update(product=hit, alternatives=others, source="habitual")
                    self._lookup[key] = (now, {"product": hit, "alternatives": others, "source": "habitual"})
                else:
                    to_search[key] = name
            rows.append(row)

        if to_search:
            await self._search(to_search, now)
            for row in rows:
                key = norm(row["name"])
                if row["source"] is None and key in self._lookup:
                    cached = self._lookup[key][1]
                    row.update(product=cached["product"], alternatives=cached["alternatives"], source=cached["source"])

        total = 0.0
        not_found = []
        for row in rows:
            p = row["product"]
            if p and p.get("price") is not None:
                row["line_total"] = round(p["price"] * row["qty"], 2)
                if p.get("available", True):
                    total += row["line_total"]
            else:
                row["line_total"] = None
                not_found.append(row["name"])

        self.result = {"items": rows, "not_found": not_found, "total": round(total, 2)}
        return self.result

    async def _search(self, names: dict[str, str], now: float) -> None:
        """Pesquisa no catálogo os artigos sem produto, numa só chamada (pré-visualização)."""
        queries = list(names.values())
        by_query: dict[str, dict[str, Any]] = {}
        failed: set[str] = set()
        # O Continente aceita no máximo 20 artigos por chamada.
        for start in range(0, len(queries), 20):
            batch = queries[start : start + 20]
            try:
                data = await self.api.resolve_list([{"query": q[:80], "quantity": 1} for q in batch])
            except ContinenteError as err:
                _LOGGER.warning("Não consegui pesquisar %d artigos no Continente: %s", len(batch), err)
                failed.update(norm(q) for q in batch)
                continue
            by_query.update({norm(r.get("query", "")): r for r in data.get("resolved", [])})
        for key, name in names.items():
            if key in failed:
                continue  # tenta outra vez na próxima atualização em vez de guardar "não encontrado"
            r = by_query.get(norm(name))
            if r and r.get("selected"):
                entry = {
                    "product": _product(r["selected"]),
                    "alternatives": [p for p in map(_product, r.get("alternatives", [])) if p],
                    "source": "pesquisa",
                }
            else:
                entry = {"product": None, "alternatives": [], "source": "nao_encontrado"}
            self._lookup[key] = (now, entry)
