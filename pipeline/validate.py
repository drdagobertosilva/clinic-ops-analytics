"""Quality controls after every load. They compare the warehouse against figures verified independently;
if any control FAILS the load is wrong and must not feed reports until it is fixed. A control whose
reference file is missing is reported as SKIP, not as a pass."""
import json, os, re
from .common import MIGRATION_CUTOVER, data_path, read_csv

# column names that would mean identifiable data reached the warehouse (decision #1)
_PII_COLUMN = re.compile(r"(^|_)(name|rut|national_id|email|address|mobile|phone)$")
_NOT_PERSONAL = {("dim_date", "day_name"), ("dim_procedure", "name")}


def run(con):
    results = []

    def check(name, ok, detail):
        results.append((name, ok, detail))

    q = lambda sql, *a: con.execute(sql, a).fetchall()

    # 1. every bank statement reconciles to the peso (opening + transactions = closing)
    bad = q("SELECT statement_no FROM fact_bank_statement WHERE reconciles = 0")
    n = q("SELECT COUNT(*) FROM fact_bank_statement")[0][0]
    check("Bank statements reconcile", None if not n else not bad,
          f"not reconciling: {[b[0] for b in bad]}" if bad else f"{n} statements")

    # 2. the warehouse P&L equals the accounting P&L, month by month
    acc = {r["year_month"]: r for r in read_csv("accounting", "pnl_monthly.csv")}
    if acc:
        db = {r[0]: r[1:] for r in q("SELECT year_month, revenue, operating_result FROM v_pnl_monthly")}
        diff = [m for m, r in acc.items() if m not in db or
                max(abs(int(r["revenue"]) - db[m][0]), abs(int(r["operating_result"]) - db[m][1])) > 1]
        check("P&L = accounting file", not diff, f"months differ: {diff}" if diff else f"{len(acc)} months identical")
    else:
        check("P&L = accounting file", None, "no data/accounting/pnl_monthly.csv")

    # 3. migration overlap: the cut-over month must match in both EHRs
    month = MIGRATION_CUTOVER[:7]
    both = dict(q("SELECT source, SUM(amount) FROM fact_payment WHERE date LIKE ? AND active = 1 GROUP BY source",
                  month + "%"))
    if len(both) == 2:
        check(f"Overlap {month}: legacy = new (payments)", abs(both["legacy"] - both["new"]) < 1,
              f"legacy {both['legacy']:,} vs new {both['new']:,}")
    else:
        check(f"Overlap {month}: legacy = new (payments)", None, "only one EHR has that month")

    # 4. legacy history total matches the figure verified by hand when the system was decommissioned
    path = data_path("control_figures.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            expected = json.load(fh)["legacy_payments_total"]
        got = q("SELECT SUM(amount) FROM fact_payment WHERE source = 'legacy' AND active = 1")[0][0] or 0
        check("Legacy total = verified figure", abs(got - expected) <= 0.005 * abs(expected),
              f"{got:,} vs {expected:,}")
    else:
        check("Legacy total = verified figure", None, "no data/control_figures.json")

    # 5. nothing is counted twice in the unified series
    dup = q("""SELECT substr(date,1,7) m, COUNT(DISTINCT source) FROM fact_payment WHERE in_series = 1
               GROUP BY m HAVING COUNT(DISTINCT source) > 1""")
    check("Payment series has one source per month", not dup, f"months with 2 sources: {dup}" if dup else "ok")

    # 6. every provider is known (a new hire must be added to data/providers.csv)
    unknown = q("SELECT DISTINCT provider_id FROM fact_appointment WHERE provider_id LIKE 'unknown:%'")
    check("Providers recognised", not unknown, f"unknown: {[u[0] for u in unknown]}" if unknown else "ok")

    # 7. privacy by construction: no column is named like a direct identifier. It checks names, not content:
    #    it catches a loader passing a raw column through, not re-identification from quasi-identifiers
    cols = [(t, c[1]) for (t,) in q("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            for c in q(f"PRAGMA table_info({t})")
            if _PII_COLUMN.search(c[1]) and (t, c[1]) not in _NOT_PERSONAL]
    check("No direct-identifier columns", not cols, f"found: {cols}" if cols else "ok")

    print("\nCONTROLS")
    for name, ok, detail in results:
        print(f"  {'SKIP' if ok is None else 'OK  ' if ok else 'FAIL'}  {name}: {detail}")
    return all(ok is not False for _, ok, _ in results), results
