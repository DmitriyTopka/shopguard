# ShopGuard: an honesty guard for shop chatbots

**Your shop chatbot just promised a discount that does not exist. ShopGuard checks every promise (price, stock, installments, specs) against the shop's data and the web, and fixes the reply before the customer sees it. Open models via Otari.**

Online shops put LLM bots behind the "chat with us" button, and those bots make promises the shop never made: a price that is not in the catalog, or 12 interest-free payments when the policy allows 3. Sometimes "in stock, ships today" for something that sold out last week. The customer believes the bot, and the shop either honors a promise it can't afford or loses the customer.

ShopGuard sits between any shop bot and the customer. Before a reply is sent, every promise in it is checked against the shop's own data and, for product specs, against the open web. Wrong or unconfirmed promises are fixed. If the fix still fails the check, the customer gets a polite hand-off to a manager instead of a lie.

> Built for Mozilla's Developers Wilding: Online hackathon, on the hosted [Otari](https://github.com/mozilla-ai/otari) gateway. Live demo: [shopguard-otari.vercel.app](https://shopguard-otari.vercel.app) · Video (4 min, my own voice in Russian with English subtitles): [shopguard-otari.vercel.app/demo.mp4](https://shopguard-otari.vercel.app/demo.mp4) · Issue opened on Otari: [mozilla-ai/otari#2717](https://github.com/mozilla-ai/otari/issues/2717), fixed by the team on 31 October

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
      │      └─ an amount that is no catalog price ──► Nemotron Super runs Python via otari_code_execution
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

- **Code checks what the shop already knows.** Prices, stock, installment rules and delivery terms are facts in the shop's data, so plain code checks them: exact and free. Models do what needs language: finding promises in free text (English, Ukrainian, Russian or a mix) and judging web evidence.
- **Two extractors.** Nemotron Nano lists the promises; a regex net independently catches every amount of money and every number of payments. If the model misses a price, the regex still sends it to the check. A term the reply itself refuses ("12 payments are not available") is not treated as a promise.
- A model asked to fix a reply can add a new mistake, so the rewrite goes through the same checks again before it is sent.
- The `draft` field of the API accepts a reply from a bot you already run, so the guard can sit in front of an existing bot.

## Otari: what we use and how

All model calls go through the hosted gateway at `https://api.otari.ai/api/v1` with one workspace key (`shopguard/llm.py`, class `OtariLLM`). The first commit where Otari came in: [`d0920d9`](https://github.com/DmitriyTopka/shopguard/commit/d0920d9221602e1482c65f4962d40e9424a37123) on branch `otari-hackathon`.

Rubric features in the rubric's own words, with what is really in the app:

| Rubric feature | Status | How ShopGuard uses it | Where |
|---|---|---|---|
| Open-weight models, two or more | used | Gemma 3 27B, Nemotron 3 Nano, Nemotron 3 Super | `shopguard/llm.py` |
| Multiple models serving distinct roles | used | Gemma writes the demo seller's reply; Nano extracts promises; Super judges web evidence, checks arithmetic and rewrites | `guard.py`, `claims.py`, `facts.py`, `mathcheck.py` |
| Web search or web fetch | used | the judge searches by itself through the built-in `otari_web_search` tool, one gateway call per claim, sources come back in `otari_tool_calls`; verdicts are cached per claim | `shopguard/facts.py`, `OtariWebSearch` |
| Code execution | used | an amount that is not a catalog price ("2 payments of 9 499.50 UAH") goes to Super with the built-in `otari_code_execution` tool; Super writes and runs Python on the shop's facts, and the amount is accepted only if the gateway really ran the code, the value matches and the split uses a payment count the policy allows. Before this, honest replies with monthly amounts were rewritten (`eval/results_live_code_check.json`) | `shopguard/mathcheck.py` |
| Budgets and spending limits | used | an organization ceiling of $15, set on otari.ai; when Otari refuses a call (403 `budget_exceeded`, 402 `insufficient_funds`) the demo switches to recorded runs instead of failing. ShopGuard's own daily USD cap was deleted in favor of these ceilings | `app.py`, `shopguard/llm.py` |
| Per-user budgets | partial | two keys with their own daily ceilings: the public demo ($0.50 a day) and the judges ($2 a day, reached with `?pass=`), plus a per-IP rate limit in ShopGuard. Real end-user budgets need a service key that a workspace cannot create | `app.py`, `shopguard/budget.py` |
| Model routing and fallbacks | partial | Super has four providers behind one catalog id, but in our runs every Super call went to bedrock and `Otari-Fallback: true` never appeared (headers are logged per call, `eval/results_live_eager_providers.json`). The fallback from Nano to Super on a timeout (20 s) or broken JSON is ShopGuard code, because a hosted routing policy only routes across your own provider keys | `shopguard/claims.py`, `shopguard/llm.py` |
| MCP integrations | not used | | |
| Guardrails | not used | every hosted guardrail needs an account and key with a vendor | |

Every call's cost comes from Otari's `usage.cost_usd` (tool charges included) and is shown under each result in the demo.

The things that did not work as documented are listed below in "Notes on Otari", they went into the feedback.

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
- `otari_web_search` with a small `max_uses` often failed on Bedrock with 400 "toolConfig field must be defined". I reported it as [#2717](https://github.com/mozilla-ai/otari/issues/2717), and the team fixed it on 31 October ([#2731](https://github.com/mozilla-ai/otari/pull/2731)). ShopGuard still uses `max_uses: 3` and retries once with a higher limit.
- `otari_web_fetch` works on hosted Otari, but beko.com and e-catalog answered it with 403, so ShopGuard keeps `otari_web_search` for specs. `otari_web_fetch` also rejects `max_uses` (400 "declarations may contain only the type field"), while `otari_web_search` accepts it.
- Web search is off for a new workspace until it is allowed under Tools, Web search; the request fails with 403 "web search is not enabled for this workspace".
- Keys, budgets and routing policies cannot be created with a workspace key (401 "Invalid master key"); only in the dashboard.
- Guardrails are not used: every guardrail on the hosted gateway (Lakera, Azure, Alinia, Patronus, watsonx, OpenAI moderation, Bedrock) needs an account and a key with that vendor. ShopGuard is itself an output guardrail for shop facts; we would like to plug it into Otari as one.
- A routing policy on hosted Otari only routes across your own provider keys, so the small-to-big model fallback had to be written in ShopGuard.

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

Configuration (see `deploy/shopguard.env.example`): the Otari keys (public and judges), model ids, prices per 1M tokens (used only when the gateway does not report cost), per-IP rate limit, daily web-check cap, and the judge pass for the demo.

## Keeping a public demo alive

The demo stays open until the end of judging. Money is capped by Otari (ceilings above); the rest lives in `shopguard/budget.py` (SQLite, one per instance on Vercel, so these limits are best effort):

- per-IP rate limit (default 20 requests per hour) with HTTP 429;
- a separate judge pool reached with `?pass=<key>`, on its own Otari key;
- at most 2 web checks per reply (other specs in that reply count as unconfirmed), a daily cap on web checks and a per-claim cache;
- recorded runs that replay instantly and cost nothing when the live budget is used up.

## Prior work

The Ukrainian / Russian / mixed test messages and the failure types come from our own benchmark published on Kaggle in October 2026, which measured how often shop assistants invent prices, installments and stock. The benchmark only measured how often bots invent things. ShopGuard tries to stop it at runtime, and the code is new.

## Data

Demo store, fictional prices, real product specs. The shop "KitchenHub", its prices and stock are made up; product models are real so that web checks have something to find. No real customer data is used.

## License

MIT, see `LICENSE`.
