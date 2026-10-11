"""Web fact-check of product characteristics: Tavily search + Nemotron Super verdict.

A spec claim ("this fridge has Wi-Fi") is not in the shop's data, so we look it
up on the open web, then ask the stronger model whether the found text supports
the claim. Results are cached by query to keep within the free Tavily quota.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

import httpx

from .checks import OK, UNVERIFIED, VIOLATION, Verdict
from .claims import Claim
from .llm import LLM, MODEL_SMART, CallLog
from .shop import Shop


@dataclass
class SearchHit:
    title: str
    url: str
    content: str


class Searcher(Protocol):
    def search(self, query: str) -> list[SearchHit]: ...


class TavilySearcher:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0, cache=None):
        self.api_key = api_key or os.environ["TAVILY_API_KEY"]
        self.http = httpx.Client(timeout=timeout)
        self.cache = cache  # object with get(key)->str|None and put(key, str)
        self.calls = 0

    def search(self, query: str) -> list[SearchHit]:
        cached = self.cache.get(query) if self.cache else None
        if cached is not None:
            raw = json.loads(cached)
        else:
            r = self.http.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"query": query, "search_depth": "basic", "max_results": 5},
            )
            r.raise_for_status()
            raw = r.json().get("results", [])
            self.calls += 1
            if self.cache:
                self.cache.put(query, json.dumps(raw))
        return [SearchHit(h.get("title", ""), h.get("url", ""), (h.get("content") or "")[:700]) for h in raw]


class StubSearcher:
    def __init__(self, pages: dict[str, list[SearchHit]] | None = None):
        self.pages = pages or {}
        self.calls = 0

    def search(self, query: str) -> list[SearchHit]:
        self.calls += 1
        for key, hits in self.pages.items():
            if key.lower() in query.lower():
                return hits
        return []


JUDGE_SYSTEM = """You verify one product claim against web search results.
Answer JSON only: {"verdict": "supported"|"contradicted"|"not_found", "url": "<best source url or empty>", "quote": "<short supporting or contradicting quote>", "correct_value": "<what the source says, or empty>"}
"supported" only if a result clearly states the claim for this exact model. If results are about another model or vague, answer "not_found"."""


def fact_check(c: Claim, shop: Shop, searcher: Searcher | None, llm: LLM, log: CallLog) -> Verdict:
    product = shop.by_sku(c.sku) if c.sku else None
    name = product.name if product else (c.product or "")
    attr, val = c.value.get("attribute", ""), c.value.get("value", "")
    claim_text = f"{name}: {attr} = {val}" if attr else f"{name}: {c.text}"
    # catalog specs win over the web when the shop has them
    if product and attr and attr in product.specs:
        truth = str(product.specs[attr])
        ok = truth.lower() == str(val).lower()
        return Verdict(c, OK if ok else VIOLATION, f"Catalog spec {attr} = {truth}.", truth=f"{name}: {attr} is {truth}.")
    if searcher is None:
        return Verdict(c, UNVERIFIED, "Web check is off; characteristic not confirmed.",
                       truth="Do not state characteristics that are not confirmed.", source="web")
    query = f"{name} {attr or c.text} specifications".strip()
    hits = searcher.search(query)
    if not hits:
        return Verdict(c, UNVERIFIED, "No web sources found.", truth="Characteristic not confirmed.", source="web")
    sources = "\n\n".join(f"[{i + 1}] {h.title}\n{h.url}\n{h.content}" for i, h in enumerate(hits))
    res = llm.complete(
        [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": f"Claim: {claim_text}\n\nSearch results:\n{sources}"},
        ],
        model=MODEL_SMART, task="factcheck", max_tokens=400, json_mode=True,
    )
    log.calls.append(res)
    data = _json_obj(res.text)
    verdict = data.get("verdict", "not_found")
    url = data.get("url", "") or hits[0].url
    quote = data.get("quote", "")
    if verdict == "supported":
        return Verdict(c, OK, f"Confirmed: {quote}", truth=claim_text, source=url)
    if verdict == "contradicted":
        correct = data.get("correct_value", "")
        return Verdict(c, VIOLATION, f"Source says otherwise: {quote}",
                       truth=f"{name}: {attr} is {correct}" if correct else "Characteristic contradicted by the manufacturer.",
                       source=url)
    return Verdict(c, UNVERIFIED, "Sources do not confirm it.", truth="Characteristic not confirmed.", source=url)


def _json_obj(text: str) -> dict:
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e < 0:
        return {}
    try:
        d = json.loads(text[s:e + 1])
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}
