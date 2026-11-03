"""New EHR -> patients, schedule and cash, from its REST API (read-only OAuth2 client in production).

Source here: data/new_ehr/{patients,appointments,payments}.json, saved in the same shape the API returns
({"data": [...]}), so the loader logic is identical to the production one minus the HTTP client.
The new EHR also holds a migrated copy of the legacy history: those rows are loaded (they are the
overlap control) but only dates from the cut-over on count for the unified series.
"""
from .common import (appointment_status, days_between, in_series, integer, iso, patient_key, phone_key,
                     provider_id, read_json, upsert)

SOURCE = "new"


def load(con, state):
    id_to_key, patients = {}, []
    for r in read_json("new_ehr", "patients.json"):
        k = patient_key(r.get("rut"))
        if not k:
            continue
        id_to_key[r["id"]] = k
        bd = iso(r.get("fecha_nacimiento"))
        patients.append({"patient_key": k, "phone_key": phone_key(r.get("celular")),
                         "birth_year": int(bd[:4]) if bd else None, "sex": r.get("sexo"),
                         "municipality": r.get("comuna"), "insurance_plan": r.get("prevision"),
                         "registered_on": iso(r.get("fecha_registro")), "source": SOURCE})
    state["rows"] += upsert(con, "dim_patient", patients, "patient_key")

    appts = []
    for r in read_json("new_ehr", "appointments.json"):
        d = iso(r.get("fecha"))
        booked_on = iso(r.get("fecha_creacion"))
        appts.append({
            "appointment_uid": f"new-{r['id']}", "source": SOURCE, "appointment_id": str(r["id"]),
            "in_series": in_series(SOURCE, d), "date": d, "time": r.get("hora_inicio"),
            "duration_min": integer(r.get("duracion")) or None,
            "provider_id": provider_id(new_ehr_id=r.get("id_profesional")),
            "patient_key": id_to_key.get(r.get("id_paciente")), "chair": r.get("sillon"),
            "source_status": r.get("estado"), "status": appointment_status(r.get("estado")),
            "booked_by": (r.get("canal") or "staff").lower(), "booked_on": booked_on,
            "lead_time_days": days_between(booked_on, d)})
    state["rows"] += upsert(con, "fact_appointment", appts, "appointment_uid")

    pays = []
    for r in read_json("new_ehr", "payments.json"):
        d = iso(r.get("fecha"))
        pays.append({
            "payment_uid": f"new-{r['id']}", "source": SOURCE, "payment_id": str(r["id"]),
            "in_series": in_series(SOURCE, d), "date": d, "amount": integer(r.get("monto")),
            "method": (r.get("medio_pago") or "").lower() or None, "patient_key": id_to_key.get(r.get("id_paciente")),
            "provider_id": provider_id(new_ehr_id=r.get("id_profesional")), "active": int(not r.get("anulado"))})
    state["rows"] += upsert(con, "fact_payment", pays, "payment_uid")
    state["detail"] = f"{len(appts):,} appointments, {len(pays):,} payments"
