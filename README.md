# ShopGuard: an honesty guard for shop chatbots

Online shops put LLM bots behind the "chat with us" button, and those bots make promises the shop never made: a price that is not in the catalog, 12 interest-free payments when the policy allows 3, "in stock, ships today" for an item that sold out last week. The customer believes the bot, and the shop either honors a promise it can't afford or loses the customer.

ShopGuard sits between any shop bot and the customer. Before a reply is sent, every promise in it is checked against the shop's own data and, for product specs, against the open web. Wrong or unconfirmed promises are fixed; if the fix still fails the check, the customer gets a polite hand-off to a manager instead of a lie.

> Demo: _link after deploy_ · Video: _link_ · Built for the Nebius x NVIDIA Global AI Hackathon, track Best Apps and Agents.

## How it works

```
customer message
      │
      ▼
 draft reply ── Nemotron Nano (or paste a reply from your own bot)
      │
      ▼
 extract promises ── Nemotron Nano, JSON  +  regex net for money and payment counts
      │
      ├── price / stock / installments / delivery ──► plain code vs catalog.json + policy.json
      └── product specs ──► Tavily web search ──► Nemotron Super: supported / contradicted / not found
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

- **Code checks what the shop already knows.** Prices, stock, installment rules and delivery terms are facts in the shop's data, so they are checked by deterministic code: exact, instant and free. Models are used where language understanding is needed: finding the promises in free text (in English, Ukrainian, Russian or a mix of them) and judging web evidence.
- **Two extractors, not one.** Nemotron Nano lists the promises; a regex net independently catches every amount of money and every number of payments. If the model misses a price, the regex still sends it to the check.
- **Small model for frequent calls, big model for judgment.** Nano drafts and extracts on every turn; Super only runs when something needs to be judged or rewritten.
- **The rewrite is checked again.** A model asked to fix a reply can introduce a new mistake, so the fixed reply goes through the same checks before it is sent.
- **Works behind any bot.** The `draft` field of the API accepts a reply from an existing bot, so a shop can keep its bot and add the guard in front of the customer.

## Where NVIDIA Nemotron and Nebius Token Factory are used

| Stage | Model on Nebius Token Factory | Why this model |
|---|---|---|
| Draft (demo seller) | Nemotron 3 Nano | fast and cheap, runs on every turn |
| Promise extraction | Nemotron 3 Nano, JSON mode | every turn; structured output |
| Web evidence verdict | Nemotron 3 Super | needs careful reading of sources |
| Rewrite | Nemotron 3 Super | must fix the reply without adding new facts |

All calls go through the OpenAI-compatible Token Factory API (`shopguard/llm.py`). Model ids and prices are set by environment variables. Each call's tokens and cost are shown in the demo under the result.

Tavily is called at runtime for product spec claims (`shopguard/facts.py`), with a per-query cache and a daily cap.

## Results

_To be filled with the live run on Nebius Token Factory._ `eval/run_eval.py` runs 49 customer messages (40 Ukrainian / Russian / mixed from our earlier public benchmark *shop_assistant_honesty_ua_ru*, plus 9 new English ones) through the guard and scores the bot draft and the sent reply with an independent pattern scorer (not the guard's own extractor).

| | before guard | after guard |
|---|---|---|
| replies with an invented promise | _live_ | _live_ |
| honest replies blocked | n/a | _live_ |

## Running it

```bash
pip install -r requirements.txt
python -m pytest -q tests          # 25 tests, offline
uvicorn app:app --port 8765        # without keys it runs on an offline stub model
```

With keys:

```bash
export NEBIUS_API_KEY=...          # Nebius Token Factory
export TAVILY_API_KEY=...          # optional, enables web checks of product specs
uvicorn app:app --port 8765
python eval/run_eval.py --live --record data/scenarios.json
```

Configuration (see `deploy/shopguard.env.example`): model ids, prices per 1M tokens, daily USD cap, per-IP rate limit, daily web-search cap, and a separate judge key with its own quota.

## Keeping a public demo alive

The demo stays open until the end of judging, so spending is capped in code (`shopguard/budget.py`, SQLite):

- per-IP rate limit (default 20 requests per hour) with HTTP 429;
- daily USD cap for the public pool, computed from real token usage of each call;
- a separate judge pool reached with `?pass=<key>`;
- daily cap on Tavily searches plus a query cache;
- recorded runs that replay instantly and cost nothing when the live budget is used up.

## Prior work

The Ukrainian / Russian / mixed test messages and the failure types come from our own benchmark published on Kaggle in October 2026, which measured how often shop assistants invent prices, installments and stock. ShopGuard is new: the benchmark measured the problem, this project fixes it at runtime.

## Data

The shop "KitchenHub", its prices and stock are fictional. Product models are real so that web checks have something to find. No real customer data is used.

## License

MIT, see `LICENSE`.
