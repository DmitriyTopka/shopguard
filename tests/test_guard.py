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


def test_extractor_failure_still_caught_by_regex():
    stub = demo_stub()
    stub.handlers["extract"] = lambda m: "not json at all"
    r = Guard(shop, stub).run("Скільки коштує кавоварка DeLonghi Magnifica S?")
    assert any(v.claim.source == "regex" and v.status == "violation" for v in r.verdicts)
    assert "15999" not in r.final


def test_cost_is_reported():
    r = Guard(shop, demo_stub()).run("Можно холодильник Beko взять в рассрочку на 10 месяцев?")
    d = r.to_dict()
    assert {c["task"] for c in d["calls"]} >= {"draft", "extract", "rewrite"}
    assert d["cost_usd"] == 0
