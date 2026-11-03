"""One command, end to end: generate the synthetic clinic, rebuild the warehouse from scratch, run the controls.

    python run.py              # seed 42
    python run.py --seed 7     # another synthetic clinic with the same calibration
    python run.py --faults     # inject three data faults: the controls must catch them (exit code 1),
                               # and the dashboard refuses to build
"""
import argparse, os, sys, time
from dashboard import build as dashboard
from pipeline import build
from pipeline.common import DATA, OUT
from synthetic import generate


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--faults", action="store_true", help="inject data faults the controls must catch")
    ap.add_argument("--no-export", action="store_true", help="skip the CSV export for BI")
    a = ap.parse_args(argv)
    t0 = time.time()
    generate.generate(DATA, a.seed)
    if a.faults:
        generate.inject_faults(DATA)
    # the database is derived (decision 5): rebuild it from scratch, keeping the salt
    db = os.path.join(OUT, "clinic.db")
    for path in (db, db + "-wal", db + "-shm"):
        if os.path.exists(path):
            os.remove(path)
    print()
    code = build.main(["--no-export"] if a.no_export else [])
    print()
    dashboard.main([])                       # refuses to build when a control fails
    print(f"\n{'All controls passed' if code == 0 else 'CONTROLS FAILED: reports blocked'} "
          f"({time.time() - t0:.1f} s). Warehouse: output/clinic.db")
    return code


if __name__ == "__main__":
    sys.exit(main())
