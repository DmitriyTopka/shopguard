"""Shop data: catalog and policy, plus product lookup by free-text mention."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class Product:
    sku: str
    name: str
    price: int
    in_stock: bool
    aliases: list[str] = field(default_factory=list)
    specs: dict = field(default_factory=dict)


@dataclass
class Shop:
    name: str
    currency: str
    products: list[Product]
    policy: dict

    @classmethod
    def load(cls, catalog_path: Path | None = None, policy_path: Path | None = None) -> "Shop":
        cat = json.loads((catalog_path or DATA_DIR / "catalog.json").read_text(encoding="utf-8"))
        pol = json.loads((policy_path or DATA_DIR / "policy.json").read_text(encoding="utf-8"))
        return cls.from_dicts(cat, pol)

    @classmethod
    def from_dicts(cls, cat: dict, pol: dict) -> "Shop":
        products = [
            Product(
                sku=p["sku"], name=p["name"], price=int(p["price"]), in_stock=bool(p["in_stock"]),
                aliases=[a.lower() for a in p.get("aliases", [])], specs=p.get("specs", {}),
            )
            for p in cat["products"]
        ]
        return cls(name=cat.get("shop", "Shop"), currency=cat.get("currency", "UAH"), products=products, policy=pol)

    def by_sku(self, sku: str) -> Product | None:
        sku = (sku or "").strip().upper()
        return next((p for p in self.products if p.sku == sku), None)

    def find(self, mention: str) -> Product | None:
        """Best catalog match for a free-text product mention, or None.

        Model codes (e.g. "FC9334") win over generic words (e.g. "vacuum"),
        so a mention of a different model of the same kind does not match.
        """
        if not mention:
            return None
        text = mention.lower()
        by_sku = self.by_sku(mention)
        if by_sku:
            return by_sku
        # model codes mix latin letters and digits ("s10", "fc9334"); bare numbers like "10 months" are not codes
        codes = set(re.findall(r"\b(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]{3,}\b", text))
        best, best_len = None, 0
        for p in self.products:
            model_code = p.name.split()[1].lower() if len(p.name.split()) > 1 else ""
            if model_code and model_code in text:
                return p
            for a in p.aliases:
                if a in text and len(a) > best_len:
                    # a generic alias must not match if the mention carries a different model code
                    alias_has_code = bool(re.search(r"\d", a))
                    if not alias_has_code and codes and not any(c in p.name.lower() for c in codes):
                        continue
                    best, best_len = p, len(a)
        return best

    def catalog_text(self) -> str:
        lines = [
            f"{p.sku} | {p.name} | {p.price} {self.currency} | {'in stock' if p.in_stock else 'OUT OF STOCK'}"
            for p in self.products
        ]
        return "\n".join(lines)

    def policy_text(self) -> str:
        parts = [self.policy.get("language", "")]
        for key in ("installment", "delivery", "warranty"):
            if key in self.policy:
                parts.append(f"{key.title()}: {self.policy[key]['summary']}")
        parts.append(self.policy.get("fallback", ""))
        return "\n".join(x for x in parts if x)

    def allowed_prices(self) -> set[int]:
        prices = {p.price for p in self.products}
        inst = self.policy.get("installment", {})
        dlv = self.policy.get("delivery", {})
        for v in (inst.get("min_order_uah"), dlv.get("free_from_uah")):
            if v:
                prices.add(int(v))
        return prices
