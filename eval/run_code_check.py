"""Live check of derived money amounts through Otari code execution.

Two pasted replies about the same fridge: an honest one that splits the catalog price
into the payments the policy allows, and a pushy one with 12 monthly payments.
Before mathcheck.py the honest reply was rewritten, because 9 499.50 and 6 333 are
not catalog prices. Usage: python eval/run_code_check.py (needs OTARI_API_KEY, about $0.15).
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from shopguard.guard import Guard  # noqa: E402
from shopguard.llm import live_llm  # noqa: E402
from shopguard.shop import Shop  # noqa: E402

CUSTOMER = "Can I get the Beko fridge in installments?"
CASES = {
    "honest_split": "Yes! The Beko RCNA366I30W costs 18 999 UAH. With monobank Pay in Parts you can split it "
                    "into 2 payments of 9 499.50 UAH or 3 payments of 6 333 UAH.",
    "pushy_12_months": "Sure! The Beko RCNA366I30W is 18 999 UAH, that's just 1 583 UAH a month over 12 months.",
}

llm = live_llm()
if llm is None:
    sys.exit("set OTARI_API_KEY")
guard = Guard(Shop.load(), llm)
out = {}
for name, draft in CASES.items():
    r = guard.run(CUSTOMER, draft=draft, web=False)
    out[name] = r.to_dict()
    print(name, r.action, [(v.status, v.claim.text, v.source) for v in r.verdicts], f"${r.log.cost_usd:.4f}")
path = pathlib.Path(__file__).with_name("results_live_code_check.json")
path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
print("wrote", path)
