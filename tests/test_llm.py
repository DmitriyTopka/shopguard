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
