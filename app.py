"""ShopGuard web app: FastAPI backend + a single static page."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from shopguard import guardrail as gr
from shopguard import mcp_server
from shopguard.budget import Budget
from shopguard.facts import make_searcher
from shopguard.guard import Guard
from shopguard.llm import (MODEL_FAST, MODEL_SELLER, MODEL_SMART, ROUTES, OtariLLM, SpendLimitReached, default_llm,
                           routing_enabled)
from shopguard.shop import DATA_DIR, Shop

ROOT = Path(__file__).resolve().parent
SCENARIOS_PATH = DATA_DIR / "scenarios.json"
JUDGE_KEY = os.getenv("SHOPGUARD_JUDGE_KEY", "")
# Where the client address comes from. On Vercel (VERCEL is set by the platform) only the edge's own
# x-vercel-forwarded-for counts. Elsewhere X-Forwarded-For is read only when SHOPGUARD_TRUST_PROXY=1 says a
# proxy in front replaces it; by default the connection's address is used, so no header can forge it.
ON_VERCEL = bool(os.getenv("VERCEL"))
TRUST_PROXY = os.getenv("SHOPGUARD_TRUST_PROXY", "0") == "1"

app = FastAPI(title="ShopGuard", docs_url=None, redoc_url=None)
shop = Shop.load()
llm = default_llm()
LIVE = isinstance(llm, OtariLLM)
# Two Otari keys, two daily spend ceilings set on otari.ai: the public pool and the judges' pool.
# Without a judges' key both pools share the public one.
judge_llm = OtariLLM(api_key=os.environ["OTARI_JUDGE_API_KEY"]) if LIVE and os.getenv("OTARI_JUDGE_API_KEY") else llm
# Per-visitor budgets: a service key, and an Otari end user per visitor. Each visitor starts on a small daily
# budget of its own, judges on a bigger one, and the "tiny budget" demo on one that runs out in a few checks.
# The guardrail service and the shop-data MCP server keep the plain key above.
# The guard model is called with a key from a separate workspace (shopguard-guard), outside the mandate,
# so the guardrail's own calls are not sent back to the guardrail.
guard_llm = OtariLLM(api_key=os.environ["OTARI_GUARD_API_KEY"]) if LIVE and os.getenv("OTARI_GUARD_API_KEY") else llm
service_llm = OtariLLM(api_key=os.environ["OTARI_SERVICE_KEY"]) if LIVE and os.getenv("OTARI_SERVICE_KEY") else None
EU_BUDGETS = {"visitor": os.getenv("SHOPGUARD_EU_VISITOR", ""), "judge": os.getenv("SHOPGUARD_EU_JUDGE", ""),
              "tiny": os.getenv("SHOPGUARD_EU_TINY", "")}
EU_SALT = os.getenv("SHOPGUARD_EU_SALT", "") or os.getenv("SHOPGUARD_GUARD_TOKEN", "")


def end_user(req: Request, judge: bool, tiny: bool) -> tuple[str, str, str]:
    """(Otari end-user id, its budget id, a label). The id is a salted hash, never the address itself."""
    if judge:
        return "judges", EU_BUDGETS["judge"], "judges"
    kind = "tiny" if tiny else "visitor"
    digest = hashlib.sha256(f"{EU_SALT}|{kind}|{client_ip(req)}".encode()).hexdigest()[:16]
    return f"{kind}-{digest}", EU_BUDGETS[kind], kind
budget = Budget(os.getenv("SHOPGUARD_DB", str(ROOT / "shopguard.db")))


searcher = make_searcher(llm, cache=budget)

# Our guardrail service, called by Otari's cloud before the seller model sees a customer message.
# A secret token in the path keeps strangers from spending our guard model: Otari sends a request-body
# entry's `credential` only for organization mandates (checked 05.11), so the token rides in the URL.
GUARD_TOKEN = os.getenv("SHOPGUARD_GUARD_TOKEN", "")
PUBLIC_URL = os.getenv("SHOPGUARD_PUBLIC_URL", "").rstrip("/")
GUARD_URL = f"{PUBLIC_URL}/guard/{GUARD_TOKEN}" if LIVE and GUARD_TOKEN and PUBLIC_URL.startswith("https://") else ""
# SHOPGUARD_GUARDRAIL=policy: the guardrail is mandated on the shopguard-seller and shopguard-drill routing
# policies on otari.ai ("Always check"), so requests carry no guardrail of their own and Otari still runs it.
GUARDRAIL_MODE = os.getenv("SHOPGUARD_GUARDRAIL", "request")
GUARDRAILS = [{"profile": p, "mode": "block", "url": GUARD_URL, "on_unavailable": "monitor"}
              for p in gr.PROFILES] if GUARD_URL and GUARDRAIL_MODE == "request" else None


# shop-data MCP server for Otari's managed loop; Otari sends authorization_token as a Bearer header.
MCP_TOKEN = os.getenv("SHOPGUARD_MCP_TOKEN", "")
MCP_SERVER_ID = os.getenv("SHOPGUARD_MCP_SERVER_ID", "")  # shop-data registered under Tools > MCP servers
if LIVE and MCP_SERVER_ID:
    MCP_SERVERS = [{"id": MCP_SERVER_ID}]
elif LIVE and MCP_TOKEN and PUBLIC_URL.startswith("https://"):
    MCP_SERVERS = [{"name": "shop-data", "url": f"{PUBLIC_URL}/mcp", "authorization_token": MCP_TOKEN,
                    "purpose_hint": "The shop's own catalog and rules: use it for every price, stock, installment and delivery fact."}]
else:
    MCP_SERVERS = None


@app.post("/mcp")
async def mcp(req: Request):
    auth = req.headers.get("authorization", "")
    if not (MCP_TOKEN and auth.lower().startswith("bearer ") and hmac.compare_digest(auth[7:], MCP_TOKEN)):
        raise HTTPException(401)
    try:
        msg = await req.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"jsonrpc": "2.0", "id": None,
                                                      "error": {"code": -32700, "message": "parse error"}})
    if isinstance(msg, list):
        out = [r for r in (mcp_server.handle(shop, m) for m in msg[:20] if isinstance(m, dict)) if r]
        return JSONResponse(out) if out else JSONResponse(status_code=202, content=None)
    out = mcp_server.handle(shop, msg) if isinstance(msg, dict) else None
    return JSONResponse(out) if out else JSONResponse(status_code=202, content=None)


@app.get("/mcp")
def mcp_get():
    raise HTTPException(405)  # no server-initiated stream: stateless JSON only


class ValidateIn(BaseModel):
    profile: str
    input_text: str = Field(max_length=200_000)
    validate_kwargs: dict = Field(default_factory=dict)


def _guard_token_ok(token: str) -> bool:
    return bool(GUARD_TOKEN) and hmac.compare_digest(token, GUARD_TOKEN)


def _guard_bearer_ok(req: Request) -> bool:
    auth = req.headers.get("authorization", "")
    return auth.lower().startswith("bearer ") and _guard_token_ok(auth[7:])


@app.get("/guard/profiles")
def guard_profiles_bearer(req: Request):
    # the organization's guardrail service on otari.ai: Otari sends its stored credential as a Bearer header
    if not _guard_bearer_ok(req):
        raise HTTPException(401)
    return [{"name": p, "guardrail_name": desc} for p, desc in gr.PROFILES.items()]


@app.post("/guard/validate")
def guard_validate_bearer(body: ValidateIn, req: Request):
    # the mandate on otari.ai: every request of the Default workspace comes here, the seller call included
    if not _guard_bearer_ok(req):
        raise HTTPException(401)
    return _validate(body, require_markers=True)


@app.get("/guard/{token}/profiles")
def guard_profiles(token: str):
    if not _guard_token_ok(token):
        raise HTTPException(404)
    return [{"name": p, "guardrail_name": desc} for p, desc in gr.PROFILES.items()]


@app.post("/guard/{token}/validate")
def guard_validate(token: str, body: ValidateIn):
    if not _guard_token_ok(token):
        raise HTTPException(404)
    return _validate(body)


def _validate(body, require_markers: bool = False) -> dict:
    try:
        result = gr.validate(body.profile, body.input_text, guard_llm, require_markers=require_markers)
    except Exception as e:  # an unreachable guard model is an inconclusive verdict, Otari decides what follows
        result = {"valid": None, "explanation": f"guard model unavailable: {type(e).__name__}", "score": None}
    return {"profile": body.profile, "result": result}


class CheckIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    draft: str | None = Field(default=None, max_length=1500)
    persona: str = Field(default="eager", pattern="^(standard|eager|pushy)$")
    drill: bool = False  # outage drill: the seller call goes to a policy whose first model is down
    tiny_budget: bool = False  # this visitor starts on an Otari end-user budget that runs out in a few checks


def client_ip(req: Request) -> str:
    # 05.11: on Vercel two requests with forged X-Forwarded-For got the same end-user id as a plain one
    if ON_VERCEL:
        vercel = req.headers.get("x-vercel-forwarded-for", "")
        if vercel:
            return vercel.split(",")[0].strip()
    elif TRUST_PROXY:
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
        "routes": ROUTES if LIVE and routing_enabled() else {},
        "guardrails": list(gr.PROFILES) if GUARD_TOKEN and LIVE else [],
        "mcp": ["shop-data"] if MCP_SERVERS else [],
        "guardrail_mode": GUARDRAIL_MODE if GUARD_TOKEN else "off",
        "end_user_budgets": bool(service_llm),
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
    eu = None
    if service_llm:
        uid, eu_budget, eu_label = end_user(req, judge, body.tiny_budget)
        run_llm = service_llm.for_user(uid, eu_budget)
        eu = {"label": eu_label, "id": uid[:20]}
    else:
        run_llm = judge_llm if judge else llm
    guard = Guard(shop, run_llm, searcher if budget.can_search() else None, guardrails=GUARDRAILS,
                  mcp_servers=MCP_SERVERS)
    searches_before = getattr(searcher, "calls", 0)
    try:
        result = guard.run(body.message, draft=body.draft or None, persona=body.persona, drill=body.drill)
    except SpendLimitReached as e:
        if e.scope == "user":
            return JSONResponse(status_code=429, content={
                "detail": "Otari stopped this visitor: its own end-user budget for today is spent "
                          "(403 budget_exceeded, scope user). Other visitors are not affected.",
                "replay": True, "otari_code": e.code, "end_user": eu,
            })
        return JSONResponse(status_code=503, content={
            "detail": "Today's live budget for this demo is used up (Otari spend ceiling). Recorded scenarios still work.",
            "replay": True, "otari_code": e.code,
        })
    searches = getattr(searcher, "calls", 0) - searches_before
    budget.record(result.log.cost_usd, searches=searches, judge=judge)
    out = result.to_dict()
    out["live"] = LIVE
    out["end_user"] = eu
    return out
