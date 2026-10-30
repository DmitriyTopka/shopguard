"""LLM access: Otari gateway, Nebius Token Factory (both OpenAI-compatible) and an offline stub.

Every call carries a `task` tag ("draft", "extract", "judge", "rewrite") so that
cost can be attributed per stage and the stub can answer deterministically.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

# Model ids and prices are configurable. Defaults are the Otari catalog (GET /api/v1/models, 30.10.2026);
# the Nano id with response_format support is `nemotron-nano-3-30b`, not `nemotron-3-nano-30b-a3b`.
MODEL_FAST = os.getenv("SHOPGUARD_MODEL_FAST", "nvidia/nemotron-nano-3-30b")
MODEL_SMART = os.getenv("SHOPGUARD_MODEL_SMART", "nvidia/nemotron-3-super-120b-a12b")
# The demo shop bot that ShopGuard checks. Not part of the guard; Nano's Ukrainian was broken
# ("частkami", "замовžiти") in the first live run, Gemma 3 27B writes it cleanly.
MODEL_SELLER = os.getenv("SHOPGUARD_MODEL_SELLER", "google/gemma-3-27b-it")

# USD per 1M tokens (input, output), from the same catalog.
PRICES = {
    MODEL_FAST: (float(os.getenv("SHOPGUARD_PRICE_FAST_IN", "0.06")), float(os.getenv("SHOPGUARD_PRICE_FAST_OUT", "0.24"))),
    MODEL_SMART: (float(os.getenv("SHOPGUARD_PRICE_SMART_IN", "0.15")), float(os.getenv("SHOPGUARD_PRICE_SMART_OUT", "0.65"))),
    MODEL_SELLER: (float(os.getenv("SHOPGUARD_PRICE_SELLER_IN", "0.10")), float(os.getenv("SHOPGUARD_PRICE_SELLER_OUT", "0.30"))),
}


@dataclass
class LLMResult:
    text: str
    model: str
    task: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICES.get(self.model, (0.0, 0.0))
        return (self.prompt_tokens * pin + self.completion_tokens * pout) / 1_000_000


@dataclass
class CallLog:
    calls: list[LLMResult] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


class LLM:
    def complete(self, messages: list[dict], *, model: str, task: str, max_tokens: int = 512,
                 temperature: float = 0.0, json_mode: bool = False) -> LLMResult:
        raise NotImplementedError


class TokenFactoryLLM(LLM):
    KEY_ENV, BASE_ENV, BASE_DEFAULT = "NEBIUS_API_KEY", "NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 60.0,
                 http: httpx.Client | None = None):
        self.api_key = api_key or os.environ[self.KEY_ENV]
        self.base_url = (base_url or os.getenv(self.BASE_ENV, self.BASE_DEFAULT)).rstrip("/")
        self.http = http or httpx.Client(timeout=timeout)
        self.no_json_mode: set[str] = set()  # models whose provider rejected response_format

    def _post(self, body: dict) -> httpx.Response:
        return self.http.post(f"{self.base_url}/chat/completions",
                              headers={"Authorization": f"Bearer {self.api_key}"}, json=body)

    def complete(self, messages, *, model, task, max_tokens=512, temperature=0.0, json_mode=False):
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if json_mode and model not in self.no_json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        r = self._post(body)
        if r.status_code == 400 and "response_format" in body and "response_format" in r.text:
            # Otari lists Nemotron Nano with structured_output, but its Bedrock offering returns
            # 400 "'response_format' is not supported for bedrock". The prompt already asks for
            # JSON and parse_llm_json tolerates prose, so retry once without it (400s are not billed).
            self.no_json_mode.add(model)
            del body["response_format"]
            r = self._post(body)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        return LLMResult(
            text=strip_reasoning(data["choices"][0]["message"].get("content") or ""),
            model=model, task=task,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=int((time.monotonic() - t0) * 1000),
        )


class OtariLLM(TokenFactoryLLM):
    """Mozilla.ai Otari hosted gateway: same OpenAI-compatible wire format, workspace token `otk_...`.

    Model ids are `provider/model` as listed in the workspace Models tab; set them in
    SHOPGUARD_MODEL_FAST / SHOPGUARD_MODEL_SMART.
    """

    KEY_ENV, BASE_ENV, BASE_DEFAULT = "OTARI_API_KEY", "OTARI_BASE_URL", "https://api.otari.ai/api/v1"


def strip_reasoning(text: str) -> str:
    """Drop a <think>...</think> block if the model returns its reasoning inline."""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


class StubLLM(LLM):
    """Offline model. `handlers[task](messages) -> str`; unknown tasks return ''."""

    def __init__(self, handlers: dict[str, Callable[[list[dict]], str]] | None = None):
        self.handlers = handlers or {}
        self.calls: list[tuple[str, list[dict]]] = []

    def complete(self, messages, *, model, task, max_tokens=512, temperature=0.0, json_mode=False):
        self.calls.append((task, messages))
        fn = self.handlers.get(task)
        text = fn(messages) if fn else ""
        return LLMResult(text=text, model=model, task=task)


def live_llm() -> TokenFactoryLLM | None:
    """The live gateway configured in env: Otari first, Nebius as the legacy path."""
    if os.getenv("OTARI_API_KEY"):
        return OtariLLM()
    if os.getenv("NEBIUS_API_KEY"):
        return TokenFactoryLLM()
    return None


def default_llm() -> LLM:
    live = live_llm()
    if live:
        return live
    from .stub import demo_stub
    return demo_stub()
