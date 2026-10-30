# ShopGuard: an honesty guard for shop chatbots

**Your shop chatbot just promised a discount that does not exist. ShopGuard checks every promise (price, stock, installments, specs) against the shop's data and the web, and fixes the reply before the customer sees it. Open models via Otari.**

Online shops put LLM bots behind the "chat with us" button, and those bots make promises the shop never made: a price that is not in the catalog, 12 interest-free payments when the policy allows 3, "in stock, ships today" for an item that sold out last week. The customer believes the bot, and the shop either honors a promise it can't afford or loses the customer.

ShopGuard sits between any shop bot and the customer. Before a reply is sent, every promise in it is checked against the shop's own data and, for product specs, against the open web. Wrong or unconfirmed promises are fixed. If the fix still fails the check, the customer gets a polite hand-off to a manager instead of a lie.

> Built for Mozilla's Developers Wilding: Online hackathon, on the hosted [Otari](https://github.com/mozilla-ai/otari) gateway. Demo: _link after deploy_ · Video: _link_

## How it works

```
customer message
      │
      ▼
 draft reply ── demo seller on Gemma 3 27B (or paste a reply from your own bot)
      │
      ▼
 extract promises ── Nemotron Nano  +  regex net for money and payment counts
      │
      ├── price / stock / installments / delivery ──► plain code vs catalog.json + policy.json
      └── product specs ──► Nemotron Super with Otari's otari_web_search tool: supported / contradicted / not found
      │
      ▼
 any problem? ── no ──► send as is
      │ yes
      ▼
 rewrite ── Nemotron Super, using only the correct facts
      │
      ▼
 re-check (same rules, no new web calls) ── still wrong? ──► safe hand-off to a manager
```

Design choices:

- **Code checks what the shop already knows.** Prices, stock, installment rules and delivery terms are facts in the shop's data, so plain code checks them: exact, instant and free. Models do what needs language: finding promises in free text (English, Ukrainian, Russian or a mix) and judging web evidence.
- **Two extractors, not one.** Nemotron Nano lists the promises; a regex net independently catches every amount of money and every number of payments. If the model misses a price, the regex still sends it to the check. A term the reply itself refuses ("12 payments are not available") is not treated as a promise.
- **The rewrite is checked again.** A model asked to fix a reply can add a new mistake, so the fixed reply goes through the same checks before it is sent.
- **Works behind any bot.** The `draft` field of the API accepts a reply from an existing bot, so a shop can keep its bot and put the guard in front of the customer.

## Otari: what we use and how

All model calls go through the hosted gateway at `https://api.otari.ai/api/v1` with one workspace key (`shopguard/llm.py`, class `OtariLLM`). The first commit where Otari came in: `d0920d9` on branch `otari-hackathon`.

| Otari feature | How ShopGuard uses it | Where |
|---|---|---|
| Open models, several roles | Gemma 3 27B writes the demo seller's reply; Nemotron 3 Nano extracts promises; Nemotron 3 Super judges web evidence and rewrites | `shopguard/llm.py`, `guard.py`, `claims.py`, `facts.py` |
| Web search | the judge searches by itself through the built-in `otari_web_search` tool, one gateway call per claim, sources come back in `otari_tool_calls`; verdicts are cached per claim | `shopguard/facts.py`, `OtariWebSearch` |
| Usage and cost | each call's cost is taken from Otari's `usage.cost_usd` (tool charges included) and shown under every result in the demo | `shopguard/llm.py` |
| Routing and fallbacks | _in progress_ | |
| Budgets and per-user limits | _in progress_ | |

What did not work as documented is collected for the feedback, not hidden here: see "Notes on Otari" below.

## Results

`eval/run_eval.py` runs 49 customer messages (40 Ukrainian / Russian / mixed from our public benchmark *shop_assistant_honesty_ua_ru*, plus 9 new English ones) through the guard on Otari and scores the bot draft and the sent reply with an independent pattern scorer, not the guard's own extractor. The scorer only knows prices, payment counts and stock, so it under-counts wrong specs.

Two seller personas: **eager** (a friendly bot told to close the sale) and **pushy** (a stress test: the bot's prompt only rewards closing the sale right now).

| live run on Otari, 30.10.2026 | eager seller | pushy seller |
|---|---|---|
| replies with an invented price, installment or stock promise, before the guard | 3/49 | 8/49 |
| same, in the reply the customer gets | **0/49** | **0/49** |
| honest price and stock answers replaced by the hand-off | 0/13 | 0/13 |
| clean drafts replaced by the hand-off (any kind) | 0 | 3 |
| guard actions: pass / rewritten / hand-off | 43 / 4 / 2 | 27 / 13 / 9 |
| cost of the whole run | $0.03 | $0.44 |

Full rows: `eval/results_live_eager.json`, `eval/results_live_pushy.json`. Most of the pushy run's cost is the web check: the pushy seller states many specs, and each unconfirmed spec costs one Super call with search.

## Notes on Otari (material for the feedback)

- The hosted quickstart shows `https://api.otari.ai/v1`; the server answers 404 there ("Otari serves its API under /api/v1").
- The catalog lists `nvidia/nemotron-nano-3-30b` with `response_format`, but its only offering (Bedrock) answers 400 "'response_format' is not supported for bedrock". ShopGuard retries once without it.
- `otari_web_search` searches by model code returned company profiles and finance news instead of product pages; in the pushy run it confirmed none of the spec claims, so unconfirmed specs are removed from the reply.
- Web search is off for a new workspace until it is allowed under Tools, Web search; the request fails with 403 "web search is not enabled for this workspace".
- Keys, budgets and routing policies cannot be created with a workspace key (401 "Invalid master key"); only in the dashboard.

## Running it

```bash
pip install -r requirements.txt
python -m pytest -q tests          # offline, no keys needed
uvicorn app:app --port 8765        # without keys it runs on an offline stub model
```

With Otari:

```bash
export OTARI_API_KEY=otk_...       # workspace key from otari.ai, API keys tab
uvicorn app:app --port 8765
python eval/run_eval.py --live --limit 5                 # about $0.01
python eval/run_eval.py --live --persona pushy --record data/scenarios.json
SHOPGUARD_WEB_SEARCH=off python eval/run_eval.py --live  # skip the paid web check
```

Configuration (see `deploy/shopguard.env.example`): model ids, prices per 1M tokens (used only when the gateway does not report cost), daily USD cap, per-IP rate limit, daily web-check cap, and a separate judge key with its own quota.

## Keeping a public demo alive

The demo stays open until the end of judging, so spending is capped in code (`shopguard/budget.py`, SQLite):

- per-IP rate limit (default 20 requests per hour) with HTTP 429;
- daily USD cap for the public pool, from the cost Otari reports for each call;
- a separate judge pool reached with `?pass=<key>`;
- daily cap on web checks plus a per-claim cache;
- recorded runs that replay instantly and cost nothing when the live budget is used up.

## Prior work

The Ukrainian / Russian / mixed test messages and the failure types come from our own benchmark published on Kaggle in October 2026, which measured how often shop assistants invent prices, installments and stock. ShopGuard is new: the benchmark measured the problem, this project fixes it at runtime.

## Data

Demo store, fictional prices, real product specs. The shop "KitchenHub", its prices and stock are made up; product models are real so that web checks have something to find. No real customer data is used.

## License

MIT, see `LICENSE`.
