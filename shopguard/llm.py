"""LLM access: the Otari gateway (OpenAI-compatible) and an offline stub.

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

# Otari routing policies (Routing > Policies on otari.ai). Each role calls a policy, not a model, so a
# provider outage is absorbed by Otari: shopguard-seller (Gemma 3 27B on Nebius, then Llama 3.3 70B),
# shopguard-extract (Nano, then Super), shopguard-judge (Super on Bedrock, then gpt-oss-120b).
# shopguard-drill starts on a model that is listed but not deployed, for the demo's outage drill.
ROUTES = {
    MODEL_SELLER: os.getenv("SHOPGUARD_ROUTE_SELLER", "shopguard-seller"),
    MODEL_FAST: os.getenv("SHOPGUARD_ROUTE_FAST", "shopguard-extract"),
    MODEL_SMART: os.getenv("SHOPGUARD_ROUTE_SMART", "shopguard-judge"),
}
TOOL_ROUNDS = int(os.getenv("SHOPGUARD_TOOL_ROUNDS", "3"))
MCP_ROUNDS = int(os.getenv("SHOPGUARD_MCP_ROUNDS", "6"))
ROUTE_DRILL = os.getenv("SHOPGUARD_ROUTE_DRILL", "shopguard-drill")


def routing_enabled() -> bool:
    return os.getenv("SHOPGUARD_ROUTING", "on").lower() not in ("off", "0", "false")


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
    billed_usd: float | None = None  # what the gateway charged (Otari: usage.cost_usd, tools included)
    sources: list[str] = field(default_factory=list)  # URLs from gateway-run web searches
    provider: str = ""  # Otari-Provider: which provider actually served the call
    gateway_fallback: bool = False  # Otari-Fallback: the gateway moved to another offering of the model
    note: str = ""  # e.g. why ShopGuard moved this task to the bigger model
    code_runs: list[dict] = field(default_factory=list)  # gateway-run otari_code_execution calls: code, stdout
    route: str = ""  # the Otari routing policy the call named, if any
    served_model: str = ""  # Otari-Model: the model that answered
    attempts: int = 1  # Otari-Attempt-Count: candidates tried, the one that served included
    guardrails: list[dict] = field(default_factory=list)  # Otari-Guardrails: verdicts of request-level checks
    mcp_calls: list[dict] = field(default_factory=list)  # gateway-run MCP tool calls: server, tool, result

    @property
    def cost_usd(self) -> float:
        if self.billed_usd is not None:
            return self.billed_usd
        pin, pout = PRICES.get(self.model, (0.0, 0.0))
        return (self.prompt_tokens * pin + self.completion_tokens * pout) / 1_000_000


@dataclass
class CallLog:
    calls: list[LLMResult] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


class GuardrailBlocked(Exception):
    """Otari refused the request (403 guardrail_violation) before any model saw it."""

    def __init__(self, verdicts: list[dict]):
        super().__init__("; ".join(f"{g.get('profile')}: {g.get('explanation')}" for g in verdicts))
        self.verdicts = verdicts


class SpendLimitReached(Exception):
    """Otari refused the call: a spend ceiling (403 budget_exceeded) or an empty balance (402)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


SPEND_CODES = {"budget_exceeded", "insufficient_funds"}


class LLM:
    def complete(self, messages: list[dict], *, model: str, task: str, max_tokens: int = 512,
                 temperature: float = 0.0, json_mode: bool = False, tools: list[dict] | None = None,
                 timeout: float | None = None, route: str | None = None, guardrails: list[dict] | None = None,
                 mcp_servers: list[dict] | None = None) -> LLMResult:
        """`model` is the catalog id of the role; `route` names an Otari policy to send instead."""
        raise NotImplementedError


class OtariLLM(LLM):
    """Mozilla.ai Otari hosted gateway: OpenAI-compatible chat completions, workspace token `otk_...`.

    Model ids are `vendor/model` as listed by GET /api/v1/models; set them in
    SHOPGUARD_MODEL_SELLER / SHOPGUARD_MODEL_FAST / SHOPGUARD_MODEL_SMART.
    """

    KEY_ENV, BASE_ENV, BASE_DEFAULT = "OTARI_API_KEY", "OTARI_BASE_URL", "https://api.otari.ai/api/v1"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 60.0,
                 http: httpx.Client | None = None):
        self.api_key = api_key or os.environ[self.KEY_ENV]
        self.base_url = (base_url or os.getenv(self.BASE_ENV, self.BASE_DEFAULT)).rstrip("/")
        self.http = http or httpx.Client(timeout=timeout)
        self.no_json_mode: set[str] = set()  # models whose provider rejected response_format

    def _post(self, body: dict, timeout: float | None = None) -> httpx.Response:
        return self.http.post(f"{self.base_url}/chat/completions",
                              headers={"Authorization": f"Bearer {self.api_key}"}, json=body,
                              timeout=timeout if timeout is not None else self.http.timeout)

    def complete(self, messages, *, model, task, max_tokens=512, temperature=0.0, json_mode=False, tools=None,
                 timeout=None, route=None, guardrails=None, mcp_servers=None):
        if route is None and routing_enabled():
            route = ROUTES.get(model)
        body = {"model": route or model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if tools:
            body["tools"] = tools
        if guardrails:
            body["guardrails"] = guardrails
        if mcp_servers:
            body["mcp_servers"] = mcp_servers
        if tools or mcp_servers:
            # caps Otari's tool loop: without it one amount check ran code 8 times for $0.08. A rewrite with
            # MCP needs more rounds (product, installment rules, delivery rules, then the answer); past the
            # cap Otari answers 422 "Exceeded max_tool_iterations".
            body["max_tool_iterations"] = MCP_ROUNDS if mcp_servers else TOOL_ROUNDS
        # A policy may put both candidates on Bedrock, which rejects response_format, and Otari then answers
        # 400 all_candidates_rejected. The prompts ask for JSON anyway, so a routed call goes without it.
        if json_mode and not route and model not in self.no_json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.monotonic()
        r = self._post(body, timeout)
        if r.status_code == 400 and "response_format" in body and "response_format" in r.text:
            # Otari lists Nemotron Nano with structured_output, but its Bedrock offering returns
            # 400 "'response_format' is not supported for bedrock". The prompt already asks for
            # JSON and parse_llm_json tolerates prose, so retry once without it (400s are not billed).
            self.no_json_mode.add(model)
            del body["response_format"]
            r = self._post(body, timeout)
        if r.status_code in (402, 403):
            code = r.headers.get("otari-error-code", "")
            try:
                payload = r.json()
            except ValueError:
                payload = {}
            # a guardrail refusal nests its code: {"detail": {"code": "guardrail_violation", "guardrails": [...]}}
            detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
            code = code or str(payload.get("code", "") or detail.get("code", ""))
            if code == "guardrail_violation":
                raise GuardrailBlocked(clean_guardrails(detail.get("guardrails") or payload.get("guardrails")
                                                        or [{"profile": "?", "explanation": "refused by a guardrail"}]))
            if code in SPEND_CODES:
                raise SpendLimitReached(code, str(payload.get("detail", ""))[:200])
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        return LLMResult(
            text=strip_reasoning(data["choices"][0]["message"].get("content") or ""),
            model=model, task=task,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=int((time.monotonic() - t0) * 1000),
            billed_usd=float(usage["cost_usd"]) if usage.get("cost_usd") is not None else None,
            provider=r.headers.get("otari-provider", ""),
            gateway_fallback=r.headers.get("otari-fallback", "").lower() == "true",
            route=route or "",
            served_model=r.headers.get("otari-model", ""),
            attempts=int(r.headers.get("otari-attempt-count", "1") or 1),
            guardrails=_guardrail_header(r.headers.get("otari-guardrails", "")),
            mcp_calls=_mcp_calls(data.get("otari_tool_calls") or []),
            sources=[src["url"] for call in data.get("otari_tool_calls") or [] if call.get("status") == "completed"
                     for src in call.get("sources") or [] if src.get("url")],
            code_runs=[{"code": call.get("code", ""), "stdout": call.get("stdout", "")}
                       for call in data.get("otari_tool_calls") or []
                       if call.get("type") == "code_execution" and call.get("status") == "completed"],
        )


GUARDRAIL_FIELDS = ("profile", "mode", "valid", "score", "status", "reason", "explanation")


def clean_guardrails(items) -> list[dict]:
    """Only these fields reach the page: never a URL (ours carries a token) or a raw gateway payload."""
    out = []
    for g in items if isinstance(items, list) else [items]:
        if isinstance(g, dict):
            out.append({k: (str(g[k])[:300] if k == "explanation" else g[k]) for k in GUARDRAIL_FIELDS if k in g})
    return out


def _mcp_calls(calls: list) -> list[dict]:
    out = []
    for c in calls:
        if isinstance(c, dict) and "mcp" in str(c.get("type", "")):
            result = c.get("output") or c.get("result") or c.get("text") or ""
            # Otari returns server, tool name, status and the tool's text output (no arguments), checked 05.11
            out.append({"server": c.get("server", ""), "tool": c.get("name", ""), "status": c.get("status", ""),
                        "result": str(result)[:400]})
    return out


def _guardrail_header(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        import json
        return clean_guardrails(json.loads(raw))
    except ValueError:
        return []


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

    def complete(self, messages, *, model, task, max_tokens=512, temperature=0.0, json_mode=False, tools=None,
                 timeout=None, route=None, guardrails=None, mcp_servers=None):
        self.calls.append((task, messages))
        fn = self.handlers.get(task)
        text = fn(messages) if fn else ""
        return LLMResult(text=text, model=model, task=task)


def live_llm() -> OtariLLM | None:
    """The live gateway if OTARI_API_KEY is set."""
    return OtariLLM() if os.getenv("OTARI_API_KEY") else None


def default_llm() -> LLM:
    live = live_llm()
    if live:
        return live
    from .stub import demo_stub
    return demo_stub()
