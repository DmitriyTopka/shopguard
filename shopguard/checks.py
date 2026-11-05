"""Deterministic checks of claims against the catalog and the policy.

No model is involved here on purpose: prices, stock and installment terms are
facts the shop already has, so code checks them exactly and for free.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, asdict

from .claims import Claim
from .shop import Shop

OK, VIOLATION, UNVERIFIED = "ok", "violation", "unverified"


@dataclass
class Verdict:
    claim: Claim
    status: str
    reason: str
    truth: str = ""      # what the shop data says, used by the rewriter
    source: str = "catalog"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["claim"] = self.claim.to_dict()
        return d


# the noun only: "guaranteed Wi-Fi" or "гарантовано доставимо" is not a warranty claim
WARRANTY_RX = re.compile(r"\bwarrant(y|ies)\b|гаранті(я|ю|ї|єю)|гаранти(я|ю|и|ей|ию)\b", re.IGNORECASE)
# a claim that joins the warranty to something else ("Wi-Fi and a 12-month warranty") is not a policy fact alone
JOINED_RX = re.compile(r",|;|\+|&|\b(and|plus|also)\b|(?<!\w)(і|й|та|и|также|також)(?!\w)", re.IGNORECASE)
TERM_RX = re.compile(r"(\d+)\s*-?\s*(months?|місяц\w*|міс\.?|месяц\w*|мес\.?|years?|рок\w*|рік|год\w*|лет)", re.IGNORECASE)
YEAR_UNITS = ("year", "рок", "рік", "год", "лет")
EXTRA_WARRANTY_RX = re.compile(
    r"lifetime|extended|forever|free repair|replacement|довічн|пожизн|розширен|расширен|безкоштовн\w* ремонт|"
    r"бесплатн\w* ремонт|заміна|замена|продовжен|продлен", re.IGNORECASE)


def policy_spec(c: Claim, shop: Shop) -> Verdict | None:
    """A "characteristic" that is really a shop rule: the warranty. Checked against policy.json, no web call.

    Found 05.11: the MCP rewrite read get_policy("warranty") and wrote "12-month manufacturer's warranty";
    as a spec it went unconfirmed in the re-check (no web there) and honest replies were handed off.
    """
    pol = shop.policy.get("warranty")
    if c.type != "spec" or not pol or not WARRANTY_RX.search(c.text) or JOINED_RX.search(c.text):
        return None  # not a plain warranty claim: it goes to the usual spec check
    months = pol.get("months")
    bad = Verdict(c, VIOLATION, f"Warranty term not in the shop policy ({months} months).", truth=pol["summary"],
                  source="policy")
    terms = [(int(n), u.lower()) for n, u in TERM_RX.findall(c.text)]
    bare_numbers = len(re.findall(r"\d+", c.text)) != len(terms)
    if EXTRA_WARRANTY_RX.search(c.text) or bare_numbers:
        return bad  # lifetime, extended, free repair, or a number we cannot read as a term: fail closed
    said = {n * 12 if u.startswith(YEAR_UNITS) else n for n, u in terms}
    if said and said != {months}:
        return bad
    return Verdict(c, OK, f"Warranty matches the shop policy: {pol['summary']}", source="policy")


def check_claim(c: Claim, shop: Shop) -> Verdict:
    product = shop.by_sku(c.sku) if c.sku else None
    if c.type == "price":
        return _price(c, product, shop)
    if c.type == "stock":
        return _stock(c, product)
    if c.type == "installment":
        return _installment(c, product, shop)
    if c.type == "delivery":
        return _delivery(c, shop)
    return Verdict(c, UNVERIFIED, "Product characteristic, needs a web fact-check.", source="web")


def _price(c: Claim, product, shop: Shop) -> Verdict:
    amount = c.value.get("amount")
    if amount is None:
        return Verdict(c, UNVERIFIED, "Price mentioned without a number.")
    amount = int(round(float(amount)))
    thresholds = shop.allowed_prices() - {p.price for p in shop.products}
    if amount in thresholds:
        return Verdict(c, OK, f"{amount} {shop.currency} is a policy threshold.", source="policy")
    if c.source == "regex" or not product:
        if product is None and c.source != "regex":
            return Verdict(c, VIOLATION, "Price quoted for a product that is not in the catalog.",
                           truth="This product is not in our catalog, so we have no confirmed price for it.")
        if amount in shop.allowed_prices():
            return Verdict(c, OK, "Amount matches a catalog price or a policy threshold.")
        split = _installment_split(float(c.value["amount"]), product, shop)
        if split:
            p, n = split
            return Verdict(c, OK, f"One of {n} installment payments: {p.price} / {n} for {p.name} "
                                  f"(the policy allows this split).", source="policy")
        return Verdict(c, VIOLATION, f"{amount} {shop.currency} does not appear anywhere in the catalog or policy.",
                       truth="This amount is not a price from our catalog.")
    if amount == product.price:
        return Verdict(c, OK, f"{product.name}: {product.price} {shop.currency}.")
    return Verdict(c, VIOLATION, f"Quoted {amount}, catalog says {product.price} {shop.currency}.",
                   truth=f"{product.name} costs {product.price} {shop.currency}.")


def _installment_split(raw: float, product, shop: Shop):
    """(product, n) when the amount is a catalog price split into a number of payments the policy allows.

    Plain arithmetic over the shop's data, the same rule as the shop-data MCP tool installment_plan.
    A product the reply names is tried first; other derived amounts (sums, differences) go to the
    code-execution check in mathcheck.py.
    """
    inst = shop.policy.get("installment", {})
    counts = range(inst.get("min_payments", 1), inst.get("max_payments", 1) + 1)
    floor = inst.get("min_order_uah", 0)
    candidates = [product] if product else []
    candidates += [p for p in shop.products if p is not product]
    for p in candidates:
        for n in counts:
            if n > 1 and p.price >= floor and abs(raw - p.price / n) <= 1:
                return p, n
    return None


def _stock(c: Claim, product) -> Verdict:
    says_available = bool(c.value.get("available"))
    if product is None:
        if says_available:
            return Verdict(c, VIOLATION, "Availability promised for a product that is not in the catalog.",
                           truth="This product is not in the catalog.")
        return Verdict(c, OK, "Says not available; product is not in the catalog.")
    if says_available and not product.in_stock:
        return Verdict(c, VIOLATION, f"{product.name} is out of stock.", truth=f"{product.name} is OUT OF STOCK.")
    if not says_available and product.in_stock:
        return Verdict(c, VIOLATION, f"{product.name} is actually in stock.", truth=f"{product.name} is in stock.")
    return Verdict(c, OK, f"{product.name}: {'in stock' if product.in_stock else 'out of stock'}.")


def _installment(c: Claim, product, shop: Shop) -> Verdict:
    pol = shop.policy.get("installment", {})
    lo, hi = pol.get("min_payments", 1), pol.get("max_payments", 1)
    providers = [p.lower() for p in pol.get("providers", [])]
    truth = pol.get("summary", "")
    n = c.value.get("payments") or c.value.get("months")
    if n is not None:
        n = int(n)
        if n > hi or n < lo:
            return Verdict(c, VIOLATION, f"{n} payments promised; policy allows {lo} to {hi}.", truth=truth, source="policy")
    prov = (c.value.get("provider") or "").lower()
    if prov and not any(prov in p or p.split()[0] in prov for p in providers):
        return Verdict(c, VIOLATION, f"Provider '{c.value.get('provider')}' is not offered.", truth=truth, source="policy")
    min_order = pol.get("min_order_uah")
    if product and min_order and product.price < min_order:
        return Verdict(c, VIOLATION, f"{product.name} is below the {min_order} {shop.currency} installment minimum.",
                       truth=truth, source="policy")
    return Verdict(c, OK, "Within installment policy.", source="policy")


def _delivery(c: Claim, shop: Shop) -> Verdict:
    pol = shop.policy.get("delivery", {})
    truth = pol.get("summary", "")
    if c.value.get("same_day") and not pol.get("same_day", False):
        return Verdict(c, VIOLATION, "Same-day dispatch promised; policy has none.", truth=truth, source="policy")
    days = c.value.get("days")
    if days is not None and int(days) < pol.get("min_days", 0):
        return Verdict(c, VIOLATION, f"{days}-day delivery promised; policy minimum is {pol.get('min_days')}.",
                       truth=truth, source="policy")
    carrier = (c.value.get("carrier") or "").lower()
    carriers = [x.lower() for x in pol.get("carriers", []) + pol.get("carrier_aliases", [])]
    if carrier and carriers and not any(x in carrier for x in carriers):
        return Verdict(c, VIOLATION, f"Carrier '{c.value.get('carrier')}' is not used.", truth=truth, source="policy")
    return Verdict(c, OK, "Within delivery policy.", source="policy")
