"""Rebuild the warehouse from data/: load every source, create the views, run the controls, export for BI.

    python -m pipeline.build             # everything
    python -m pipeline.build --no-export # skip the CSV export for BI

Exit code 1 if any control fails. The database is derived: deleting output/clinic.db and rerunning gives
the same result (keep output/.salt, or every patient key changes).
"""
import csv, os, sys
from . import load_bank, load_legacy_ehr, load_new_ehr, load_ops, load_reference, validate
from .common import HERE, OUT, connect, load_log

# order matters: reference first, legacy before new EHR (the new one updates patients the legacy inserted)
LOADERS = [
    ("reference", load_reference.load),
    ("legacy_ehr", load_legacy_ehr.load),
    ("new_ehr", load_new_ehr.load),
    ("bank", load_bank.load),
    ("card", load_ops.load_card),
    ("crm", load_ops.load_crm),
    ("marketing", load_ops.load_marketing),
    ("ai_agent", load_ops.load_ai_agent),
]


def export(con, folder):
    os.makedirs(folder, exist_ok=True)
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE (type = 'view') OR (type = 'table' AND name LIKE 'dim_%') ORDER BY name")]
    for name in names:
        cur = con.execute(f"SELECT * FROM {name}")
        with open(os.path.join(folder, f"{name}.csv"), "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([d[0] for d in cur.description])
            w.writerows(cur)
    return len(names)


def main(argv=()):
    con = connect()
    print("LOADS")
    for source, fn in LOADERS:
        with load_log(con, source) as state:
            fn(con, state)
    with open(os.path.join(HERE, "views.sql"), encoding="utf-8") as fh:
        con.executescript(fh.read())
    ok, _ = validate.run(con)
    if "--no-export" not in argv:
        print(f"\nexported {export(con, os.path.join(OUT, 'bi'))} tables/views to output/bi/")
    con.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
