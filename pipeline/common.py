"""Shared pieces for every loader: paths, connection, pseudonymisation, normalisers, upsert and load log."""
import contextlib, csv, datetime, hashlib, hmac, json, os, re, secrets, sqlite3, traceback, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.environ.get("CLINIC_DATA", os.path.join(ROOT, "data"))
OUT = os.environ.get("CLINIC_OUT", os.path.join(ROOT, "output"))

# The legacy EHR ran until June 2026; the new EHR imported a copy of that history. Before the cut-over
# the legacy system is the source of truth, from the cut-over on the new one (see docs/decisions.md #3).
MIGRATION_CUTOVER = "2026-07-01"


def data_path(*p):
    return os.path.join(DATA, *p)


def read_csv(*p):
    """Rows of a UTF-8 CSV under data/, or [] if the file does not exist (a missing source is not a crash)."""
    path = data_path(*p)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def read_json(*p):
    path = data_path(*p)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return doc.get("data", doc) if isinstance(doc, dict) else doc


def connect(db=None):
    os.makedirs(OUT, exist_ok=True)
    con = sqlite3.connect(db or os.path.join(OUT, "clinic.db"))
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as fh:
        con.executescript(fh.read())
    return con


# ------------------------------------------------------------------ pseudonymisation (Law 21.719)
# The salt never leaves the machine that runs the pipeline. Losing it changes every key: the database
# can be rebuilt, but saved reports that filter by key stop matching.
def _salt():
    path = os.path.join(OUT, ".salt")
    if not os.path.exists(path):
        os.makedirs(OUT, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip().encode()


_KEY = None


def _h(text):
    global _KEY
    _KEY = _KEY or _salt()
    return hmac.new(_KEY, text.encode(), hashlib.sha256).hexdigest()[:16]


_PLACEHOLDER_IDS = {"", "0", "1", "11111111", "22222222", "99999999", "12345678"}


def patient_key(national_id):
    """Hash of the national ID body without its check digit, so a mistyped check digit still maps to the
    same patient. Placeholders that front desks type when they don't have the ID return None."""
    if not national_id:
        return None
    s = re.sub(r"[^0-9kK-]", "", str(national_id)).upper()
    body = s.split("-")[0] if "-" in s else s[:-1]
    body = body.lstrip("0")
    if body in _PLACEHOLDER_IDS or not body.isdigit():
        return None
    return "p" + _h("id:" + body)


def phone_key(phone):
    """Hash of a Chilean mobile normalised to +569XXXXXXXX. None for landlines and junk."""
    d = re.sub(r"\D", "", str(phone or ""))
    if len(d) < 9:
        return None
    d = d[-9:]
    if not d.startswith("9"):
        return None
    return "t" + _h("tel:+56" + d)


# ------------------------------------------------------------------ normalisers
def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")


def tokens(s):
    return set(re.findall(r"[a-z]+", strip_accents(s).lower()))


def integer(v):
    """'40.000' -> 40000 ; '$ 1.250' -> 1250 ; 40000.0 -> 40000 ; None/'' -> 0. Chilean format: dot = thousands."""
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(round(v))
    s = str(v).strip().replace(".", "").replace("$", "").replace(" ", "").replace(",", ".")
    try:
        return int(round(float(s)))
    except ValueError:
        return 0


def iso(v):
    """'dd/mm/yyyy' or 'yyyy-mm-dd …' -> 'yyyy-mm-dd' (or None)."""
    if v is None or v == "":
        return None
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()[:10]
    s = str(v).strip()
    if m := re.match(r"(\d{4})-(\d{2})-(\d{2})", s):
        return "-".join(m.groups())
    if m := re.match(r"(\d{2})/(\d{2})/(\d{4})", s):
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def minutes(hhmm):
    m = re.match(r"(\d{1,2}):(\d{2})", str(hhmm or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def days_between(a, b):
    if not a or not b:
        return None
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days


# ------------------------------------------------------------------ providers
# Staff live in data/providers.csv, never in code. `aliases` holds the words each EHR uses for that person
# (free text in the legacy export, an id in the new one); a provider matches when all its aliases appear.
_PROVIDERS = None


def providers():
    global _PROVIDERS
    if _PROVIDERS is None:
        _PROVIDERS = read_csv("providers.csv")
    return _PROVIDERS


def provider_id(name=None, new_ehr_id=None):
    """Resolve a free-text name or a new-EHR id to the stable slug. Unknown -> 'unknown:<words>', which the
    validation step reports so the new person can be added to providers.csv."""
    if new_ehr_id not in (None, ""):
        for p in providers():
            if p.get("new_ehr_id") == str(new_ehr_id):
                return p["provider_id"]
        return f"unknown:new-{new_ehr_id}"
    t = tokens(name)
    if not t:
        return None
    for p in providers():
        if tokens(p["aliases"]) <= t:
            return p["provider_id"]
    return "unknown:" + "-".join(sorted(t))[:40]


# ------------------------------------------------------------------ appointment status
# One vocabulary for both EHRs. The appointment state is the source of truth, not a vendor's separate
# "attendance" field, which is only filled when the front desk remembers to mark the patient in.
# Patterns match the Spanish states both vendors export.
_STATUSES = [
    ("block", r"^bloqueo"),
    ("attended", r"^atendid|atendiendose|sala de espera|lista de espera|ingresada a box"),
    ("no_show", r"no asiste|inasistencia"),
    ("rescheduled", r"cambio de fecha"),
    ("cancelled", r"anulad|cancelad"),
    ("booked", r"agendad|confirmad|notificad|no confirmado|reserva"),
]


def appointment_status(source_status):
    s = strip_accents(source_status or "").lower().strip()
    for status, pattern in _STATUSES:
        if re.search(pattern, s):
            return status
    return "other"


def in_series(source, date):
    """1 if this row belongs to the unified series: legacy before the cut-over, new EHR from it."""
    if not date:
        return 0
    return int(date < MIGRATION_CUTOVER) if source == "legacy" else int(date >= MIGRATION_CUTOVER)


# ------------------------------------------------------------------ writing
def upsert(con, table, rows, pk):
    """INSERT … ON CONFLICT DO UPDATE. Never deletes. Returns the number of rows processed."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    pks = [pk] if isinstance(pk, str) else list(pk)
    upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in pks)
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT({', '.join(pks)}) DO " + (f"UPDATE SET {upd}" if upd else "NOTHING"))
    con.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


@contextlib.contextmanager
def load_log(con, source):
    """Record every load in etl_load. An error in one source rolls back that source only."""
    cur = con.execute("INSERT INTO etl_load (source, started, status) VALUES (?, ?, 'running')", (source, _now()))
    rid, state = cur.lastrowid, {"rows": 0, "detail": ""}
    con.commit()
    try:
        yield state
        con.execute("UPDATE etl_load SET finished=?, rows=?, status=?, detail=? WHERE id=?",
                    (_now(), state["rows"], "ok" if state["rows"] else "skipped", state["detail"], rid))
        con.commit()
        print(f"  [{'ok' if state['rows'] else 'skip'}] {source}: {state['rows']:,} rows  {state['detail']}")
    except Exception as e:
        con.rollback()
        con.execute("UPDATE etl_load SET finished=?, status='error', detail=? WHERE id=?",
                    (_now(), f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}", rid))
        con.commit()
        print(f"  [ERROR] {source}: {type(e).__name__}: {e}")
