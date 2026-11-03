"""Legacy EHR (decommissioned) -> patients, schedule and cash, from its official CSV exports.

Source: data/legacy_ehr/{patients,appointments,payments}.csv. The export no longer changes, but reloading it
is harmless (upsert). Only rows before the migration cut-over count for the unified series.
Quirks handled here, as found in the real export: dd/mm/yyyy dates, amounts with dots as thousands
separators, and providers written as free text ("Dra. Canelo", "Canelo Ana").
"""
from .common import (appointment_status, days_between, in_series, integer, iso, minutes, patient_key,
                     phone_key, provider_id, read_csv, upsert)

SOURCE = "legacy"


def load(con, state):
    # --- patients: the export's internal patient number is mapped to the pseudonymous key and then dropped
    no_to_key, patients = {}, []
    for r in read_csv("legacy_ehr", "patients.csv"):
        k = patient_key(r.get("national_id"))
        if not k:
            continue
        no_to_key[r["patient_no"]] = k
        bd = iso(r.get("birth_date"))
        patients.append((k, phone_key(r.get("mobile")), int(bd[:4]) if bd else None, r.get("sex") or None,
                         r.get("municipality") or None, r.get("insurance_plan") or None, None, SOURCE))
    # the new EHR loads afterwards and updates these; here only patients that don't exist yet are inserted
    con.executemany("INSERT OR IGNORE INTO dim_patient VALUES (?,?,?,?,?,?,?,?)", patients)
    state["rows"] += len(patients)

    # --- appointments
    appts = []
    for r in read_csv("legacy_ehr", "appointments.csv"):
        d = iso(r.get("date"))
        start, end = minutes(r.get("start")), minutes(r.get("end"))
        booked_on = iso(r.get("booked_on"))
        appts.append({
            "appointment_uid": f"legacy-{r['appt_no']}", "source": SOURCE, "appointment_id": r["appt_no"],
            "in_series": in_series(SOURCE, d), "date": d, "time": r.get("start") or None,
            "duration_min": end - start if start is not None and end is not None else None,
            "provider_id": provider_id(name=r.get("dentist")), "patient_key": no_to_key.get(r.get("patient_no")),
            "chair": r.get("chair") or None, "source_status": r.get("status"),
            "status": appointment_status(r.get("status")), "booked_by": (r.get("booked_by") or "staff").lower(),
            "booked_on": booked_on, "lead_time_days": days_between(booked_on, d)})
    state["rows"] += upsert(con, "fact_appointment", appts, "appointment_uid")

    # --- payments (one row per receipt; the legacy "total paid" column double counts and is ignored)
    pays = []
    for r in read_csv("legacy_ehr", "payments.csv"):
        d = iso(r.get("date"))
        pays.append({
            "payment_uid": f"legacy-{r['payment_no']}", "source": SOURCE, "payment_id": r["payment_no"],
            "in_series": in_series(SOURCE, d), "date": d, "amount": integer(r.get("amount")),
            "method": (r.get("method") or "").lower() or None, "patient_key": no_to_key.get(r.get("patient_no")),
            "provider_id": provider_id(name=r.get("received_by")), "active": int(r.get("voided", "0") != "1")})
    state["rows"] += upsert(con, "fact_payment", pays, "payment_uid")
    state["detail"] = f"{len(appts):,} appointments, {len(pays):,} payments"
