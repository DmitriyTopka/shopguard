"""ShopGuard web app: FastAPI backend + a single static page."""
from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from shopguard.budget import Budget
from shopguard.facts import make_searcher
from shopguard.guard import Guard
from shopguard.llm import MODEL_FAST, MODEL_SELLER, MODEL_SMART, OtariLLM, SpendLimitReached, default_llm
from shopguard.shop import DATA_DIR, Shop

ROOT = Path(__file__).resolve().parent
SCENARIOS_PATH = DATA_DIR / "scenarios.json"
JUDGE_KEY = os.getenv("SHOPGUARD_JUDGE_KEY", "")
TRUST_PROXY = os.getenv("SHOPGUARD_TRUST_PROXY", "1") == "1"

app = FastAPI(title="ShopGuard", docs_url=None, redoc_url=None)
shop = Shop.load()
llm = default_llm()
LIVE = isinstance(llm, OtariLLM)
# Two Otari keys, two daily spend ceilings set on otari.ai: the public pool and the judges' pool.
# Without a judges' key both pools share the public one.
judge_llm = OtariLLM(api_key=os.environ["OTARI_JUDGE_API_KEY"]) if LIVE and os.getenv("OTARI_JUDGE_API_KEY") else llm
budget = Budget(os.getenv("SHOPGUARD_DB", str(ROOT / "shopguard.db")))


searcher = make_searcher(llm, cache=budget)


class CheckIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    draft: str | None = Field(default=None, max_length=1500)
    persona: str = Field(default="eager", pattern="^(standard|eager|pushy)$")


def client_ip(req: Request) -> str:
    if TRUST_PROXY:
        fwd = req.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


def is_judge(req: Request) -> bool:
    key = req.headers.get("x-judge-key", "") or req.query_params.get("pass", "")
    return bool(JUDGE_KEY) and hmac.compare_digest(key, JUDGE_KEY)


def load_scenarios() -> list[dict]:
    if SCENARIOS_PATH.exists():
        return json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return []


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/status")
def status(req: Request):
    return {
        "live": LIVE,
        "models": {"fast": MODEL_FAST, "smart": MODEL_SMART, "seller": MODEL_SELLER},
        "web_check": searcher is not None,
        "judge": is_judge(req),
        "budget": budget.status(),
    }


@app.get("/api/scenarios")
def scenarios():
    return load_scenarios()


@app.post("/api/check")
def check(body: CheckIn, req: Request):
    judge = is_judge(req)
    who = "judge" if judge else client_ip(req)
    if not budget.allow_request(who, judge=judge):
        raise HTTPException(429, "Too many requests from your address. Try the recorded scenarios or come back in an hour.")
    guard = Guard(shop, judge_llm if judge else llm, searcher if budget.can_search() else None)
    searches_before = getattr(searcher, "calls", 0)
    try:
        result = guard.run(body.message, draft=body.draft or None, persona=body.persona)
    except SpendLimitReached as e:
        return JSONResponse(status_code=503, content={
            "detail": "Today's live budget for this demo is used up (Otari spend ceiling). Recorded scenarios still work.",
            "replay": True, "otari_code": e.code,
        })
    searches = getattr(searcher, "calls", 0) - searches_before
    budget.record(result.log.cost_usd, searches=searches, judge=judge)
    out = result.to_dict()
    out["live"] = LIVE
    return out
