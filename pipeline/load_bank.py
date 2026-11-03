"""Bank statements -> statement headers and categorised transactions.

Source: data/bank/{statements,transactions}.csv. Transactions arrive already categorised: the
classification rules live in the accounting step, not here, so the P&L has a single owner
(see docs/decisions.md #6). This loader only checks that every statement reconciles to the peso.
"""
from collections import defaultdict
from .common import integer, iso, read_csv, upsert


def load(con, state):
    txns, total = [], defaultdict(int)
    for r in read_csv("bank", "transactions.csv"):
        n, d = integer(r["statement_no"]), iso(r["date"])
        amount = integer(r["amount"])
        total[n] += amount
        txns.append({"txn_id": f"{n}-{r['ordinal']}", "statement_no": n, "ordinal": integer(r["ordinal"]),
                     "date": d, "year_month": d[:7] if d else None, "amount": amount, "category": r["category"]})
    state["rows"] += upsert(con, "fact_bank_transaction", txns, "txn_id")

    heads = []
    for r in read_csv("bank", "statements.csv"):
        n = integer(r["statement_no"])
        opening, closing = integer(r["opening_balance"]), integer(r["closing_balance"])
        heads.append({"statement_no": n, "year_month": r["year_month"], "opening_balance": opening,
                      "closing_balance": closing, "n_transactions": sum(t["statement_no"] == n for t in txns),
                      "reconciles": int(opening + total[n] == closing)})
    state["rows"] += upsert(con, "fact_bank_statement", heads, "statement_no")
    bad = [h["statement_no"] for h in heads if not h["reconciles"]]
    state["detail"] = f"{len(heads)} statements" + (f", NOT reconciling: {bad}" if bad else ", all reconcile")
