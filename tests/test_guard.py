import json

from shopguard.facts import SearchHit, StubSearcher
from shopguard.guard import FALLBACK, Guard
from shopguard.llm import StubLLM
from shopguard.shop import Shop
from shopguard.stub import demo_stub, stub_extract

shop = Shop.load()


def test_honest_draft_passes_untouched():
    g = Guard(shop, demo_stub())
    r = g.run("Скільки коштує мікрохвильовка Samsung MS23K3513AW?")
    assert r.action == "pass"
    assert r.final == r.draft


def test_invented_installment_is_rewritten():
    g = Guard(shop, demo_stub())
    r = g.run("Можно холодильник Beko взять в рассрочку на 10 месяцев?")
    assert r.action == "rewritten"
    assert "12 платежів" not in r.final
    assert "monobank" in r.final


def test_out_of_stock_is_caught():
    g = Guard(shop, demo_stub())
    r = g.run("Пылесос Philips FC9334 есть в наличии? Хочу сегодня забрать")
    assert any(v.status == "violation" and v.claim.type == "stock" for v in r.verdicts)
    assert "є в наявності" not in r.final


def test_unknown_product_price_never_reaches_customer():
    g = Guard(shop, demo_stub())
    r = g.run("Скільки коштує кавоварка DeLonghi Magnifica S?")
    assert "15999" not in r.final
    assert r.action in ("rewritten", "fallback")


def test_user_supplied_draft_mode():
    """The guard can sit behind any bot: check a reply we did not generate."""
    g = Guard(shop, demo_stub())
    r = g.run("Сколько стоит чайник Tefal?", draft="Чайник Tefal KO2408 стоит 999 грн.")
    assert r.action != "pass"
    assert "999 грн" not in r.final


def test_rewrite_that_still_lies_falls_back():
    stub = demo_stub()
    stub.handlers["rewrite"] = lambda m: "Звісно, 24 платежі без переплати!"
    r = Guard(shop, stub).run("Розстрочка на 24 місяці є?")
    assert r.action == "fallback"
    assert r.final == FALLBACK["uk"]


def test_web_check_supported_and_contradicted():
    hits = {"RCNA366I30W": [SearchHit("Beko RCNA366I30W specs", "https://example.com/beko", "No Wi-Fi connectivity.")]}
    stub = demo_stub()
    stub.handlers["factcheck"] = lambda m: json.dumps(
        {"verdict": "contradicted", "url": "https://example.com/beko", "quote": "No Wi-Fi connectivity.", "correct_value": "no"})
    r = Guard(shop, stub, StubSearcher(hits)).run("Does the Beko fridge have Wi-Fi?")
    spec = [v for v in r.verdicts if v.claim.type == "spec"][0]
    assert spec.status == "violation" and spec.source == "https://example.com/beko"
    assert "Wi-Fi and app control" not in r.final


def test_extractor_failure_fails_closed():
    stub = demo_stub()
    stub.handlers["extract"] = lambda m: "not json at all"  # both the small and the big model
    r = Guard(shop, stub).run("Скільки коштує кавоварка DeLonghi Magnifica S?")
    assert r.action == "fallback" and r.final == FALLBACK["uk"]
    assert "15999" not in r.final


def test_cost_is_reported():
    r = Guard(shop, demo_stub()).run("Можно холодильник Beko взять в рассрочку на 10 месяцев?")
    d = r.to_dict()
    assert {c["task"] for c in d["calls"]} >= {"draft", "extract", "rewrite"}
    assert d["cost_usd"] == 0


def test_fallback_language():
    from shopguard.guard import detect_lang
    assert detect_lang("Розстрочка на 24 місяці є?") == "uk"
    assert detect_lang("Можно чайник в рассрочку?") == "ru"
    assert detect_lang("Пишите по-русски. Рассрочка есть?") == "ru"
    assert detect_lang("Слухайте а без переплати можна оформить?") == "uk"
    assert detect_lang("Can I pay in 12 parts?") == "en"


def test_gateway_web_check_uses_otari_tool_and_caches():
    from shopguard.facts import OtariWebSearch
    from shopguard.llm import LLMResult

    class Cache(dict):
        def get(self, k):
            return super().get(k)

        def put(self, k, v):
            self[k] = v

    stub = demo_stub()
    seen = []
    base = stub.complete

    def complete(messages, **kw):
        if kw["task"] == "factcheck":
            seen.append(kw.get("tools"))
            return LLMResult(text=json.dumps({"verdict": "contradicted", "quote": "No Wi-Fi.", "correct_value": "no"}),
                             model=kw["model"], task="factcheck", sources=["https://bosch.example/spec"])
        return base(messages, **kw)

    stub.complete = complete
    web = OtariWebSearch(cache=Cache())
    calls_after = []
    for _ in range(2):
        r = Guard(shop, stub, web).run("Does the Beko fridge have Wi-Fi?")
        spec = [v for v in r.verdicts if v.claim.type == "spec"][0]
        assert spec.status == "violation" and spec.source == "https://bosch.example/spec"
        calls_after.append(web.calls)
    assert seen and all(t == [{"type": "otari_web_search", "max_uses": 3}] for t in seen)
    assert calls_after[0] >= 1 and calls_after[1] == calls_after[0]  # the repeat is served from cache


def test_extract_falls_back_to_big_model_on_broken_json_and_timeout():
    import httpx
    from shopguard.llm import MODEL_FAST, MODEL_SMART

    for broken in ("Sure! Here are the claims: price 999", httpx.ReadTimeout("slow")):
        stub = demo_stub()
        good = stub.handlers["extract"]
        base = stub.complete
        models = []

        def complete(messages, broken=broken, good=good, base=base, models=models, **kw):
            if kw["task"] == "extract":
                models.append(kw["model"])
                if kw["model"] == MODEL_FAST:
                    if isinstance(broken, Exception):
                        raise broken
                    return base(messages, **{**kw, "task": "extract-broken"})
                return base(messages, **kw)
            return base(messages, **kw)

        stub.handlers["extract-broken"] = lambda m, b=broken: b
        stub.complete = complete
        r = Guard(shop, stub).run("Можна чайник Tefal на 10 платежів?")
        assert models[:2] == [MODEL_FAST, MODEL_SMART]
        assert any(c["note"].startswith("fallback to") for c in r.to_dict()["calls"])
        assert any(v.claim.source == "llm" for v in r.verdicts)  # the big model's claims reach the checks
