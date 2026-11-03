"""Smaller operational sources: card POS, agency CRM, weekly ad report and the AI agent's monthly snapshot.

Sources: data/card/sales.csv, data/crm/contacts.csv, data/marketing/weekly.csv, data/ai_agent/monthly.csv.
Each one is optional: a missing file is logged as skipped, never as an error.
"""
from .common import integer, iso, patient_key, phone_key, read_csv, upsert


def load_card(con, state):
    rows = [{"txn_id": r["txn_id"], "date": iso(r["date"]), "kind": r["kind"], "amount": integer(r["amount"]),
             "fee": integer(r["fee"]), "net_payout": integer(r["net_payout"])}
            for r in read_csv("card", "sales.csv")]
    state["rows"] += upsert(con, "fact_card_sale", rows, "txn_id")


def load_crm(con, state):
    # the CRM export carries raw phones and, sometimes, national IDs: both are hashed on the way in
    rows = [{"contact_id": r["contact_id"], "phone_key": phone_key(r.get("mobile")),
             "patient_key": patient_key(r.get("national_id")), "created_on": iso(r.get("created_on")),
             "lead_source": r.get("lead_source") or None, "service_interest": r.get("service_interest") or None}
            for r in read_csv("crm", "contacts.csv")]
    state["rows"] += upsert(con, "fact_crm_contact", rows, "contact_id")


def load_marketing(con, state):
    rows = [{"iso_week": r["iso_week"], "spend_meta": integer(r.get("spend_meta")),
             "spend_google": integer(r.get("spend_google")), "conversations": integer(r.get("conversations")),
             "bookings": integer(r.get("bookings"))} for r in read_csv("marketing", "weekly.csv")]
    state["rows"] += upsert(con, "fact_marketing_weekly", rows, "iso_week")


def load_ai_agent(con, state):
    rows = [{"year_month": r["year_month"], "metric": r["metric"], "value": float(r["value"]),
             "captured_on": iso(r.get("captured_on")), "partial": integer(r.get("partial"))}
            for r in read_csv("ai_agent", "monthly.csv")]
    state["rows"] += upsert(con, "fact_ai_agent_monthly", rows, ("year_month", "metric"))
