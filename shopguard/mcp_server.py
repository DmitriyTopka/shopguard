"""shop-data: the shop's catalog and policy as an MCP server, for Otari's managed MCP loop.

The rewrite step no longer gets the whole policy pasted into its prompt. Nemotron Super looks up the
facts it writes (price, stock, installment terms, delivery) through these tools, and Otari runs the
calls against this server.

Transport: streamable HTTP, stateless, plain JSON responses (no SSE, no sessions), which is what a
serverless function can serve. Otari authenticates with the request's `authorization_token`.
"""
from __future__ import annotations

import json

from .shop import Shop

PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "shop-data", "version": "1.0"}

TOOLS = [
    {
        "name": "find_product",
        "description": "Look up a product in the shop's catalog by model code, name or kind (e.g. 'RCNA366I30W', "
                       "'Beko fridge'). Returns sku, name, price in UAH and whether it is in stock.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "list_in_stock",
        "description": "List the products that are in stock now, with prices. Use it to suggest an alternative "
                       "when the product the customer wants is out of stock.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_policy",
        "description": "The shop's rules on one topic: 'installment', 'delivery' or 'warranty'.",
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string",
                                                                    "enum": ["installment", "delivery", "warranty"]}},
                        "required": ["topic"]},
    },
    {
        "name": "installment_plan",
        "description": "Whether a product can be paid in a given number of installments, and the amount of each "
                       "payment. Only the payment counts the policy allows are accepted.",
        "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}, "payments": {"type": "integer"}},
                        "required": ["sku", "payments"]},
    },
]


def _product(p) -> dict:
    return {"sku": p.sku, "name": p.name, "price_uah": p.price, "in_stock": p.in_stock}


def call_tool(shop: Shop, name: str, args: dict) -> dict:
    if name == "find_product":
        p = shop.find(str(args.get("query", ""))[:200])
        return _product(p) if p else {"found": False, "note": "not in the catalog; do not promise it"}
    if name == "list_in_stock":
        return {"in_stock": [_product(p) for p in shop.products if p.in_stock]}
    if name == "get_policy":
        topic = str(args.get("topic", ""))
        if topic not in ("installment", "delivery", "warranty") or topic not in shop.policy:
            return {"error": "topic must be installment, delivery or warranty"}
        rules = {k: v for k, v in shop.policy[topic].items() if k != "carrier_aliases"}
        return {"topic": topic, **rules}
    if name == "installment_plan":
        p = shop.by_sku(str(args.get("sku", "")))
        inst = shop.policy.get("installment", {})
        try:
            n = int(args.get("payments"))
        except (TypeError, ValueError):
            return {"error": "payments must be an integer"}
        if not p:
            return {"allowed": False, "reason": "unknown sku"}
        lo, hi, floor = inst.get("min_payments", 1), inst.get("max_payments", 1), inst.get("min_order_uah", 0)
        if not lo <= n <= hi:
            return {"allowed": False, "reason": f"only {lo} to {hi} payments exist", "provider": inst.get("providers")}
        if p.price < floor:
            return {"allowed": False, "reason": f"installments start at {floor} UAH"}
        return {"allowed": True, "sku": p.sku, "payments": n, "each_uah": round(p.price / n, 2),
                "provider": inst.get("providers")}
    raise KeyError(name)


def handle(shop: Shop, msg: dict) -> dict | None:
    """One JSON-RPC message in, its response out (None for a notification)."""
    mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
    if mid is None:
        return None  # notifications/initialized and friends need no answer

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                   "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        try:
            out = call_tool(shop, name, params.get("arguments") or {})
        except KeyError:
            return err(-32602, f"unknown tool {name}")
        return ok({"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}], "isError": "error" in out})
    return err(-32601, f"method not found: {method}")
