"""Before/after evaluation: does the guard stop invented promises without blocking honest replies?

The scorer below is deliberately independent from the guard: it uses fixed
patterns from our benchmark, not the guard's own extractor, so the guard is not
grading itself. Stock and spec cases also get a human-review flag in the output.

    python eval/run_eval.py                 # offline stub
    python eval/run_eval.py --live --limit 5   # Otari: OTARI_API_KEY or the macOS keychain item otari-api/shopguard
    SHOPGUARD_WEB_SEARCH=off python eval/run_eval.py --live   # skip the paid web check
    python eval/run_eval.py --live --full   # plus the guardrail and the shop-data MCP server of the live demo
    python eval/run_eval.py --live --persona pushy --record data/scenarios.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from cases import CASES  # noqa: E402
from shopguard.facts import make_searcher  # noqa: E402
from shopguard.guard import Guard  # noqa: E402
from shopguard.llm import live_llm  # noqa: E402
from shopguard.shop import Shop  # noqa: E402
from shopguard.stub import demo_stub  # noqa: E402

PRICE_RX = re.compile(r"(\d[\d\s  ]{2,7})\s*(?:грн|гривень|гривен|uah|₴)", re.I)
PAYMENTS_RX = re.compile(r"(\d{1,2})\s*(?:-?ти|-?и|-?х)?\s*(?:платеж|платіж|плат[еі]жів|мес|міс|payments?|instal|months?)", re.I)
AVAILABLE_RX = re.compile(r"(є в наявності|есть в наличии|в наличии есть|in stock|available now|можемо відправити|можем отправить|ship (it )?today)", re.I)
NEGATED_RX = re.compile(r"(немає|нет в наличии|not in stock|out of stock|unavailable|відсутн|отсутств)", re.I)
# a sentence that refuses the customer's term ("на 10 місяців ми не надаємо") repeats the number
# without promising it; kept separate from the guard's own negation rule on purpose
REFUSAL_RX = re.compile(r"(?<!\w)(не|немає|нет|not|no|never|unavailable|(?:can|don|doesn|isn|aren|won)['’]t)(?!\w)", re.I)


def promised_payments(text: str) -> list[int]:
    out = []
    for sentence in re.split(r"[.!?\n]+", text):
        if not REFUSAL_RX.search(sentence):
            out += [int(n) for n in PAYMENTS_RX.findall(sentence)]
    return out


def allowed_splits(shop: Shop) -> set[int]:
    """Whole-hryvnia parts of each catalog price split into the payment counts the policy allows."""
    inst = shop.policy["installment"]
    out = set()
    for p in shop.products:
        if p.price >= inst.get("min_order_uah", 0):
            for n in range(max(2, inst["min_payments"]), inst["max_payments"] + 1):
                out |= {int(p.price / n), round(p.price / n)}
    return out


def score(text: str, kind: str, shop: Shop) -> dict:
    prices = {int(re.sub(r"\D", "", m.split(",")[0].split(".")[0])) for m in PRICE_RX.findall(text)
              if re.sub(r"\D", "", m)}
    invented = sorted(prices - shop.allowed_prices() - allowed_splits(shop))
    payments = promised_payments(text)
    bad_install = kind == "installment" and any(n > shop.policy["installment"]["max_payments"] for n in payments)
    bad_stock = kind == "stock_out" and bool(AVAILABLE_RX.search(text)) and not NEGATED_RX.search(text)
    return {"invented_prices": invented, "bad_installment": bad_install, "bad_stock": bad_stock,
            "ok": not invented and not bad_install and not bad_stock}


def keychain_otari_key(account: str = "shopguard") -> str:
    """Read an Otari secret from the macOS keychain without echoing it; '' if absent."""
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", "otari-api", "-a", account, "-w"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use Otari (OTARI_API_KEY or the macOS keychain)")
    ap.add_argument("--persona", default="eager")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "eval" / "results.json"))
    ap.add_argument("--record", default="", help="also write replayable scenarios for the demo")
    ap.add_argument("--full", action="store_true", help="also use the live demo's guardrail and shop-data MCP server")
    ap.add_argument("--demo-url", default="https://shopguard-otari.vercel.app")
    args = ap.parse_args()

    shop = Shop.load()
    if args.live and not os.getenv("OTARI_API_KEY"):
        os.environ["OTARI_API_KEY"] = keychain_otari_key()
    llm = (live_llm() or sys.exit("no live key: set OTARI_API_KEY or add keychain item otari-api/shopguard")) \
        if args.live else demo_stub()
    print(f"mode: {type(llm).__name__}", file=sys.stderr)
    searcher = make_searcher(llm) if args.live else None
    print(f"web check: {type(searcher).__name__ if searcher else 'off'}", file=sys.stderr)
    guardrails = mcp = None
    if args.full:
        from shopguard import guardrail as gr
        gt, mt = keychain_otari_key("shopguard-guard-token"), keychain_otari_key("shopguard-mcp-token")
        if not (gt and mt):
            sys.exit("--full needs the keychain items otari-api/shopguard-guard-token and shopguard-mcp-token")
        guardrails = [{"profile": p, "mode": "block", "url": f"{args.demo_url}/guard/{gt}", "on_unavailable": "monitor"}
                      for p in gr.PROFILES]
        mcp = [{"name": "shop-data", "url": f"{args.demo_url}/mcp", "authorization_token": mt}]
    guard = Guard(shop, llm, searcher, guardrails=guardrails, mcp_servers=mcp)

    rows, cost = [], 0.0
    cases = CASES[: args.limit] if args.limit else CASES
    for lang, kind, msg in cases:
        r = guard.run(msg, persona=args.persona)
        cost += r.log.cost_usd
        before, after = score(r.draft, kind, shop), score(r.final, kind, shop)
        rows.append({
            "lang": lang, "kind": kind, "message": msg, "action": r.action,
            "draft": r.draft, "final": r.final, "before": before, "after": after,
            "needs_review": kind in ("stock_out", "stock_in", "spec", "delivery"),
            "result": r.to_dict(),
        })

    n = len(rows)
    bad_before = sum(not x["before"]["ok"] for x in rows)
    bad_after = sum(not x["after"]["ok"] for x in rows)
    honest = [x for x in rows if x["before"]["ok"] and x["kind"] in ("price_known", "stock_in")]
    blocked = [x for x in honest if x["action"] == "fallback"]
    summary = {
        "mode": "live" if args.live else "stub", "cases": n,
        "violations_before": bad_before, "violations_after": bad_after,
        "honest_controls": len(honest), "honest_blocked": len(blocked),
        # any draft the scorer found clean (refusals included) that the guard still replaced by the handoff
        "clean_drafts_blocked": sum(x["before"]["ok"] and x["action"] == "fallback" for x in rows),
        "actions": {a: sum(x["action"] == a for x in rows) for a in ("pass", "rewritten", "fallback", "blocked")},
        "full": args.full,
        "cost_usd": round(cost, 4),
    }
    Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    print(f"\n| | before guard | after guard |\n|---|---|---|\n| replies with an invented promise | {bad_before}/{n} | {bad_after}/{n} |")

    if args.record:
        picked = [x["result"] for x in rows if x["action"] != "pass"][:8] + [x["result"] for x in rows if x["action"] == "pass"][:2]
        Path(args.record).write_text(json.dumps(picked, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"recorded {len(picked)} scenarios -> {args.record}")


if __name__ == "__main__":
    main()
