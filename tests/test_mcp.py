import json

from shopguard import mcp_server as M
from shopguard.shop import Shop

SHOP = Shop.load()


def rpc(method, params=None, mid=1):
    return M.handle(SHOP, {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def tool(name, args):
    r = rpc("tools/call", {"name": name, "arguments": args})["result"]
    return json.loads(r["content"][0]["text"]), r["isError"]


def test_handshake_and_list():
    init = rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    assert M.handle(SHOP, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert names == {"find_product", "list_in_stock", "get_policy", "installment_plan"}


def test_tools_answer_from_the_catalog_and_policy():
    p, _ = tool("find_product", {"query": "Beko RCNA366I30W"})
    assert p["price_uah"] == 18999 and p["in_stock"] is True
    ok, _ = tool("installment_plan", {"sku": p["sku"], "payments": 2})
    assert ok["allowed"] and ok["each_uah"] == 9499.5
    no, _ = tool("installment_plan", {"sku": p["sku"], "payments": 12})
    assert no["allowed"] is False
    pol, _ = tool("get_policy", {"topic": "delivery"})
    assert pol["same_day"] is False and "carrier_aliases" not in pol
    bad, is_err = tool("get_policy", {"topic": "secrets"})
    assert is_err and "error" in bad
    assert rpc("tools/call", {"name": "nope", "arguments": {}})["error"]["code"] == -32602


def test_endpoint_needs_the_token(monkeypatch):
    import importlib
    monkeypatch.setenv("SHOPGUARD_MCP_TOKEN", "t0k")
    import app as A
    importlib.reload(A)
    from fastapi.testclient import TestClient
    c = TestClient(A.app)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert c.post("/mcp", json=body).status_code == 401
    assert c.post("/mcp", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.post("/mcp", json=body, headers={"Authorization": "Bearer t0k"})
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 4
    n = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers={"Authorization": "Bearer t0k"})
    assert n.status_code == 202
