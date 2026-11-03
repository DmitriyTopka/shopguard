"""Arithmetic check of money amounts that are not catalog prices, through Otari code execution.

An honest reply often contains derived amounts: "18 999 UAH, that is 9 499.50 a month
in 2 payments", or the total for two products. The regex net catches every amount,
and the plain price check only knows catalog prices and policy thresholds, so such
an amount looked invented and an honest reply went to the rewrite or the hand-off.

Here Nemotron Super gets the shop facts and the sentence, writes Python and runs it
with Otari's built-in otari_code_execution tool. The amount is accepted only when the
gateway really ran the code and the reported value matches the quoted amount. A wrong
derivation (12 payments when the policy allows 2 to 3) stays a violation, because the
model may use only the payment counts the policy allows.
"""
from __future__ import annotations

import json
import os
import re

from .checks import OK, Verdict
from .claims import Claim
from .llm import LLM, MODEL_SMART, CallLog
from .shop import Shop

MAX_MATH_CHECKS = 2  # per reply, each one is a Super call with a code run (about $0.01)
CODE_TOOL = [{"type": "otari_code_execution"}]

MATH_SYSTEM = (
    "You check whether a money amount in a shop assistant's reply follows from the shop's facts. "
    "Use the code tool: write Python that computes candidate values only from the facts given "
    "(catalog prices, allowed numbers of installment payments, policy thresholds): a price split into "
    "an allowed number of payments, a sum of listed prices, the difference between two listed prices. "
    "Never use a number of payments the policy does not allow, never invent a price. "
    'Then answer with JSON only: {"derived": true or false, "formula": "...", "value": number}.'
)


def enabled() -> bool:
    return os.getenv("SHOPGUARD_CODE_CHECK", "on").lower() not in ("off", "0", "false")


def needs_math(v: Verdict) -> bool:
    """A regex-caught amount that matched no catalog price or threshold."""
    c = v.claim
    return (v.status != OK and c.type == "price" and c.source == "regex"
            and c.value.get("amount") is not None and "does not appear anywhere" in v.reason)


def _context(reply: str, text: str, width: int = 160) -> str:
    i = reply.find(text)
    if i < 0:
        return reply[:2 * width]
    return reply[max(0, i - width):i + len(text) + width]


def _facts(shop: Shop) -> str:
    inst = shop.policy.get("installment", {})
    lo, hi = inst.get("min_payments", 1), inst.get("max_payments", 1)
    return (f"Catalog ({shop.currency}):\n{shop.catalog_text()}\n"
            f"Allowed installment payments: {', '.join(str(n) for n in range(lo, hi + 1))}.\n"
            f"Policy thresholds: {sorted(shop.allowed_prices() - {p.price for p in shop.products})}.")


def _bad_split(formula: str, shop: Shop) -> bool:
    """A split into a number of payments the policy does not allow (15999 / 12) is not a valid derivation."""
    inst = shop.policy.get("installment", {})
    allowed = set(range(inst.get("min_payments", 1), inst.get("max_payments", 1) + 1))
    return any(1 < int(n) <= 60 and int(n) not in allowed for n in re.findall(r"/\s*(\d+)(?![\d.])", formula))


def _parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}


def math_check(v: Verdict, reply: str, shop: Shop, llm: LLM, log: CallLog) -> Verdict:
    c: Claim = v.claim
    amount = float(c.value["amount"])
    res = llm.complete(
        [{"role": "system", "content": MATH_SYSTEM},
         {"role": "user", "content": f"{_facts(shop)}\n\nReply fragment:\n{_context(reply, c.text)}\n\n"
                                     f"Amount to check: {c.text} ({amount:g})."}],
        model=MODEL_SMART, task="math_check", max_tokens=400, tools=CODE_TOOL,
    )
    log.calls.append(res)
    ans = _parse(res.text)
    try:
        value = float(ans.get("value"))
    except (TypeError, ValueError):
        value = None
    ran = bool(res.code_runs)
    formula = str(ans.get("formula", "")).strip()[:120]
    if ran and ans.get("derived") is True and value is not None and abs(value - amount) <= 1 \
            and not _bad_split(formula, shop):
        return Verdict(c, OK, f"Derived amount: {formula} (computed with Otari code execution).",
                       source="code")
    return v
