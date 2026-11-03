"""Synthetic clinic: writes data/ in exactly the layout the loaders read, so the whole pipeline runs end to
end without a single real record.

    python -m synthetic.generate [--seed 42] [--out data] [--faults]

Everything is invented: people, national IDs, phones and amounts. What is calibrated is the shape, from the
aggregate rates published in the case study: no-show near 22 % before the scheduling redesign and near 10 %
after it; agency-booked first visits attending about 37 % of the time against 66 % for clinic-booked ones;
and cold leads attending about 66 % of the time when booked <24 h ahead but 26 % beyond 7 days.
The timeline and every money figure are illustrative, not the clinic's. Edit the tables below to explore
other scenarios. The generator only ever writes into a folder it created itself (see MARKER).
"""
import argparse, csv, datetime as dt, json, os, random
from collections import defaultdict
from pipeline.common import MIGRATION_CUTOVER, strip_accents

# ------------------------------------------------------------------ timeline (illustrative)
START, END = dt.date(2023, 5, 1), dt.date(2026, 10, 31)
CUTOVER = dt.date.fromisoformat(MIGRATION_CUTOVER)     # new EHR from here on
LEGACY_LAST = dt.date(2026, 7, 31)                      # July lives in both EHRs: the overlap control
ADS_IN_HOUSE = dt.date(2024, 1, 1)                      # first paid ads, run by the owner
AGENCY = (dt.date(2024, 7, 1), dt.date(2026, 9, 30))    # a marketing agency books first visits
REDESIGN = dt.date(2026, 10, 1)                         # new admin scheduling workflow
MARKER = "SYNTHETIC"

# completed visits per month, interpolated between these points (case study: 209 in Jan-Sep 2026, 245 in Oct)
VOLUME = [("2023-05", 90), ("2024-01", 140), ("2025-01", 180), ("2026-01", 209), ("2026-09", 209),
          ("2026-10", 245)]

# ------------------------------------------------------------------ attendance model (the calibrated part)
BANDS = [(0, 0), (1, 3), (4, 7), (8, 30)]               # days booked ahead: <24h, 1-3d, 4-7d, >7d
LEAD_MIX = {                                            # how far ahead each kind of visit is booked
    "returning":    [.05, .15, .30, .50],               # follow-ups are booked weeks ahead
    "first_staff":  [.30, .40, .20, .10],               # a patient who calls the clinic wants to come soon
    "first_agency": [.10, .20, .25, .45],               # the agency chatbot filled far-away slots
}
ATTEND = {                                              # probability of showing up, by band above
    ("before", "returning"):    [.92, .91, .90, .88],
    ("before", "first_staff"):  [.75, .68, .62, .55],   # ~66 % overall
    ("before", "first_agency"): [.66, .48, .36, .26],   # ~37 % overall; 66 % -> 26 % with lead time
    ("after", "returning"):     [.92, .91, .90, .89],
    ("after", "first_staff"):   [.87, .85, .83, .80],   # the redesign holds 80-87 % at every lead time
}
FIRST_VISIT_SHARE = {"pre_agency": .22, "agency": .27, "after": .25}
AGENCY_SHARE_OF_FIRST = .50                             # while the agency ran, it booked half the first visits
RESCHEDULED, CANCELLED = .04, .04
PLAN_RATE = {"pre_agency": .84, "agency": .78, "after": .91}   # expected show rate, used to size the agenda

# ------------------------------------------------------------------ people (fictional: native tree names)
# provider_id, surname, title, role, specialty, new-EHR id, share of bookings
STAFF = [
    ("dentist-gp-1", "Canelo", "Dra.", "dentist", "General", 11, .27),
    ("dentist-gp-2", "Ulmo", "Dr.", "dentist", "General", 12, .23),
    ("ortho-1", "Lenga", "Dra.", "specialist", "Orthodontics", 13, .16),
    ("endo-1", "Coigüe", "Dr.", "specialist", "Endodontics", 14, .09),
    ("perio-1", "Raulí", "Dra.", "specialist", "Periodontics", 15, .07),
    ("surgery-1", "Alerce", "Dr.", "specialist", "Oral surgery and implants", 16, .07),
    ("aesthetics-1", "Maitén", "Dr.", "specialist", "Facial aesthetics", 17, .11),
    ("admin-1", "Quillay", "", "admin", "", 18, 0),
    ("assistant-1", "Peumo", "", "assistant", "", "", 0),
    ("assistant-2", "Boldo", "", "assistant", "", "", 0),
]
CLINICIANS = {s[0]: s for s in STAFF if s[6]}
FIRST_VISIT_PROVIDER = {"dentist-gp-1": .38, "dentist-gp-2": .32, "aesthetics-1": .20, "ortho-1": .10}

PROCEDURES = [  # code, name, category, list price (CLP), minutes
    ("EXAM", "Exam and treatment plan", "Diagnostic", 25000, 30),
    ("CLEAN", "Cleaning and prophylaxis", "Preventive", 45000, 45),
    ("FILL", "Composite filling", "Restorative", 55000, 45),
    ("CROWN", "Ceramic crown", "Prosthodontics", 320000, 60),
    ("EXTR", "Simple extraction", "Surgery", 60000, 45),
    ("SURG3", "Third molar surgery", "Surgery", 180000, 60),
    ("IMPLANT", "Implant placement", "Implants", 750000, 90),
    ("ENDO", "Root canal session", "Endodontics", 110000, 60),
    ("PERIO", "Root planing (quadrant)", "Periodontics", 70000, 60),
    ("ORTHO_CTRL", "Orthodontic control", "Orthodontics", 45000, 30),
    ("ORTHO_START", "Bracket placement", "Orthodontics", 450000, 90),
    ("WHITEN", "In-office whitening", "Aesthetics", 180000, 60),
    ("BOTOX", "Botulinum toxin (one area)", "Aesthetics", 160000, 30),
    ("FILLER", "Hyaluronic acid filler (1 ml)", "Aesthetics", 280000, 45),
]
PROC = {p[0]: p for p in PROCEDURES}
MIX = {  # what each specialty does at a visit: procedure -> weight
    "General": {"EXAM": .25, "CLEAN": .25, "FILL": .35, "EXTR": .10, "CROWN": .05},
    "Orthodontics": {"ORTHO_CTRL": .9, "ORTHO_START": .1},
    "Endodontics": {"ENDO": 1},
    "Periodontics": {"PERIO": .8, "CLEAN": .2},
    "Oral surgery and implants": {"EXTR": .45, "SURG3": .40, "IMPLANT": .15},
    "Facial aesthetics": {"BOTOX": .45, "FILLER": .25, "WHITEN": .15, "EXAM": .15},
}
FIRST_VISIT_MIX = {"General": {"EXAM": .6, "CLEAN": .4}, "Orthodontics": {"EXAM": 1}}

MUNICIPALITIES = {"Comuna 1": .55, "Comuna 2": .20, "Comuna 3": .10, "Comuna 4": .08, "Comuna 5": .07}
PLANS = {"FONASA": .70, "ISAPRE": .20, "Particular": .10}
FIXED_HOLIDAYS = ["01-01", "05-01", "05-21", "07-16", "08-15", "09-18", "09-19", "11-01", "12-08", "12-25"]

# ------------------------------------------------------------------ money (illustrative CLP)
PAY_PROB, VOID_PROB, CARD_FEE = .88, .005, .0295
METHODS = {"Card": .55, "Transfer": .30, "Cash": .15}
OPENING_BALANCE = 5_000_000
SHARE_OF_COLLECTIONS = {"PROFESSIONAL_FEES": .30, "LAB": .08, "SUPPLIES": .05, "TAXES": .02}
PAYROLL = {2023: 900_000, 2024: 1_800_000, 2025: 2_200_000, 2026: 2_300_000}
RENT, UTILITIES = 1_100_000, 160_000
WEEKLY_ADS = {"in_house": (60_000, 0), "agency": (190_000, 45_000)}   # (Meta, Google)
AGENCY_FEE_RATIO = 1.57                                 # the agency fee cost 1.57x the media it managed
AFTER_MARKETING = .70                                   # stage 6: total marketing -30 %, agency gone

BANK_CATEGORIES = [
    ("CARD_PAYOUT", "Card payouts", "Revenue"), ("TRANSFER_IN", "Patient transfers", "Revenue"),
    ("CASH_DEPOSIT", "Cash deposits", "Revenue"), ("PROFESSIONAL_FEES", "Dentist fees", "Variable"),
    ("LAB", "Dental lab", "Variable"), ("SUPPLIES", "Clinical supplies", "Variable"),
    ("ADS_META", "Meta ads", "Marketing"), ("ADS_GOOGLE", "Google ads", "Marketing"),
    ("AGENCY_FEE", "Marketing agency fee", "Marketing"), ("RENT", "Rent", "Fixed"),
    ("PAYROLL", "Staff payroll", "Fixed"), ("UTILITIES", "Utilities", "Fixed"),
    ("SOFTWARE", "Software (EHR, messaging)", "Fixed"), ("TAXES", "Tax prepayments", "Taxes"),
    ("OWNER_DRAW", "Owner draw", "OwnerDraw"), ("PERSONAL", "Personal spending", "Personal"),
    ("OWN_TRANSFER", "Transfers between own accounts", "Excluded"),
]
GROUP = {c: g for c, _, g in BANK_CATEGORIES}
OPERATING = {"Revenue", "Variable", "Marketing", "Fixed", "Taxes"}


# ------------------------------------------------------------------ helpers
def _pick(rng, weights):
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _month_index(ym):
    y, m = map(int, ym.split("-"))
    return y * 12 + m - 1


def target_visits(ym):
    i, pts = _month_index(ym), [(_month_index(k), v) for k, v in VOLUME]
    for (a, va), (b, vb) in zip(pts, pts[1:]):
        if a <= i <= b:
            return va + (vb - va) * (i - a) / (b - a)
    return pts[-1][1]


def period(d):
    return "after" if d >= REDESIGN else "agency" if AGENCY[0] <= d <= AGENCY[1] else "pre_agency"


def check_digit(body):
    s, m = 0, 2
    for c in reversed(str(body)):
        s, m = s + int(c) * m, 2 if m == 7 else m + 1
    r = 11 - s % 11
    return "0" if r == 11 else "K" if r == 10 else str(r)


def dmy(d):
    return d.strftime("%d/%m/%Y")


def hhmm(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _write_json(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"data": rows}, fh, ensure_ascii=False)


def _prepare(out):
    if os.path.isdir(out) and os.listdir(out) and not os.path.exists(os.path.join(out, MARKER)):
        raise SystemExit(f"{out} holds files the generator did not create; refusing to overwrite them")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, MARKER), "w") as fh:
        fh.write("Written by synthetic/generate.py. Every record in this folder is invented.\n")


class Calendar:
    def __init__(self):
        self.holidays = {f"{y}-{md}" for y in range(START.year, END.year + 2) for md in FIXED_HOLIDAYS}

    def is_business(self, d):
        return d.weekday() < 5 and d.isoformat() not in self.holidays

    def next_business(self, d, include=False):
        d = d if include else d + dt.timedelta(days=1)
        while not self.is_business(d):
            d += dt.timedelta(days=1)
        return d

    def business_in_month(self, y, m, day):
        """First business day on or after `day` that is still in the month (else the last one)."""
        d = dt.date(y, m, day)
        while d.month == m and not self.is_business(d):
            d += dt.timedelta(days=1)
        if d.month != m:
            d = dt.date(y, m, day)
            while not self.is_business(d):
                d -= dt.timedelta(days=1)
        return d

    def last_business(self, y, m):
        d = (dt.date(y + m // 12, m % 12 + 1, 1)) - dt.timedelta(days=1)
        while not self.is_business(d):
            d -= dt.timedelta(days=1)
        return d


def _months():
    y, m = START.year, START.month
    while (y, m) <= (END.year, END.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ------------------------------------------------------------------ simulation
class Clinic:
    def __init__(self, seed):
        self.rng, self.cal = random.Random(seed), Calendar()
        self.patients, self.active, self.appointments, self.payments = [], [], [], []
        self._ids = set()

    def new_patient(self, registered):
        rng = self.rng
        while (body := rng.randint(30_000_000, 39_999_999)) in self._ids:
            pass
        self._ids.add(body)
        r = rng.random()
        mobile = None if r < .12 else ("45" + "".join(rng.choices("0123456789", k=7))) if r < .15 else \
            "9" + "".join(rng.choices("0123456789", k=8))
        p = {"no": len(self.patients) + 1, "body": body, "dv": check_digit(body),
             "placeholder": rng.random() < .015, "typo": rng.random() < .005, "mobile": mobile,
             "birth": dt.date(rng.randint(1950, 2016), rng.randint(1, 12), rng.randint(1, 28)),
             "sex": "F" if rng.random() < .58 else "M", "municipality": _pick(rng, MUNICIPALITIES),
             "plan": _pick(rng, PLANS), "registered": registered}
        self.patients.append(p)
        return p

    def book_day(self, d, n):
        rng, per = self.rng, period(d)
        cursor = {"1": 9 * 60, "2": 9 * 60}
        for _ in range(n):
            first = not self.active or rng.random() < FIRST_VISIT_SHARE[per]
            if first:
                kind = "first_agency" if per == "agency" and rng.random() < AGENCY_SHARE_OF_FIRST else "first_staff"
                prov = CLINICIANS[_pick(rng, FIRST_VISIT_PROVIDER)]
                proc = _pick(rng, FIRST_VISIT_MIX.get(prov[4], MIX[prov[4]]))
            else:
                kind = "returning"
                prov = CLINICIANS[_pick(rng, {k: s[6] for k, s in CLINICIANS.items()})]
                proc = _pick(rng, MIX[prov[4]])
            chair = min(cursor, key=cursor.get)
            start, dur = cursor[chair] + rng.choice([0, 0, 15, 30]), PROC[proc][4]
            if start < 14 * 60 + 30 and start + dur > 13 * 60 + 30:      # lunch block 13:30-14:30
                start = 14 * 60 + 30
            if start + dur > 19 * 60:
                continue                                                # chair full: the slot is lost
            cursor[chair] = start + dur
            band = rng.choices(range(4), weights=LEAD_MIX[kind])[0]
            booked_on = d - dt.timedelta(days=rng.randint(*BANDS[band]))
            patient = self.new_patient(booked_on) if first else rng.choice(self.active)
            r = rng.random()
            if r < RESCHEDULED:
                status = "rescheduled"
            elif r < RESCHEDULED + CANCELLED:
                status = "cancelled"
            else:
                p_show = ATTEND[("after" if per == "after" else "before", kind)][band]
                status = "attended" if rng.random() < p_show else "no_show"
            if first and status == "attended":
                self.active.append(patient)
                if len(self.active) > 700:                              # long-gone patients stop returning
                    self.active.pop(0)
            appt = {"n": len(self.appointments) + 1, "date": d, "start": start, "dur": dur, "provider": prov,
                    "patient": patient, "chair": chair, "status": status, "proc": proc,
                    "booked_by": "agency" if kind == "first_agency" else "staff", "booked_on": booked_on}
            self.appointments.append(appt)
            if status == "attended" and rng.random() < PAY_PROB:
                self.payments.append({"n": len(self.payments) + 1, "date": d, "patient": patient, "provider": prov,
                                      "amount": int(round(PROC[proc][3] * rng.uniform(.85, 1.0), -3)),
                                      "method": _pick(rng, METHODS), "voided": rng.random() < VOID_PROB})

    def run(self):
        for y, m in _months():
            ym, per = f"{y}-{m:02d}", period(dt.date(y, m, 1))
            days = [dt.date(y, m, k) for k in range(1, 32) if _valid(y, m, k) and self.cal.is_business(dt.date(y, m, k))]
            per_day = target_visits(ym) / PLAN_RATE[per] / (1 - RESCHEDULED - CANCELLED) / len(days)
            for d in days:
                self.book_day(d, max(0, round(per_day + self.rng.gauss(0, 1.5))))


def _valid(y, m, d):
    try:
        dt.date(y, m, d)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ money flows
def marketing_weeks(clinic):
    """Weekly ad report: spend per channel, conversations and first-visit bookings by booking week."""
    rng, bookings = clinic.rng, defaultdict(int)
    for a in clinic.appointments:
        if a["booked_by"] == "agency" or (a["date"] >= REDESIGN and a["patient"]["registered"] == a["booked_on"]):
            y, w, _ = a["booked_on"].isocalendar()
            bookings[f"{y}-W{w:02d}"] += 1
    weeks, monday = [], ADS_IN_HOUSE - dt.timedelta(days=ADS_IN_HOUSE.weekday())
    while monday <= END:
        y, w, _ = monday.isocalendar()
        weeks.append({"monday": monday, "iso_week": f"{y}-W{w:02d}", "period": period(monday)})
        monday += dt.timedelta(days=7)
    for wk in weeks:
        if wk["period"] == "after":
            continue
        meta, google = WEEKLY_ADS["agency" if wk["period"] == "agency" else "in_house"]
        wk["spend_meta"] = int(round(meta * rng.uniform(.85, 1.15), -3))
        wk["spend_google"] = int(round(google * rng.uniform(.85, 1.15), -3))
    # stage 6: in-house ads take over the budget, at 70 % of what media + agency fee cost in Jan-Sep 2026
    before = [w for w in weeks if dt.date(2026, 1, 1) <= w["monday"] < REDESIGN]
    weekly_before = sum(w["spend_meta"] + w["spend_google"] for w in before) * (1 + AGENCY_FEE_RATIO) / len(before)
    for wk in weeks:
        if wk["period"] == "after":
            total = weekly_before * AFTER_MARKETING * rng.uniform(.9, 1.1)
            wk["spend_meta"], wk["spend_google"] = int(round(total * .8, -3)), int(round(total * .2, -3))
        wk["bookings"] = bookings.get(wk["iso_week"], 0)
        wk["conversations"] = int(wk["bookings"] * rng.uniform(2.5, 4)) + rng.randint(5, 20)
    return weeks


def bank_lines(clinic, weeks):
    rng, cal, lines = clinic.rng, clinic.cal, []
    card_sales, payout, transfer, cash = [], defaultdict(int), defaultdict(int), defaultdict(int)
    collections = defaultdict(int)
    for p in clinic.payments:
        if p["voided"]:
            continue
        d, amount = p["date"], p["amount"]
        collections[(d.year, d.month)] += amount
        if p["method"] == "Card":
            fee = int(round(amount * CARD_FEE))
            card_sales.append({"txn_id": f"pos-{p['n']}", "date": d.isoformat(), "kind": "sale", "amount": amount,
                               "fee": fee, "net_payout": amount - fee})
            payout[cal.next_business(d)] += amount - fee
        elif p["method"] == "Transfer":
            transfer[d] += amount
        else:                                                           # cash goes to the bank next Monday
            cash[cal.next_business(d + dt.timedelta(days=7 - d.weekday()), include=True)] += amount
    for cat, flows in (("CARD_PAYOUT", payout), ("TRANSFER_IN", transfer), ("CASH_DEPOSIT", cash)):
        lines += [(d, v, cat) for d, v in flows.items() if d <= END]

    ads = defaultdict(lambda: [0, 0])
    for w in weeks:
        ads[(w["monday"].year, w["monday"].month)][0] += w["spend_meta"]
        ads[(w["monday"].year, w["monday"].month)][1] += w["spend_google"]
    for y, m in _months():
        c, last = collections[(y, m)], cal.last_business(y, m)
        month = [(cal.business_in_month(y, m, 5), -RENT, "RENT"), (last, -PAYROLL[y], "PAYROLL"),
                 (cal.business_in_month(y, m, 15), -int(round(UTILITIES * rng.uniform(.85, 1.2))), "UTILITIES"),
                 (cal.business_in_month(y, m, 3), -(220_000 if dt.date(y, m, 1) >= CUTOVER else 120_000), "SOFTWARE"),
                 (last, -int(round(c * SHARE_OF_COLLECTIONS["PROFESSIONAL_FEES"])), "PROFESSIONAL_FEES"),
                 (cal.business_in_month(y, m, 12), -int(round(c * SHARE_OF_COLLECTIONS["TAXES"])), "TAXES")]
        for cat, days in (("LAB", (10, 25)), ("SUPPLIES", (8, 22))):
            month += [(cal.business_in_month(y, m, k), -int(round(c * SHARE_OF_COLLECTIONS[cat] / 2)), cat) for k in days]
        meta, google = ads.get((y, m), (0, 0))
        if meta or google:
            month += [(last, -meta, "ADS_META"), (last, -google, "ADS_GOOGLE")]
        if period(dt.date(y, m, 1)) == "agency":
            month.append((cal.business_in_month(y, m, 5), -int(round((meta + google) * AGENCY_FEE_RATIO)), "AGENCY_FEE"))
        month += [(cal.business_in_month(y, m, rng.randint(1, 28)), -rng.randint(15, 60) * 1000, "PERSONAL")
                  for _ in range(2)]
        operating = sum(v for d, v, cat in lines + month if (d.year, d.month) == (y, m) and GROUP[cat] in OPERATING)
        month.append((last, -int(round(max(0, operating * .8), -4)), "OWNER_DRAW"))
        lines += month
    return [l for l in lines if l[1]], card_sales


def statements(lines):
    by_month = defaultdict(list)
    for d, v, cat in lines:
        by_month[d.strftime("%Y-%m")].append((d, v, cat))
    heads, txns, pnl, balance = [], [], [], OPENING_BALANCE
    for n, ym in enumerate(sorted(by_month), 1):
        rows = sorted(by_month[ym], key=lambda l: (l[0], l[2], l[1]))
        closing = balance + sum(v for _, v, _ in rows)
        heads.append({"statement_no": n, "year_month": ym, "opening_balance": balance, "closing_balance": closing})
        txns += [{"statement_no": n, "ordinal": i, "date": d.isoformat(), "amount": v, "category": cat}
                 for i, (d, v, cat) in enumerate(rows, 1)]
        pnl.append({"year_month": ym, "revenue": sum(v for _, v, c in rows if GROUP[c] == "Revenue"),
                    "operating_result": sum(v for _, v, c in rows if GROUP[c] in OPERATING)})
        balance = closing
    return heads, txns, pnl


# ------------------------------------------------------------------ writing the two EHRs and the rest
LEGACY_STATUS = {"attended": "Atendida", "no_show": "No asiste", "rescheduled": "Cambio de fecha",
                 "cancelled": "Anulada"}
NEW_STATUS = {"attended": "Atendido", "no_show": "Inasistencia", "rescheduled": "Cambio de fecha",
              "cancelled": "Cancelado"}


def _rut(p, dots, system):
    if p["placeholder"]:
        return "11.111.111-1" if dots else "11111111-1"
    dv = p["dv"]
    if system == "new" and p["typo"]:                                   # retyped by hand at migration
        dv = "0" if dv != "0" else "1"
    b = str(p["body"])
    return (f"{b[:-6]}.{b[-6:-3]}.{b[-3:]}-{dv}" if dots else f"{b}-{dv}")


def _legacy_name(rng, s):
    """The legacy EHR stored the provider as free text, typed differently each time."""
    title, surname = s[2], s[1]
    return rng.choice([f"{title} {surname}".strip(), surname, surname.upper(), f"{surname} {title}".strip()])


def write(clinic, out, weeks, lines, card_sales):
    rng = clinic.rng
    j = lambda *p: os.path.join(out, *p)
    _write_csv(j("providers.csv"), [
        {"provider_id": s[0], "aliases": strip_accents(s[1]).lower(), "role": s[3], "specialty": s[4],
         "new_ehr_id": s[5], "active": 1} for s in STAFF])
    _write_csv(j("bank_categories.csv"), [{"category": c, "label": l, "pnl_group": g, "sort_order": i}
                                           for i, (c, l, g) in enumerate(BANK_CATEGORIES, 1)])
    _write_csv(j("procedures.csv"), [{"code": c, "name": n, "category": cat, "list_price": p, "minutes": mi}
                                      for c, n, cat, p, mi in PROCEDURES])
    with open(j("holidays.txt"), "w", encoding="utf-8") as fh:
        fh.write("# fixed-date national holidays only (moveable ones omitted in this synthetic set)\n")
        fh.writelines(f"{h}\n" for h in sorted(clinic.cal.holidays))

    # --- legacy EHR: CSV exports, dd/mm/yyyy, amounts with thousands dots, providers as free text
    legacy_pat = [p for p in clinic.patients if p["registered"] <= LEGACY_LAST]
    _write_csv(j("legacy_ehr", "patients.csv"), [
        {"patient_no": p["no"], "national_id": _rut(p, True, "legacy"),
         "mobile": rng.choice([f"+56 {m[0]} {m[1:5]} {m[5:]}", m, f"56{m}"]) if (m := p["mobile"]) else "",
         "birth_date": dmy(p["birth"]) if rng.random() > .05 else "", "sex": p["sex"],
         "municipality": p["municipality"], "insurance_plan": p["plan"]} for p in legacy_pat])
    legacy_appts = []
    for a in (a for a in clinic.appointments if a["date"] <= LEGACY_LAST):
        legacy_appts.append({"appt_no": a["n"], "date": dmy(a["date"]), "start": hhmm(a["start"]),
                             "end": hhmm(a["start"] + a["dur"]), "dentist": _legacy_name(rng, a["provider"]),
                             "patient_no": a["patient"]["no"], "status": LEGACY_STATUS[a["status"]],
                             "chair": a["chair"], "booked_by": a["booked_by"], "booked_on": dmy(a["booked_on"])})
    # the legacy agenda also exports the daily lunch block of each chair as an appointment
    blocks, d, n = [], START, len(clinic.appointments)
    while d <= LEGACY_LAST:
        if clinic.cal.is_business(d):
            for chair in ("1", "2"):
                n += 1
                blocks.append({"appt_no": n, "date": dmy(d), "start": "13:30", "end": "14:30",
                               "dentist": "Dra. Canelo", "patient_no": "", "status": "Bloqueo horario",
                               "chair": chair, "booked_by": "staff", "booked_on": dmy(d)})
        d += dt.timedelta(days=1)
    _write_csv(j("legacy_ehr", "appointments.csv"), legacy_appts + blocks)
    legacy_pay = [p for p in clinic.payments if p["date"] <= LEGACY_LAST]
    _write_csv(j("legacy_ehr", "payments.csv"), [
        {"payment_no": p["n"], "date": dmy(p["date"]), "amount": f"{p['amount']:,}".replace(",", "."),
         "method": p["method"], "patient_no": p["patient"]["no"],
         "received_by": _legacy_name(rng, p["provider"]), "voided": int(p["voided"])} for p in legacy_pay])

    # --- new EHR: API-shaped JSON, ISO dates, numeric ids; holds a migrated copy of every legacy patient
    new_id = lambda p: 100_000 + p["no"]
    _write_json(j("new_ehr", "patients.json"), [
        {"id": new_id(p), "rut": _rut(p, False, "new"), "celular": f"+56{p['mobile']}" if p["mobile"] else "",
         "fecha_nacimiento": p["birth"].isoformat(), "sexo": p["sex"], "comuna": p["municipality"],
         "prevision": p["plan"], "fecha_registro": p["registered"].isoformat()} for p in clinic.patients])
    _write_json(j("new_ehr", "appointments.json"), [
        {"id": 500_000 + a["n"], "fecha": a["date"].isoformat(), "hora_inicio": hhmm(a["start"]),
         "duracion": a["dur"], "id_profesional": a["provider"][5], "id_paciente": new_id(a["patient"]),
         "sillon": a["chair"], "estado": NEW_STATUS[a["status"]], "canal": a["booked_by"],
         "fecha_creacion": a["booked_on"].isoformat()} for a in clinic.appointments if a["date"] >= CUTOVER])
    _write_json(j("new_ehr", "payments.json"), [
        {"id": 800_000 + p["n"], "fecha": p["date"].isoformat(), "monto": p["amount"], "medio_pago": p["method"],
         "id_paciente": new_id(p["patient"]), "id_profesional": p["provider"][5], "anulado": p["voided"]}
        for p in clinic.payments if p["date"] >= CUTOVER])

    # --- bank, accounting P&L and the hand-verified legacy total
    heads, txns, pnl = statements(lines)
    _write_csv(j("bank", "statements.csv"), heads)
    _write_csv(j("bank", "transactions.csv"), txns)
    _write_csv(j("accounting", "pnl_monthly.csv"), pnl)
    with open(j("control_figures.json"), "w") as fh:
        json.dump({"legacy_payments_total": sum(p["amount"] for p in legacy_pay if not p["voided"])}, fh)

    # --- card POS, agency CRM, weekly ad report, AI agent dashboard snapshot
    _write_csv(j("card", "sales.csv"), card_sales)
    contacts, agency = [], [a for a in clinic.appointments if a["booked_by"] == "agency"]
    for a in agency:
        p = a["patient"]
        lead = {"created_on": (a["booked_on"] - dt.timedelta(days=rng.randint(0, 2))).isoformat(),
                "lead_source": "meta" if rng.random() < .8 else "google",
                "service_interest": "aesthetics" if a["provider"][0] == "aesthetics-1" else "general"}
        contacts.append({"mobile": p["mobile"] or "", "national_id": _rut(p, True, "crm") if rng.random() < .2 else "",
                         **lead})
        for _ in range(rng.choices([0, 1, 2], weights=[.3, .4, .3])[0]):      # leads that never booked
            contacts.append({"mobile": "9" + "".join(rng.choices("0123456789", k=8)), "national_id": "", **lead})
    _write_csv(j("crm", "contacts.csv"), [{"contact_id": f"c{i}", **c} for i, c in enumerate(contacts, 1)])
    _write_csv(j("marketing", "weekly.csv"), [
        {k: w[k] for k in ("iso_week", "spend_meta", "spend_google", "conversations", "bookings")} for w in weeks])
    per_month = defaultdict(int)
    for a in agency:
        per_month[a["booked_on"].strftime("%Y-%m")] += 1
    bot = []
    for ym in sorted(m for m in per_month if m >= "2025-07"):          # the vendor chatbot, stage 5
        nxt = dt.date(int(ym[:4]) + int(ym[5:]) // 12, int(ym[5:]) % 12 + 1, 1).isoformat()
        conv = int(per_month[ym] * rng.uniform(5, 7))
        bot += [{"year_month": ym, "metric": k, "value": v, "captured_on": nxt, "partial": 0}
                for k, v in (("conversations", conv), ("bookings", per_month[ym]),
                             ("human_handoffs", int(conv * rng.uniform(.12, .18))))]
    _write_csv(j("ai_agent", "monthly.csv"), bot)
    return {"patients": len(clinic.patients), "appointments": len(clinic.appointments) + len(blocks),
            "payments": len(clinic.payments), "bank_lines": len(txns), "crm_contacts": len(contacts)}


def generate(out, seed=42):
    _prepare(out)
    clinic = Clinic(seed)
    clinic.run()
    weeks = marketing_weeks(clinic)
    lines, card_sales = bank_lines(clinic, weeks)
    summary = write(clinic, out, weeks, lines, card_sales)
    print("SYNTHETIC DATA  seed", seed, " ".join(f"{k}={v:,}" for k, v in summary.items()))
    return summary


# ------------------------------------------------------------------ faults for the demo
def inject_faults(out):
    """Three realistic data problems, one per control family. Running the pipeline afterwards must FAIL."""
    path = os.path.join(out, "bank", "transactions.csv")
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    lost = next(i for i, r in enumerate(rows) if r["date"].startswith("2025-03") and r["category"] == "LAB")
    del rows[lost]
    _write_csv(path, rows)

    path = os.path.join(out, "legacy_ehr", "appointments.csv")
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    hire = next(i for i, r in enumerate(rows) if r["date"].endswith("03/2026") and r["patient_no"])
    rows[hire]["dentist"] = "Dra. Tepa"
    _write_csv(path, rows)

    path = os.path.join(out, "new_ehr", "payments.json")
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)["data"]
    next(r for r in rows if r["fecha"].startswith(MIGRATION_CUTOVER[:7]))["monto"] += 1000
    _write_json(path, rows)
    faults = ["a bank line from March 2025 is missing from the statement export",
              "a new hire ('Dra. Tepa') appears in the agenda but not in providers.csv",
              "one July payment was edited in the new EHR after the migration (+1,000)"]
    print("INJECTED FAULTS\n  - " + "\n  - ".join(faults))
    return faults


def main(argv=None):
    from pipeline.common import DATA
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=DATA)
    ap.add_argument("--faults", action="store_true")
    a = ap.parse_args(argv)
    generate(a.out, a.seed)
    if a.faults:
        inject_faults(a.out)


if __name__ == "__main__":
    main()
