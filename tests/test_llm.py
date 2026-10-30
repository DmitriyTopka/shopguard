import httpx

from shopguard import llm as L


def test_live_llm_prefers_otari(monkeypatch):
    monkeypatch.setenv("OTARI_API_KEY", "otk_test")
    monkeypatch.setenv("NEBIUS_API_KEY", "nb_test")
    assert isinstance(L.live_llm(), L.OtariLLM)
    monkeypatch.delenv("OTARI_API_KEY")
    got = L.live_llm()
    assert type(got) is L.TokenFactoryLLM
    monkeypatch.delenv("NEBIUS_API_KEY")
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
    assert seen["url"] == "https://api.otari.ai/v1/chat/completions"
    assert seen["auth"] == "Bearer otk_test"
    assert b'"response_format":{"type":"json_object"}' in seen["body"].replace(b" ", b"")
    assert res.text == "[]" and res.prompt_tokens == 100 and res.completion_tokens == 10
