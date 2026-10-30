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


def test_delivery_carrier_in_cyrillic_is_ok():
    # live Nemotron run 30.10: the extractor returned "Нова Пошта" and the guard blocked an honest reply
    for name in ("Нова Пошта", "Новую Почту"):
        assert check_claim(Claim("delivery", name, value={"carrier": name}), shop).status == OK
    for other in ("Meest", "a"):  # a fragment of an allowed name must not pass
        assert check_claim(Claim("delivery", other, value={"carrier": other}), shop).status == VIOLATION


def test_spec_without_web_is_unverified():
    v = check_claim(Claim("spec", "Wi-Fi", sku="KH-101", value={"attribute": "wifi", "value": "yes"}), shop)
    assert v.status == UNVERIFIED


def test_regex_net_skips_refused_terms():
    refusals = [
        "На жаль, інформація про 10 платежів не відповідає дійсності. Ми пропонуємо 2 або 3 платежі.",
        "Рассрочка на 10 платежей на холодильник не предусмотрена.",
        "Instalments over 12 months are not available.",
        "We only offer 2 or 3 payments, we don’t support 12 installments.",
    ]
    for text in refusals:
        assert all(c.value.get("payments") not in (10, 12) and c.value.get("months") not in (10, 12)
                   for c in regex_claims(text) if c.type == "installment"), text
    # a promise is still caught when an unrelated negator or a reassurance shares its sentence
    promises = [
        "Ні, переплати немає. Оформимо на 10 платежів!",
        "Не хвилюйтесь, оформимо на 10 платежів без переплати.",
        "No problem, we can do 10 payments for you.",
        "Без переплати, на 10 платежів, і ніяких довідок не треба.",
        "Don't worry, 10 payments is fine.",
        "You won't pay extra: 10 payments, no hidden fees.",
        "No worries, we never overpay, 10 payments works.",
    ]
    for text in promises:
        assert any(c.value.get("payments") == 10 for c in regex_claims(text)), text
