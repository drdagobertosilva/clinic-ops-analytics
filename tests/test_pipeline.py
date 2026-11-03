"""End-to-end smoke test on a hand-built micro clinic: two providers, three patients, a migration overlap.

    python -m unittest discover tests

Standard library only. Each assertion pins one design decision from docs/decisions.md.
"""
import csv, importlib, json, os, sqlite3, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_ID, RAW_PHONE = "15.111.222-3", "+56 9 8765 4321"   # fake, but must never reach the warehouse


def _csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _json(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"data": rows}, fh)


def make_fixture(d):
    _csv(f"{d}/providers.csv", [
        {"provider_id": "dentist-gp-1", "aliases": "rojas", "role": "dentist", "specialty": "General", "new_ehr_id": "11", "active": 1},
        {"provider_id": "endo-1", "aliases": "vera", "role": "specialist", "specialty": "Endodontics", "new_ehr_id": "12", "active": 1}])
    _csv(f"{d}/bank_categories.csv", [
        {"category": "CARD_PAYOUT", "label": "Card payouts", "pnl_group": "Revenue", "sort_order": 1},
        {"category": "LAB", "label": "Dental lab", "pnl_group": "Variable", "sort_order": 2}])
    # legacy EHR: June (in series) and July (overlap, NOT in series)
    _csv(f"{d}/legacy_ehr/patients.csv", [
        {"patient_no": "1", "national_id": RAW_ID, "mobile": RAW_PHONE, "birth_date": "02/03/1980", "sex": "F",
         "municipality": "Comuna 1", "insurance_plan": "FONASA"},
        {"patient_no": "2", "national_id": "16.333.444-K", "mobile": "", "birth_date": "", "sex": "M",
         "municipality": "Comuna 2", "insurance_plan": "ISAPRE"},
        {"patient_no": "3", "national_id": "11111111-1", "mobile": "", "birth_date": "", "sex": "",
         "municipality": "", "insurance_plan": ""}])                       # placeholder ID: dropped
    _csv(f"{d}/legacy_ehr/appointments.csv", [
        {"appt_no": "100", "date": "10/06/2026", "start": "09:00", "end": "09:45", "dentist": "Dra. Ana Rojas",
         "patient_no": "1", "status": "Atendida", "chair": "1", "booked_by": "staff", "booked_on": "09/06/2026"},
        {"appt_no": "101", "date": "11/06/2026", "start": "10:00", "end": "10:30", "dentist": "Rojas Ana",
         "patient_no": "2", "status": "No asiste", "chair": "1", "booked_by": "agency", "booked_on": "01/06/2026"},
        {"appt_no": "102", "date": "15/07/2026", "start": "10:00", "end": "11:00", "dentist": "Dr. Vera",
         "patient_no": "2", "status": "Atendida", "chair": "2", "booked_by": "staff", "booked_on": "14/07/2026"}])
    _csv(f"{d}/legacy_ehr/payments.csv", [
        {"payment_no": "500", "date": "10/06/2026", "amount": "40.000", "method": "Card", "patient_no": "1",
         "received_by": "Rojas", "voided": "0"},
        {"payment_no": "501", "date": "15/07/2026", "amount": "120.000", "method": "Transfer", "patient_no": "2",
         "received_by": "Vera", "voided": "0"}])
    # new EHR: migrated copy of July + its own August
    _json(f"{d}/new_ehr/patients.json", [
        {"id": 7, "rut": "16333444-9", "celular": "", "fecha_nacimiento": "1975-05-05", "sexo": "M",
         "comuna": "Comuna 2", "prevision": "ISAPRE", "fecha_registro": "2026-07-01"}])   # wrong check digit, same patient
    _json(f"{d}/new_ehr/appointments.json", [
        {"id": 9001, "fecha": "2026-07-15", "hora_inicio": "10:00", "duracion": 60, "id_profesional": 12,
         "id_paciente": 7, "sillon": "2", "estado": "Atendida", "canal": "staff", "fecha_creacion": "2026-07-14"},
        {"id": 9002, "fecha": "2026-08-03", "hora_inicio": "11:00", "duracion": 30, "id_profesional": 12,
         "id_paciente": 7, "sillon": "2", "estado": "No asiste", "canal": "ai_agent", "fecha_creacion": "2026-07-20"}])
    _json(f"{d}/new_ehr/payments.json", [
        {"id": 8001, "fecha": "2026-07-15", "monto": 120000, "medio_pago": "Transfer", "id_paciente": 7,
         "id_profesional": 12, "anulado": False}])
    _csv(f"{d}/bank/statements.csv", [
        {"statement_no": 1, "year_month": "2026-06", "opening_balance": 100000, "closing_balance": 120000}])
    _csv(f"{d}/bank/transactions.csv", [
        {"statement_no": 1, "ordinal": 1, "date": "2026-06-11", "amount": 38000, "category": "CARD_PAYOUT"},
        {"statement_no": 1, "ordinal": 2, "date": "2026-06-20", "amount": -18000, "category": "LAB"}])
    _csv(f"{d}/accounting/pnl_monthly.csv", [{"year_month": "2026-06", "revenue": 38000, "operating_result": 20000}])
    _csv(f"{d}/crm/contacts.csv", [
        {"contact_id": "c1", "mobile": "987654321", "national_id": "", "created_on": "2026-06-01",
         "lead_source": "meta", "service_interest": "cleaning"}])
    with open(f"{d}/control_figures.json", "w") as fh:
        json.dump({"legacy_payments_total": 160000}, fh)


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data, out = os.path.join(cls.tmp.name, "data"), os.path.join(cls.tmp.name, "output")
        make_fixture(data)
        os.environ["CLINIC_DATA"], os.environ["CLINIC_OUT"] = data, out
        sys.path.insert(0, ROOT)
        for m in [m for m in sys.modules if m.startswith("pipeline")]:
            del sys.modules[m]                       # paths are read at import time
        cls.exit_code = importlib.import_module("pipeline.build").main(["--no-export"])
        cls.db_path = os.path.join(out, "clinic.db")
        cls.con = sqlite3.connect(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def q(self, sql):
        return self.con.execute(sql).fetchall()

    def test_controls_pass(self):
        self.assertEqual(self.exit_code, 0)

    def test_no_raw_identifiers_anywhere_in_the_file(self):            # decision 1
        self.con.execute("PRAGMA wal_checkpoint(FULL)")
        with open(self.db_path, "rb") as fh:
            blob = fh.read()
        for raw in ("15111222", "8765 4321", "987654321", "Rojas", "Vera"):
            self.assertNotIn(raw.encode(), blob)

    def test_placeholder_id_is_dropped(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM dim_patient")[0][0], 2)

    def test_check_digit_typo_maps_to_same_patient(self):              # decision 1
        legacy = self.q("SELECT patient_key FROM fact_appointment WHERE appointment_uid = 'legacy-101'")[0][0]
        new = self.q("SELECT patient_key FROM fact_appointment WHERE appointment_uid = 'new-9002'")[0][0]
        self.assertEqual(legacy, new)

    def test_migration_overlap_not_double_counted(self):               # decision 3
        july = self.q("SELECT SUM(amount) FROM v_payment WHERE year_month = '2026-07'")[0][0]
        self.assertEqual(july, 120000)

    def test_free_text_providers_resolve(self):                        # decision 4 needs this
        ids = {r[0] for r in self.q("SELECT DISTINCT provider_id FROM fact_appointment")}
        self.assertEqual(ids, {"dentist-gp-1", "endo-1"})

    def test_no_show_rate(self):
        rate = self.q("SELECT no_show_rate FROM v_schedule_monthly WHERE year_month = '2026-06'")[0][0]
        self.assertEqual(rate, 0.5)

    def test_reload_is_idempotent(self):                               # decisions 2 and 5
        before = self.q("SELECT COUNT(*) FROM fact_appointment")[0][0]
        importlib.import_module("pipeline.build").main(["--no-export"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM fact_appointment")[0][0], before)

    def test_crm_contact_matches_patient_by_phone(self):
        self.assertEqual(self.q("SELECT matched_by FROM v_crm_contact_patient")[0][0], "phone")


if __name__ == "__main__":
    unittest.main()
