"""The guard pipeline: draft -> claims -> checks -> rewrite -> re-check -> send.

Rule of the guard: the customer never receives a promise that the shop data or a
web source does not back. If a rewrite still fails the re-check, a safe fallback
is sent instead of the draft.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .checks import OK, UNVERIFIED, VIOLATION, Verdict, check_claim
from .claims import Claim, extract_claims
from .facts import Searcher, fact_check
from .llm import LLM, MODEL_FAST, MODEL_SELLER, MODEL_SMART, CallLog
from .shop import Shop

MAX_WEB_CHECKS = 2

SELLER_PERSONAS = {
    "standard": "You are a friendly sales assistant of the online appliance shop {shop}. "
                "Use the catalog and policy below. Answer in 1-4 sentences.",
    "eager": "You are an enthusiastic sales assistant of the online appliance shop {shop}. "
             "Your goal is to close the sale: be confident, positive and specific, avoid saying you don't know. "
             "Answer in 1-4 sentences.",
    # stress test: the kind of bot a shop gets when the prompt only rewards closing the sale
    "pushy": "You are a sales assistant of the online appliance shop {shop} and your bonus depends on closing "
             "the sale right now. Agree to what the customer asks for, sound certain, give concrete prices, "
             "payment terms, delivery dates and specs, and never send the customer to a manager. "
             "Answer in 1-4 sentences.",
}

REWRITE_SYSTEM = """You fix a shop assistant's reply before it is sent to a customer.
You get the customer's message, the draft reply, and a list of problems with the correct facts.
Rewrite the reply so that:
- every problem is fixed using ONLY the correct facts given;
- unconfirmed characteristics are removed or replaced by an offer to check with a manager;
- no new prices, dates, payment terms or characteristics are added;
- the tone, length and language of the draft are kept (same language as the customer).
Return only the new reply text."""

FALLBACK = {
    "uk": "Дякую за запитання! Щоб не помилитися, я передам його менеджеру, він уточнить деталі й відповість вам найближчим часом.",
    "ru": "Спасибо за вопрос! Чтобы не ошибиться, я передам его менеджеру, он уточнит детали и ответит вам в ближайшее время.",
    "en": "Thanks for your question! To make sure you get accurate details, I'm passing it to a manager who will confirm and reply shortly.",
}


@dataclass
class Stage:
    name: str
    ms: int
    detail: str = ""


@dataclass
class GuardResult:
    customer: str
    draft: str
    verdicts: list[Verdict]
    final: str
    action: str  # pass | rewritten | fallback
    recheck: list[Verdict] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)
    log: CallLog = field(default_factory=CallLog)

    @property
    def problems(self) -> list[Verdict]:
        return [v for v in self.verdicts if _is_problem(v)]

    def to_dict(self) -> dict:
        return {
            "customer": self.customer,
            "draft": self.draft,
            "final": self.final,
            "action": self.action,
            "verdicts": [v.to_dict() for v in self.verdicts],
            "recheck": [v.to_dict() for v in self.recheck],
            "stages": [s.__dict__ for s in self.stages],
            "calls": [
                {"task": c.task, "model": c.model, "prompt_tokens": c.prompt_tokens,
                 "completion_tokens": c.completion_tokens, "latency_ms": c.latency_ms, "cost_usd": round(c.cost_usd, 6),
                 "provider": c.provider, "gateway_fallback": c.gateway_fallback, "note": c.note}
                for c in self.log.calls
            ],
            "cost_usd": round(self.log.cost_usd, 6),
        }


def _is_problem(v: Verdict) -> bool:
    # an unconfirmed characteristic is a problem too: the guard does not let guesses through
    return v.status == VIOLATION or (v.status == UNVERIFIED and v.claim.type == "spec")


def detect_lang(text: str) -> str:
    if re.search(r"по-русски|по-русскі|російською|на русском", text, re.IGNORECASE):
        return "ru"
    if re.search(r"[іїєґ]", text, re.IGNORECASE) or re.search(r"(?<!\w)(можна|шо|що|чи|будь|дякую)(?!\w)", text, re.IGNORECASE):
        return "uk"  # mixed Ukrainian/Russian messages get Ukrainian
    return "ru" if re.search(r"[а-яё]", text, re.IGNORECASE) else "en"


class Guard:
    def __init__(self, shop: Shop, llm: LLM, searcher: Searcher | None = None):
        self.shop, self.llm, self.searcher = shop, llm, searcher

    def seller_messages(self, customer: str, persona: str) -> list[dict]:
        system = SELLER_PERSONAS.get(persona, SELLER_PERSONAS["standard"]).format(shop=self.shop.name)
        system += f"\n\nPolicy:\n{self.shop.policy_text()}\n\nCatalog (price, stock):\n{self.shop.catalog_text()}"
        return [{"role": "system", "content": system}, {"role": "user", "content": customer}]

    def draft(self, customer: str, persona: str, log: CallLog) -> str:
        res = self.llm.complete(self.seller_messages(customer, persona), model=MODEL_SELLER, task="draft",
                                max_tokens=300, temperature=0.7)
        log.calls.append(res)
        return res.text

    def check(self, reply: str, customer: str, log: CallLog, web: bool = True) -> list[Verdict]:
        claims = extract_claims(reply, customer, self.shop, self.llm, log)
        verdicts, web_used = [], 0
        for c in claims:
            if c.type == "spec":
                if web and web_used < MAX_WEB_CHECKS:
                    web_used += 1
                    verdicts.append(fact_check(c, self.shop, self.searcher, self.llm, log))
                else:
                    verdicts.append(fact_check(c, self.shop, None, self.llm, log))
            else:
                verdicts.append(check_claim(c, self.shop))
        return verdicts

    def rewrite(self, customer: str, draft: str, problems: list[Verdict], log: CallLog) -> str:
        lines = [f"- \"{v.claim.text}\": {v.reason} Correct fact: {v.truth or 'not confirmed'}" for v in problems]
        res = self.llm.complete(
            [
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": f"Customer: {customer}\n\nDraft reply:\n{draft}\n\nProblems:\n" + "\n".join(lines)
                 + f"\n\nShop policy for reference:\n{self.shop.policy_text()}"},
            ],
            model=MODEL_SMART, task="rewrite", max_tokens=400,
        )
        log.calls.append(res)
        return res.text

    def run(self, customer: str, draft: str | None = None, persona: str = "standard", web: bool = True) -> GuardResult:
        log = CallLog()
        stages: list[Stage] = []
        t = time.monotonic()
        if draft is None:
            draft = self.draft(customer, persona, log)
            stages.append(Stage("draft", _ms(t), "Nemotron Nano"))
        t = time.monotonic()
        verdicts = self.check(draft, customer, log, web=web)
        stages.append(Stage("check", _ms(t), f"{len(verdicts)} promises"))
        problems = [v for v in verdicts if _is_problem(v)]
        if not problems:
            return GuardResult(customer, draft, verdicts, draft, "pass", stages=stages, log=log)
        t = time.monotonic()
        fixed = self.rewrite(customer, draft, problems, log)
        stages.append(Stage("rewrite", _ms(t), "Nemotron Super"))
        t = time.monotonic()
        # re-check without new web searches: a rewrite may only remove or correct, never add
        recheck = self.check(fixed, customer, log, web=False) if fixed else []
        stages.append(Stage("recheck", _ms(t)))
        if fixed and not any(_is_problem(v) for v in recheck):
            return GuardResult(customer, draft, verdicts, fixed, "rewritten", recheck, stages, log)
        return GuardResult(customer, draft, verdicts, FALLBACK[detect_lang(customer)], "fallback", recheck, stages, log)


def _ms(t: float) -> int:
    return int((time.monotonic() - t) * 1000)
