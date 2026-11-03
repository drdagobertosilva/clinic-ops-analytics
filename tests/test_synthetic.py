"""The synthetic clinic: the full pipeline passes on it, it reproduces the case-study shape, the dashboard is
built from aggregates only, and the controls catch injected faults (and then block the dashboard).

    python -m unittest discover tests
"""
import csv, filecmp, importlib, os, re, sqlite3, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fresh(tmp):
    """Point the pipeline at a temp folder and reimport it (paths and the salt are read at import time)."""
    data, out = os.path.join(tmp, "data"), os.path.join(tmp, "output")
    os.environ["CLINIC_DATA"], os.environ["CLINIC_OUT"] = data, out
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    for m in [m for m in sys.modules if m.startswith(("pipeline", "synthetic", "dashboard"))]:
        del sys.modules[m]
    return (importlib.import_module("synthetic.generate"), importlib.import_module("pipeline.build"),
            importlib.import_module("pipeline.validate"), data, os.path.join(out, "clinic.db"),
            importlib.import_module("dashboard.build"))


class SyntheticClinicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        gen, build, _, cls.data, cls.db_path, dash = fresh(cls.tmp.name)
        cls.gen = gen
        gen.generate(cls.data, seed=42)
        cls.exit_code = build.main(["--no-export"])
        cls.dash_code = dash.main([])
        out = os.path.dirname(cls.db_path)
        with open(os.path.join(out, "dashboard", "index.html"), encoding="utf-8") as fh:
            cls.page = fh.read()
        with open(os.path.join(out, "pulse", "2026-W44.md"), encoding="utf-8") as fh:
            cls.pulse = fh.read()
        cls.con = sqlite3.connect(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def one(self, sql):
        return self.con.execute(sql).fetchone()[0]

    def no_show(self, where):
        return self.one(f"SELECT 1.0 * SUM(status = 'no_show') / SUM(counts_for_no_show) FROM v_appointment WHERE {where}")

    def test_all_controls_pass(self):
        self.assertEqual(self.exit_code, 0)

    def test_no_show_before_and_after_the_redesign(self):           # case study: 21.8 % -> 9.9 %
        self.assertTrue(.17 <= self.no_show("year_month BETWEEN '2026-01' AND '2026-09'") <= .26)
        self.assertTrue(.06 <= self.no_show("year_month = '2026-10'") <= .16)

    def test_agency_first_visits_attend_far_less(self):               # case study: 37 % vs 66 %
        rate = 1 - self.no_show("booked_by = 'agency'")
        self.assertTrue(.30 <= rate <= .43, rate)

    def test_cold_leads_decay_with_lead_time(self):                   # case study: 66 % (<24 h) -> 26 % (>7 d)
        band = lambda b: self.one(f"SELECT attendance_rate FROM v_attendance_by_lead_time "
                                  f"WHERE booked_by = 'agency' AND lead_time_band = '{b}'")
        self.assertGreater(band("0: <24h") - band("3: >7d"), .25)

    def test_monthly_volume(self):                                    # case study: 209 -> 245 visits
        before = self.one("SELECT SUM(status = 'attended') / 9.0 FROM v_appointment "
                          "WHERE year_month BETWEEN '2026-01' AND '2026-09'")
        october = self.one("SELECT SUM(status = 'attended') FROM v_appointment WHERE year_month = '2026-10'")
        self.assertTrue(190 <= before <= 235, before)
        self.assertTrue(215 <= october <= 275, october)

    def test_no_generated_identifier_reaches_the_warehouse(self):
        with open(os.path.join(self.data, "legacy_ehr", "patients.csv"), encoding="utf-8") as fh:
            sample = [r for r in csv.DictReader(fh) if r["mobile"]][:200]
        raw = [r["national_id"].replace(".", "").split("-")[0] for r in sample] + \
              [re.sub(r"\D", "", r["mobile"])[-8:] for r in sample]
        # every text cell in every table, except the pseudonymous keys themselves: a 16-hex hash contains 8 digits
        # in a row about 1 time in 6, so a byte-level search over thousands of keys would match by chance
        key = re.compile(r"^[pt][0-9a-f]{16}$")
        cells = set()
        for (table,) in self.con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for row in self.con.execute(f"SELECT * FROM {table}"):
                cells.update(c for c in row if isinstance(c, str) and not key.match(c))
        text = "\n".join(cells)
        for value in raw:
            self.assertNotIn(value, text)

    def test_same_seed_same_files(self):                              # reproducible: anyone gets this clinic
        with tempfile.TemporaryDirectory() as other:
            self.gen.generate(other, seed=42)
            for f in ("legacy_ehr/appointments.csv", "new_ehr/payments.json", "bank/transactions.csv"):
                self.assertTrue(filecmp.cmp(os.path.join(self.data, f), os.path.join(other, f), shallow=False), f)

    def test_dashboard_and_pulse_are_built(self):
        self.assertEqual(self.dash_code, 0)
        self.assertEqual(self.pulse.count("\n## "), 7)                  # the seven questions
        self.assertIn("No alarms", self.pulse)

    def test_dashboard_holds_aggregates_only(self):                   # decision 1 reaches the reports too
        for text in (self.page, self.pulse):
            self.assertIsNone(re.search(r"[pt][0-9a-f]{16}", text))

    def test_refuses_to_write_over_foreign_files(self):
        with tempfile.TemporaryDirectory() as other:
            open(os.path.join(other, "real_export.csv"), "w").close()
            with self.assertRaises(SystemExit):
                self.gen.generate(other)


class InjectedFaultsTest(unittest.TestCase):
    """Decision 6: a control that never fails proves nothing. Each injected fault must be caught."""

    def test_controls_catch_each_fault(self):
        with tempfile.TemporaryDirectory() as tmp:
            gen, build, validate, data, db_path, dash = fresh(tmp)
            gen.generate(data, seed=42)
            gen.inject_faults(data)
            self.assertEqual(build.main(["--no-export"]), 1)
            self.assertEqual(dash.main([]), 1)                         # decision 6: no report on bad data
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(db_path), "dashboard")))
            con = sqlite3.connect(db_path)
            _, results = validate.run(con)
            con.close()
        failed = {name for name, ok, _ in results if ok is False}
        self.assertEqual(failed, {"Bank statements reconcile", "P&L = accounting file",
                                  "Overlap 2026-07: legacy = new (payments)", "Providers recognised"})


if __name__ == "__main__":
    unittest.main()
