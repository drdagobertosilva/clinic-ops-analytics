"""Reference dimensions: calendar, providers, bank categories and procedure catalogue.

Sources: data/holidays.txt, data/providers.csv, data/bank_categories.csv, data/procedures.csv.
"""
import datetime, os
from .common import data_path, integer, read_csv, upsert

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _holidays():
    path = data_path("holidays.txt")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {line[:10] for line in fh if line[:4].isdigit()}


def load_dates(con, state, start="2023-01-01", end="2027-12-31"):
    hol, rows = _holidays(), []
    d, last = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    while d <= last:
        y, w, wd = d.isocalendar()
        s = d.isoformat()
        rows.append({"date": s, "year": d.year, "month": d.month, "year_month": s[:7],
                     "quarter": f"{d.year}-Q{(d.month - 1) // 3 + 1}", "iso_week": f"{y}-W{w:02d}",
                     "week_monday": (d - datetime.timedelta(days=wd - 1)).isoformat(), "weekday": wd,
                     "day_name": DAY_NAMES[wd - 1], "is_holiday": int(s in hol),
                     "is_business_day": int(wd <= 5 and s not in hol)})
        d += datetime.timedelta(days=1)
    state["rows"] += upsert(con, "dim_date", rows, "date")


def load(con, state):
    load_dates(con, state)
    state["rows"] += upsert(con, "dim_provider", [
        {"provider_id": p["provider_id"], "role": p["role"], "specialty": p.get("specialty") or None,
         "active": integer(p.get("active", 1))} for p in read_csv("providers.csv")], "provider_id")
    state["rows"] += upsert(con, "dim_bank_category", [
        {"category": c["category"], "label": c["label"], "pnl_group": c["pnl_group"],
         "sort_order": integer(c["sort_order"])} for c in read_csv("bank_categories.csv")], "category")
    state["rows"] += upsert(con, "dim_procedure", [
        {"code": p["code"], "name": p["name"], "category": p["category"], "list_price": integer(p["list_price"])}
        for p in read_csv("procedures.csv")], "code")
