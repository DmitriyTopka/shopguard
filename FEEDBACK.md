# Otari feedback from building ShopGuard

ShopGuard checks what a shop chatbot promises (price, stock, installments, delivery, specs) before the customer reads it. Until this hackathon it had never run on a live model. I moved it to hosted Otari on 30 October 2026 and ran 49 test messages several times. Total spend so far: about $0.70 of the $22 balance.

## What I used

- Three open models in three roles: Gemma 3 27B as the demo shop bot, Nemotron 3 Nano to pull the promises out of a reply, Nemotron 3 Super to judge web evidence and rewrite.
- `otari_web_search` as a tool on the Super call, so the judge searches by itself.
- `usage.cost_usd` from every response, shown under each result in the demo.
- Provider fallback behind one catalog id: Super has four offerings, and my logs show calls served by both nebius and bedrock.
- Spend ceilings: $15 for the organization, and two keys with daily ceilings ($0.50 public, $2 for judges). I deleted my own daily USD cap after this worked.

## What worked well

- Switching was small. My client already spoke the OpenAI format, so the first commit was a new base URL, a key and model ids.
- Cost in the response body is the best thing in Otari for me. I dropped my price table and my spend counter, and the demo now shows real cost per call, tool calls included.
- The spend ceiling error is excellent. I set a key to $0.0001 and got a 403 with `code: budget_exceeded`, the scope in a header, and a message that said my 512 `max_tokens` made the estimate too high. I mapped it to "switch the demo to recorded runs" in ten lines.
- `GET /api/v1/models` gives prices and `supported_parameters` per model, free. I picked models from it.

## What did not work, with reproduction steps

1. **Quickstart URL.** The hosted quickstart says `https://api.otari.ai/v1`. `GET https://api.otari.ai/v1/models` returns 404 with "Otari serves its API under /api/v1". Everything works on `/api/v1`.
2. **JSON mode on Nemotron Nano.** The catalog lists `nvidia/nemotron-nano-3-30b` with `response_format` and `structured_output: true`. Its only offering is Bedrock, and a request with `"response_format": {"type": "json_object"}` returns 400 "'response_format' is not supported for bedrock". I retry once without it. The catalog also has `nvidia/nemotron-3-nano-30b-a3b`, the same model with no metadata at all, which is easy to pick by mistake.
3. **Web search finds the wrong pages for product codes.** A Super call with `otari_web_search` for "Bosch WAN24263 specifications" got Bosch's corporate pages, Yahoo Finance market reports and RocketReach company profiles. "RCNA366I30W No Frost" returned a Frost Bank branch on MapQuest. In a 49-message run the judge confirmed zero of about 25 spec claims, and the searches cost $0.42 (about $0.01 per search plus 10k to 38k prompt tokens of results).
4. **Web search is off for a new workspace**, and the request fails with 403 "web search is not enabled for this workspace". `GET /api/v1/tools` says the tool is `available: true` at the same time. A hint in that 403 pointing to Tools, Web search would have saved me a search through the docs.
5. **Search API needs its own search tool.** `POST /api/v1/search` returns 400 "No search tools are configured" even after web search is allowed, so I could not run a plain search and control the query myself.
6. **One tool-limit failure.** Once, `"max_uses": 1` on `otari_web_search` returned 400 "The toolConfig field must be defined when using toolUse and toolResult content blocks" (Bedrock). The same request later worked, so I have no clean repro yet.
7. **My first key died.** The workspace key worked at about 08:25 UTC and returned 401 "Invalid API key" from about 08:35, with only GET calls in between and no one touching the dashboard. I regenerated it and it has worked since. I could not find any log of why.

## When I was most tempted to skip Otari and call the provider directly

The web check. Before Otari, ShopGuard sent its own query to a search API and pasted five results into the judge prompt. With `otari_web_search` the model writes the query, Otari runs it, and I cannot see or steer what comes back. For exact model codes the results were unrelated, and I paid for every search. A direct search call with my own query would have been cheaper and better, and `/api/v1/search` would give me that if it worked without bringing my own search provider.

## What I had to build myself that Otari should have done

- **Fallback from the small model to the big one.** A routing policy on hosted Otari only routes across your own provider keys, so I could not send `model: "extract"` and let Otari move from Nano to Super. Otari's own fallback also ignores a 200 answer with broken JSON, which is the most common failure I see from a small model.
- **Per-user limits.** End-user budgets need a service key that only the deployment operator can create. I used two keys (public and judges) plus my own per-IP rate limit.
- **Guardrails.** Every guardrail on hosted Otari needs an account and key with a vendor (Lakera, Azure, Alinia, Patronus, watsonx, OpenAI, Bedrock). ShopGuard is itself an output guardrail for shop facts, and I would like to plug it in as one.
- **Budgets and keys from code.** Workspace keys get 401 "Invalid master key" on budgets, keys and routing policies, so I set all of them up by clicking in the dashboard.

## Would I keep using Otari without the rule

Yes, for the cost reporting and the spend ceilings alone. They replaced my price table and my daily USD cap, and they hold even if my app has a bug. I would not use the built-in web search for product facts until it finds product pages, and I would want routing policies that work on Otari's own hosted models.
