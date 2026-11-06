# Otari feedback from building ShopGuard

ShopGuard checks what a shop chatbot promises (price, stock, installments, delivery, specs) before the customer reads it. Until this hackathon it had never run on a live model. I moved it to hosted Otari on 30 October 2026 and ran 49 test messages several times. By 5 November the organization had spent $4.14 against its $15 ceiling, all experiments included; the two 49-message runs with every feature on cost $0.31 of it.

## What I used

- Three open models in three roles: Gemma 3 27B as the demo shop bot, Nemotron 3 Nano to pull the promises out of a reply, Nemotron 3 Super to judge web evidence and rewrite.
- `otari_web_search` as a tool on the Super call, so the judge searches by itself.
- `otari_code_execution` as a tool on a Super call (added 3 November): when a reply has a money amount that is not a catalog price, Super writes Python on the shop's facts and runs it through Otari, so an honest "2 payments of 9 499.50 UAH" is no longer treated as an invented price.
- `usage.cost_usd` from every response, shown under each result in the demo.
- Routing policies (added 5 November): every role calls a Fallback policy instead of a model (`shopguard-seller`, `shopguard-extract`, `shopguard-judge`). A drill policy starts on a model that is listed but not deployed, and the demo shows `Otari-Fallback: true` with 2 attempts.
- A guardrail of my own (5 November): the seller call names ShopGuard's `/validate` endpoint in `guardrails`, and Otari refuses a manipulative customer message with 403 before the model sees it. The judge is gpt-oss-safeguard-120b, itself called through Otari.
- An MCP server of my own (5 November): `shop-data` serves the catalog and the shop's rules, and the rewrite step looks facts up through Otari's managed MCP loop.
- End-user budgets (5 November): a service key, one end user per visitor with a daily budget of its own, a bigger one for judges.
- Spend ceilings: $15 for the organization and a daily ceiling on the demo's key. I deleted my own daily USD cap after this worked.

## What worked well

- Switching was small. My client already spoke the OpenAI format, so the first commit was a new base URL, a key and model ids.
- Cost in the response body is the best thing in Otari for me. I dropped my price table and my spend counter, and the demo now shows real cost per call, tool calls included.
- The spend ceiling error is good. I set a key to $0.0001 and got a 403 with `code: budget_exceeded`, the scope in a header, and a message that said my 512 `max_tokens` made the estimate too high. I mapped it to "switch the demo to recorded runs" in ten lines.
- A guardrail pointed at my own URL worked on the first try, and the 403 body names the profile and my service's explanation, so the demo can show the customer why. This turned ShopGuard from "a check after the bot" into a check on both sides of the bot.
- Fallback policies on hosted models did what the docs say: one name, a backup chain, and the headers tell me which model answered and after how many attempts.
- Code execution worked on the first call. The code and its stdout come back in `otari_tool_calls`, so my app can check that the code really ran before it trusts the answer. One check costs about $0.01.
- `GET /api/v1/models` gives prices and `supported_parameters` per model, free. I picked models from it.

## What did not work, with reproduction steps

1. **Quickstart URL.** The hosted quickstart says `https://api.otari.ai/v1`. `GET https://api.otari.ai/v1/models` returns 404 with "Otari serves its API under /api/v1". Everything works on `/api/v1`.
2. **JSON mode on Nemotron Nano.** The catalog lists `nvidia/nemotron-nano-3-30b` with `response_format` and `structured_output: true`. Its only offering is Bedrock, and a request with `"response_format": {"type": "json_object"}` returns 400 "'response_format' is not supported for bedrock". I retry once without it. The catalog also has `nvidia/nemotron-3-nano-30b-a3b`, the same model with no metadata at all, which is easy to pick by mistake.
3. **Web search finds the wrong pages for product codes.** A Super call with `otari_web_search` for "Bosch WAN24263 specifications" got Bosch's corporate pages, Yahoo Finance market reports and RocketReach company profiles. "RCNA366I30W No Frost" returned a Frost Bank branch on MapQuest. In a 49-message run the judge confirmed zero of about 25 spec claims, and the searches cost $0.42 (about $0.01 per search plus 10k to 38k prompt tokens of results).
4. **Web search is off for a new workspace**, and the request fails with 403 "web search is not enabled for this workspace". `GET /api/v1/tools` says the tool is `available: true` at the same time. A hint in that 403 pointing to Tools, Web search would have saved me a search through the docs.
5. **Search API needs its own search tool.** `POST /api/v1/search` returns 400 "No search tools are configured" even after web search is allowed, so I could not run a plain search and control the query myself.
6. **Tool limit breaks the request.** With `otari_web_search` and `"max_uses": 1` or `2`, Super on Bedrock often fails with 400 "The toolConfig field must be defined when using toolUse and toolResult content blocks": 3 of 4 identical calls in a row. With `max_uses: 3` it works. Full repro and request ids: [mozilla-ai/otari#2717](https://github.com/mozilla-ai/otari/issues/2717). The team confirmed it the same day: past the limit the model keeps searching until the last tool round, and that round goes to Bedrock with no tools declared. They fixed it in any-llm and closed the issue on 31 October ([otari#2731](https://github.com/mozilla-ai/otari/pull/2731)). I keep `max_uses: 3` and the retry in ShopGuard until I see the fix on otari.ai.
7. **My first key died.** The workspace key worked at about 08:25 UTC and returned 401 "Invalid API key" from about 08:35, with only GET calls in between and no one touching the dashboard. I regenerated it and it has worked since. I could not find any log of why.

8. **web_fetch and max_uses.** `{"type": "otari_web_fetch", "max_uses": 1}` returns 400 "otari_web_fetch declarations may contain only the type field", while `otari_web_search` accepts `max_uses`. Without a limit, one fetch request for an icecat page made 9 fetch calls and cost $0.08. Manufacturer pages (beko.com) and e-catalog answered the fetcher with 403.
9. **Code execution loops sometimes.** For one amount check the model called the code tool 9 times in a row before answering, $0.08. `max_tool_iterations` caps it (I found it in the MCP page, not in the code execution page), but past the cap a request with MCP fails with 422 "Exceeded max_tool_iterations=3" instead of answering from what it already has, which the docs say the last round should do. It happens through a routing policy (3 of 6 requests), not when the request names the model (0 of 6): [otari#2956](https://github.com/mozilla-ai/otari/issues/2956).
10. **`response_format` through a policy.** Nano and Super are both on Bedrock, which rejects `response_format`; through a Fallback policy the same request becomes 400 `all_candidates_rejected` "No model accepted the request (2 attempts)", which hides the real reason ([otari#2957](https://github.com/mozilla-ai/otari/issues/2957)).
11. **`credential` on a request-body guardrail is not sent** ([otari#2955](https://github.com/mozilla-ai/otari/issues/2955)). With `"url": ".../guard/validate", "credential": "..."` the request is accepted, but my endpoint got no `Authorization` header and the verdict came back `unavailable`, `evaluation_failed`. I moved the secret into the URL path, which then shows up in my access logs. A mandate under Organization, Guardrails does send its credential, so the demo now uses a mandate. A mandate on the whole workspace also checks the guardrail's own model calls, so I had to run the guard model from a second workspace; a way to exempt a key, or a mandate on one routing policy with your own URL, would make this simpler.
12. **MCP calls come back without arguments.** `otari_tool_calls` entries for MCP have `server`, `name`, `status` and `output`, so the demo can show what `find_product` returned but not what the model asked for.
13. **An empty code run counts as completed** ([otari#2954](https://github.com/mozilla-ai/otari/issues/2954)). About one call in three, Nemotron Super called `otari_code_execution` with an empty program, and `otari_tool_calls` reported it as `completed` with empty stdout and exit code 0. A `failed` status, or a hint back to the model, would save a retry.
14. **The service key is hard to find.** It is a checkbox at the bottom of Create key, Advanced. Its list of end-user budgets is a short box that scrolls inside the dialog, so I first thought my three new budgets were missing and reloaded the page.

## When I was most tempted to skip Otari and call the provider directly

The web check. Before Otari, ShopGuard sent its own query to a search API and pasted five results into the judge prompt. With `otari_web_search` the model writes the query, Otari runs it, and I cannot see or steer what comes back. For exact model codes the results were unrelated, and I paid for every search. A direct search call with my own query would have been cheaper and better, and `/api/v1/search` would give me that if it worked without bringing my own search provider.

## What I had to build myself that Otari should have done

A correction first. Until 5 November this file said that hosted guardrails need a vendor account, that routing policies only route across your own provider keys, and that a workspace cannot create a service key. All three were wrong: I had read the dashboard's vendor list and missed that a guardrail entry can name any `url`, and I had not found the service-key checkbox. Other participants' READMEs showed me. ShopGuard now uses all three, and the list below is what is still missing.

- **A fallback on a bad answer.** A policy falls back when a provider fails, not when a small model answers 200 with broken JSON, which is the most common failure I see. ShopGuard keeps its own Nano-to-Super retry for that.
- **An output guardrail.** Guardrails see the request, not the model's answer. ShopGuard's real job, checking what the bot promised, still runs in my code after the call. A `direction: "output"` hook on the same `/validate` contract would let ShopGuard plug into any app on Otari without code changes.
- **A guardrail and MCP server Otari can reach without a public URL.** Both are called from Otari's cloud, so they must sit on public https. I had Vercel; the projects that ran on a laptop needed a tunnel.
- **Budgets and keys from code.** Workspace keys get 401 "Invalid master key" on budgets, keys and routing policies, so I set all of them up by clicking in the dashboard.

## Would I keep using Otari without the rule

Yes. The cost reporting and the spend ceilings alone replaced my price table and my daily USD cap. End-user budgets, policies and a guardrail hook that calls my own service are the parts I would otherwise build and run myself. I would not use the built-in web search for product facts until it finds product pages, and I would want the guardrail hook on the output side too.
