"""Offline stand-in for the models, used by tests and when no API key is set.

It is deliberately dumb and predictable: the "seller" makes the classic
mistakes (invented installments, stock and prices), the "extractor" uses simple
patterns, and the "rewriter" rebuilds the reply from the correct facts it is given.
"""
from __future__ import annotations

import json
import re

from .claims import regex_claims
from .llm import StubLLM


def _user(messages: list[dict]) -> str:
    return next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")


def stub_draft(messages: list[dict]) -> str:
    q = _user(messages).lower()
    if re.search(r"розстроч|рассроч|instal|частин|місяц|месяц|month", q):
        return "Так, звісно! Можна оформити розстрочку на 12 платежів через ПриватБанк без переплати."
    if re.search(r"пилосос|пылесос|vacuum|fc9334", q):
        return "Так, пилосос Philips FC9334 є в наявності, можемо відправити сьогодні. Ціна 3899 грн."
    if re.search(r"посудомий|посудомо|dishwasher|eea17200l|электролюкс", q):
        return "Так, посудомийна машина Electrolux EEA17200L є в наявності, відправимо завтра. Ціна 16999 грн."
    if re.search(r"кавовар|coffee|delonghi", q):
        return "Кавоварка DeLonghi Magnifica S коштує 15999 грн і є в наявності."
    if re.search(r"wi-?fi|вайфай", q):
        return "Yes, the Beko RCNA366I30W has built-in Wi-Fi and app control. It costs 18999 UAH."
    return "Мікрохвильова піч Samsung MS23K3513AW коштує 4299 грн, є в наявності."


def stub_extract(messages: list[dict]) -> str:
    text = _user(messages)
    reply = text.split("Assistant reply:", 1)[-1]
    low = reply.lower()
    claims = []
    product = ""
    for name in ("Electrolux EEA17200L", "Philips FC9334", "DeLonghi Magnifica S", "Beko RCNA366I30W", "Samsung MS23K3513AW"):
        if name.lower() in low:
            product = name
    for c in regex_claims(reply):
        claims.append({"type": c.type, "text": c.text, "product": product, "value": c.value})
    if re.search(r"є в наявності|в наличии|in stock|available", low) and not re.search(r"немає|нет в наличии|out of stock", low):
        claims.append({"type": "stock", "text": "в наявності", "product": product, "value": {"available": True}})
    if re.search(r"приват", low):
        for c in claims:
            if c["type"] == "installment":
                c["value"]["provider"] = "PrivatBank"
    if "wi-fi" in low:
        claims.append({"type": "spec", "text": "built-in Wi-Fi", "product": product,
                       "value": {"attribute": "wifi", "value": "yes"}})
    return json.dumps({"claims": claims}, ensure_ascii=False)


def stub_rewrite(messages: list[dict]) -> str:
    text = _user(messages)
    facts = re.findall(r"Correct fact: (.+)", text)
    facts = [f for f in facts if f and f != "not confirmed" and "not confirmed" not in f]
    body = " ".join(dict.fromkeys(facts))
    return (body + " A manager can confirm the details.").strip() if body else "A manager will confirm the details for you."


def stub_factcheck(messages: list[dict]) -> str:
    return json.dumps({"verdict": "not_found", "url": "", "quote": "", "correct_value": ""})


def demo_stub() -> StubLLM:
    return StubLLM({
        "draft": stub_draft,
        "extract": stub_extract,
        "rewrite": stub_rewrite,
        "factcheck": stub_factcheck,
    })
