"""LLM access: Nebius Token Factory (OpenAI-compatible) and an offline stub.

Every call carries a `task` tag ("draft", "extract", "judge", "rewrite") so that
cost can be attributed per stage and the stub can answer deterministically.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

# Model ids and prices are configurable: verify both in the Token Factory console.
MODEL_FAST = os.getenv("SHOPGUARD_MODEL_FAST", "nvidia/Nemotron-3-Nano-30B-A3B")
MODEL_SMART = os.getenv("SHOPGUARD_MODEL_SMART", "nvidia/Nemotron-3-Super-120B-A12B")

# USD per 1M tokens (input, output). Defaults are aggregator figures, override from the console.
PRICES = {
    MODEL_FAST: (float(os.getenv("SHOPGUARD_PRICE_FAST_IN", "0.06")), float(os.getenv("SHOPGUARD_PRICE_FAST_OUT", "0.24"))),
    MODEL_SMART: (float(os.getenv("SHOPGUARD_PRICE_SMART_IN", "0.30")), float(os.getenv("SHOPGUARD_PRICE_SMART_OUT", "0.90"))),
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
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 60.0):
        self.api_key = api_key or os.environ["NEBIUS_API_KEY"]
        self.base_url = (base_url or os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")).rstrip("/")
        self.http = httpx.Client(timeout=timeout)

    def complete(self, messages, *, model, task, max_tokens=512, temperature=0.0, json_mode=False):
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        r = self.http.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
        )
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


def default_llm() -> LLM:
    if os.getenv("NEBIUS_API_KEY"):
        return TokenFactoryLLM()
    from .stub import demo_stub
    return demo_stub()
