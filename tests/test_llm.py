import httpx

from shopguard import llm as L


def test_live_llm_needs_otari_key(monkeypatch):
    monkeypatch.setenv("OTARI_API_KEY", "otk_test")
    assert isinstance(L.live_llm(), L.OtariLLM)
    monkeypatch.delenv("OTARI_API_KEY")
    assert L.live_llm() is None
    assert isinstance(L.default_llm(), L.StubLLM)


def test_otari_request_shape(monkeypatch):
    monkeypatch.delenv("OTARI_BASE_URL", raising=False)
    monkeypatch.setenv("SHOPGUARD_ROUTING", "off")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], req.read()
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "<think>x</think>[]"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
        })

    client = L.OtariLLM(api_key="otk_test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    res = client.complete([{"role": "user", "content": "hi"}], model=L.MODEL_FAST, task="extract", json_mode=True)
    assert seen["url"] == "https://api.otari.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer otk_test"
    assert b'"response_format":{"type":"json_object"}' in seen["body"].replace(b" ", b"")
    assert res.text == "[]" and res.prompt_tokens == 100 and res.completion_tokens == 10


def test_routed_call_sends_no_response_format(monkeypatch):
    monkeypatch.delenv("SHOPGUARD_ROUTING", raising=False)
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(req.read())
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}], "usage": {}})

    client = L.OtariLLM(api_key="otk_test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.complete([{"role": "user", "content": "x"}], model=L.MODEL_FAST, task="extract", json_mode=True)
    assert b"response_format" not in bodies[0] and b"shopguard-extract" in bodies[0]


def test_json_mode_rejected_by_provider_is_retried_without_it():
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = req.read()
        bodies.append(body)
        if b"response_format" in body:
            return httpx.Response(400, json={"detail": "'response_format' is not supported for bedrock."})
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}], "usage": {}})

    client = L.OtariLLM(api_key="otk_test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    for _ in range(2):
        assert client.complete([{"role": "user", "content": "x"}], model="m", task="extract", json_mode=True).text == "[]"
    assert len(bodies) == 3  # one rejected call, then the model is remembered


def _routing_client(seen: list, headers: dict | None = None) -> L.OtariLLM:
    def handler(req: httpx.Request) -> httpx.Response:
        import json
        seen.append(json.loads(req.read())["model"])
        return httpx.Response(200, headers=headers or {}, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})
    return L.OtariLLM(api_key="otk_test", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_roles_call_routing_policies(monkeypatch):
    monkeypatch.delenv("SHOPGUARD_ROUTING", raising=False)
    seen = []
    client = _routing_client(seen, {"Otari-Model": "google/gemma-3-27b-it", "Otari-Fallback": "true",
                                    "Otari-Attempt-Count": "2", "Otari-Provider": "nebius"})
    res = client.complete([{"role": "user", "content": "x"}], model=L.MODEL_SELLER, task="draft")
    client.complete([{"role": "user", "content": "x"}], model=L.MODEL_SELLER, task="draft", route=L.ROUTE_DRILL)
    assert seen == ["shopguard-seller", "shopguard-drill"]
    assert res.model == L.MODEL_SELLER and res.route == "shopguard-seller"
    assert res.gateway_fallback and res.attempts == 2 and res.served_model == "google/gemma-3-27b-it"


def test_routing_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("SHOPGUARD_ROUTING", "off")
    seen = []
    _routing_client(seen).complete([{"role": "user", "content": "x"}], model=L.MODEL_SMART, task="rewrite")
    assert seen == [L.MODEL_SMART]


def test_guardrail_refusal_is_typed():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": {"message": "Request blocked by guardrail policy.",
                                                    "code": "guardrail_violation",
                                                    "guardrails": [{"profile": "shop-manipulation",
                                                                    "explanation": "M1: override", "score": 1.0}]}})

    client = L.OtariLLM(api_key="otk_test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        client.complete([{"role": "user", "content": "x"}], model=L.MODEL_SELLER, task="draft",
                        guardrails=[{"profile": "shop-manipulation", "mode": "block", "url": "https://x"}])
    except L.GuardrailBlocked as e:
        assert e.verdicts[0]["profile"] == "shop-manipulation"
    else:
        raise AssertionError("no GuardrailBlocked")


def test_end_user_is_named_on_each_request():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        seen.append((json.loads(req.read()).get("user"), req.headers.get("otari-end-user-budget")))
        return httpx.Response(200, headers={"Otari-End-User-Budget": "b1"},
                              json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    base = L.OtariLLM(api_key="otk_test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    res = base.for_user("visitor-abc", "b1").complete([{"role": "user", "content": "x"}], model="m", task="draft")
    base.complete([{"role": "user", "content": "x"}], model="m", task="draft")
    assert seen == [("visitor-abc", "b1"), (None, None)] and res.end_user_budget == "b1"


def test_end_user_budget_refusal_carries_its_scope():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"Otari-Error-Code": "budget_exceeded", "Otari-Budget-Scope": "user"},
                              json={"detail": "User has $0.0001 left", "code": "budget_exceeded"})

    client = L.OtariLLM(api_key="otk_test", http=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        client.complete([{"role": "user", "content": "x"}], model="m", task="draft")
    except L.SpendLimitReached as e:
        assert e.scope == "user" and e.code == "budget_exceeded"
    else:
        raise AssertionError("no SpendLimitReached")
