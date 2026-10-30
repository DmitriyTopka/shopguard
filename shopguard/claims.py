"""Turning a draft reply into a list of checkable promises.

Two extractors run together:
- the LLM extractor (Nemotron Nano, JSON) understands paraphrase and mixed languages;
- a regex safety net catches any money amount or payment count the LLM missed,
  so a price can never slip through just because extraction failed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict

from .llm import LLM, MODEL_FAST, CallLog
from .shop import Shop

CLAIM_TYPES = ("price", "stock", "installment", "delivery", "spec")


@dataclass
class Claim:
    type: str
    text: str
    product: str = ""
    sku: str = ""
    value: dict = field(default_factory=dict)
    source: str = "llm"  # llm | regex

    def to_dict(self) -> dict:
        return asdict(self)


EXTRACT_SYSTEM = """You extract factual promises from a shop assistant's reply.
Return JSON: {"claims": [ ... ]}. One item per promise. Item fields:
- "type": one of "price", "stock", "installment", "delivery", "spec"
- "text": the exact words from the reply that make the promise
- "product": the product as named in the reply ("" if none)
- "value": object, depending on type:
    price: {"amount": number}
    stock: {"available": true|false}
    installment: {"payments": integer or null, "months": integer or null, "provider": string or ""}
    delivery: {"days": integer or null, "same_day": true|false, "carrier": string or ""}
    spec: {"attribute": string, "value": string}
Only promises the reply actually makes. Refusals, questions and "a manager will confirm" are not promises.
The reply may be in English, Ukrainian, Russian or a mix. Output JSON only."""

MONEY_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:грн|грив|uah|₴|hryvn|hrn)",
    re.IGNORECASE,
)
PAYMENTS_RE = re.compile(
    r"(\d{1,2})\s*(?:-?х\s*)?(платеж\w*|платіж\w*|payments?|instal?lments?|місяц\w*|месяц\w*|months?|міс\b|мес\b)",
    re.IGNORECASE,
)


def parse_amount(s: str) -> int:
    return int(re.sub(r"[   ]", "", s))


# "10 payments are not available", "на 10 місяців ми не надаємо": a refusal repeats the customer's
# number without promising it. Only a negator within a few words of the term counts, and
# reassurances ("no problem", "не хвилюйтесь", "без переплати") are ignored, so a promise is never
# dropped because of an unrelated "no" in the same sentence.
NEGATION_RE = re.compile(
    r"(?<!\w)(?:не|ні|нет|немає|нема|відсутн\w*|отсутств\w*|not|no|cannot|can't|isn't|unavailable)(?!\w)",
    re.IGNORECASE,
)
REASSURANCE_RE = re.compile(
    r"no problem|не проблема|не хвилюйте\w*|не переживайте|не волнуйтесь|без переплат\w*|"
    r"переплат\w* (?:немає|нет|не буде|не будет)|(?:немає|нет) переплат\w*",
    re.IGNORECASE,
)
NEAR_WORDS = 4


def _refused(text: str, start: int, end: int) -> bool:
    left = max(text.rfind(ch, 0, start) for ch in ".!?\n")
    rights = [i for i in (text.find(ch, end) for ch in ".!?\n") if i >= 0]
    before = REASSURANCE_RE.sub(" ", text[left + 1:start]).split()[-NEAR_WORDS:]
    after = REASSURANCE_RE.sub(" ", text[end:min(rights) if rights else len(text)]).split()[:NEAR_WORDS]
    return bool(NEGATION_RE.search(" ".join(before + after)))


def regex_claims(reply: str) -> list[Claim]:
    out: list[Claim] = []
    for m in MONEY_RE.finditer(reply):
        out.append(Claim(type="price", text=m.group(0), value={"amount": parse_amount(m.group(1))}, source="regex"))
    for m in PAYMENTS_RE.finditer(reply):
        window = reply[max(0, m.start() - 30): m.end() + 30].lower()
        if re.search(r"гарант|warrant", window):
            continue  # "12 months warranty" is not an installment plan
        if _refused(reply, m.start(), m.end()):
            continue  # the reply refuses this term
        n = int(m.group(1))
        unit = m.group(2).lower()
        months = unit.startswith(("міс", "мес", "month"))
        out.append(Claim(
            type="installment", text=m.group(0),
            value={"payments": None if months else n, "months": n if months else None, "provider": ""},
            source="regex",
        ))
    return out


def parse_llm_json(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    items = data.get("claims", []) if isinstance(data, dict) else []
    return [i for i in items if isinstance(i, dict) and i.get("type") in CLAIM_TYPES]


def extract_claims(reply: str, customer_msg: str, shop: Shop, llm: LLM, log: CallLog) -> list[Claim]:
    res = llm.complete(
        [
            {"role": "system", "content": EXTRACT_SYSTEM},
            {"role": "user", "content": f"Customer: {customer_msg}\n\nAssistant reply:\n{reply}"},
        ],
        model=MODEL_FAST, task="extract", max_tokens=600, json_mode=True,
    )
    log.calls.append(res)
    claims = [
        Claim(type=i["type"], text=str(i.get("text", "")), product=str(i.get("product", "")),
              value=i.get("value") if isinstance(i.get("value"), dict) else {})
        for i in parse_llm_json(res.text)
    ]
    # attach catalog SKUs; if the reply names no product, fall back to the customer's message
    for c in claims:
        p = shop.find(c.product) or shop.find(c.text) or shop.find(customer_msg)
        c.sku = p.sku if p else ""
    # regex safety net: add what the LLM did not report
    seen_amounts = {c.value.get("amount") for c in claims if c.type == "price"}
    seen_counts = {(c.value.get("payments"), c.value.get("months")) for c in claims if c.type == "installment"}
    for rc in regex_claims(reply):
        if rc.type == "price" and rc.value["amount"] in seen_amounts:
            continue
        if rc.type == "installment" and (rc.value["payments"], rc.value["months"]) in seen_counts:
            continue
        p = shop.find(customer_msg)
        rc.sku = p.sku if p else ""
        claims.append(rc)
    return claims
