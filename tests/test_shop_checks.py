from shopguard.checks import OK, VIOLATION, UNVERIFIED, check_claim
from shopguard.claims import Claim, regex_claims
from shopguard.shop import Shop

shop = Shop.load()


def test_find_by_model_code_and_alias():
    assert shop.find("Пылесос Philips FC9334 есть?").sku == "KH-104"
    assert shop.find("холодильник Beko в рассрочку на 10 месяцев").sku == "KH-101"
    # a different model of the same kind must not match the catalog item
    assert shop.find("робот-пылесос Xiaomi S10") is None
    assert shop.find("айфон 16") is None


def test_price_exact_and_wrong():
    ok = check_claim(Claim("price", "4299 грн", sku="KH-103", value={"amount": 4299}), shop)
    bad = check_claim(Claim("price", "3999 грн", sku="KH-103", value={"amount": 3999}), shop)
    assert ok.status == OK and bad.status == VIOLATION
    assert "4299" in bad.truth


def test_price_for_unknown_product_is_violation():
    v = check_claim(Claim("price", "15999 грн", product="DeLonghi", value={"amount": 15999}), shop)
    assert v.status == VIOLATION


def test_regex_price_safety_net():
    v_ok = check_claim(regex_claims("Ціна 18 999 грн")[0], shop)
    v_bad = check_claim(regex_claims("Ціна 17 500 грн")[0], shop)
    assert v_ok.status == OK and v_bad.status == VIOLATION


def test_stock():
    v = check_claim(Claim("stock", "є в наявності", sku="KH-104", value={"available": True}), shop)
    assert v.status == VIOLATION
    v = check_claim(Claim("stock", "є", sku="KH-105", value={"available": True}), shop)
    assert v.status == OK


def test_installment_rules():
    too_many = check_claim(Claim("installment", "12 платежів", sku="KH-101", value={"payments": 12}), shop)
    fine = check_claim(Claim("installment", "3 платежі", sku="KH-101", value={"payments": 3, "provider": "monobank"}), shop)
    other_bank = check_claim(Claim("installment", "через Приват", sku="KH-101", value={"payments": 3, "provider": "PrivatBank"}), shop)
    cheap = check_claim(Claim("installment", "2 платежі", sku="KH-105", value={"payments": 2}), shop)
    assert too_many.status == VIOLATION
    assert fine.status == OK
    assert other_bank.status == VIOLATION
    assert cheap.status == VIOLATION  # kettle is below the 3000 UAH minimum


def test_regex_installment_ignores_warranty():
    assert regex_claims("Гарантія 12 місяців від виробника.") == []
    found = regex_claims("Можна розбити на 10 платежів")
    assert found and found[0].value["payments"] == 10


def test_delivery():
    same_day = check_claim(Claim("delivery", "сьогодні", value={"same_day": True}), shop)
    ok = check_claim(Claim("delivery", "1-3 дні", value={"days": 2, "carrier": "Nova Poshta"}), shop)
    assert same_day.status == VIOLATION and ok.status == OK


def test_spec_without_web_is_unverified():
    v = check_claim(Claim("spec", "Wi-Fi", sku="KH-101", value={"attribute": "wifi", "value": "yes"}), shop)
    assert v.status == UNVERIFIED
