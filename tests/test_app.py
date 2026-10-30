import importlib

from fastapi.testclient import TestClient


def make_client(tmp_path, monkeypatch, **env):
    monkeypatch.delenv("OTARI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SHOPGUARD_DB", str(tmp_path / "app.db"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import shopguard.budget as budget
    importlib.reload(budget)
    import app
    importlib.reload(app)
    return TestClient(app.app), app


def test_status_and_check(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    s = client.get("/api/status").json()
    assert s["live"] is False and s["web_check"] is False
    r = client.post("/api/check", json={"message": "Можно холодильник Beko взять в рассрочку на 10 месяцев?"})
    assert r.status_code == 200
    assert r.json()["action"] in ("rewritten", "fallback")


def test_rate_limit_returns_429(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch, SHOPGUARD_RATE_PER_IP_HOUR="2")
    codes = [client.post("/api/check", json={"message": "hi"}, headers={"X-Forwarded-For": "9.9.9.9"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_judge_key_bypasses_public_pool(tmp_path, monkeypatch):
    client, app = make_client(tmp_path, monkeypatch, SHOPGUARD_JUDGE_KEY="secret", SHOPGUARD_DAILY_USD_CAP="0")
    assert client.post("/api/check", json={"message": "hi"}).status_code == 503
    assert client.post("/api/check", json={"message": "hi"}, headers={"X-Judge-Key": "secret"}).status_code == 200
    assert client.post("/api/check", json={"message": "hi"}, headers={"X-Judge-Key": "wrong"}).status_code == 503


def test_input_length_limit(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    assert client.post("/api/check", json={"message": "x" * 501}).status_code == 422
