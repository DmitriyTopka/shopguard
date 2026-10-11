import shopguard.budget as b


def test_rate_limit_per_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "RATE_PER_IP_HOUR", 3)
    bud = b.Budget(str(tmp_path / "t.db"))
    assert [bud.allow_request("1.1.1.1") for _ in range(4)] == [True, True, True, False]
    assert bud.allow_request("2.2.2.2")


def test_daily_cap_and_judge_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "DAILY_USD_CAP", 0.01)
    bud = b.Budget(str(tmp_path / "t.db"))
    assert bud.can_spend()
    bud.record(0.011)
    assert not bud.can_spend()
    assert bud.can_spend(judge=True)  # judges have their own pool


def test_search_cap_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "DAILY_SEARCH_CAP", 2)
    bud = b.Budget(str(tmp_path / "t.db"))
    bud.record(0, searches=2)
    assert not bud.can_search()
    bud.put("Beko specs", "[]")
    assert bud.get("beko specs") == "[]"
