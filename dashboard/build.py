"""Management dashboard and weekly pulse: the seven questions the owner answers every Monday, from the warehouse.

    python -m dashboard.build [--week 2026-W44]

Without --week it uses the last week with attended visits. Writes:
  output/dashboard/index.html   self-contained page (no scripts, fonts or data from the internet)
  output/pulse/<week>.md        the seven questions with their numeric answers, for the Monday review
Aggregates only: no patient key reaches either file. If any control fails nothing is written
(decision 6: controls before reports). Targets are illustrative; edit TARGETS.
"""
import argparse, contextlib, datetime as dt, html, io, os, sqlite3, sys
from pipeline import validate
from pipeline.common import OUT

TARGETS = {"collections_month": 13_000_000, "no_show": .10, "utilisation": .60, "ad_cost_per_new_patient": 20_000,
           "card_gap": .06}                # one business day of payouts crossing a month end is ~5 %
CAPACITY_WEEK_H = 90                     # 2 chairs x 5 days x 9 h (09:00-19:00 minus the lunch block)
WEEKLY_PACE = TARGETS["collections_month"] * 12 / 52
WEEKS = MONTHS = 13
CHANNELS = [("staff", "Clinic staff"), ("agency", "Agency"), ("ai_agent", "AI agent"), ("online", "Online")]
MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


# ------------------------------------------------------------------ data
def default_week(con):
    last = con.execute("SELECT MAX(date) FROM v_appointment WHERE status = 'attended'").fetchone()[0]
    return con.execute("SELECT iso_week FROM dim_date WHERE date = ?", (last,)).fetchone()[0]


def week_rows(con, week, n):
    rows = con.execute("""SELECT DISTINCT iso_week, week_monday FROM dim_date
                          WHERE week_monday <= (SELECT MIN(week_monday) FROM dim_date WHERE iso_week = ?)
                          ORDER BY week_monday DESC LIMIT ?""", (week, n)).fetchall()
    return list(reversed(rows))


def weekly(con, weeks):
    names = [w for w, _ in weeks]
    marks = ",".join("?" * len(names))
    base = {r[0]: r for r in con.execute(
        f"SELECT iso_week, attended, no_show, no_show_rate, new_patients, collections, spend_meta, spend_google, "
        f"ad_cost_per_new_patient FROM v_weekly WHERE iso_week IN ({marks})", names)}
    hours = dict(con.execute(f"""SELECT d.iso_week, SUM(a.duration_min) / 60.0 FROM v_appointment a
                                 JOIN dim_date d ON d.date = a.date WHERE a.status = 'attended'
                                 AND d.iso_week IN ({marks}) GROUP BY 1""", names))
    booked = {}
    for w, by, n in con.execute(f"""SELECT d.iso_week, a.booked_by, COUNT(*) FROM v_appointment a
                                    JOIN dim_date d ON d.date = a.booked_on WHERE d.iso_week IN ({marks})
                                    GROUP BY 1, 2""", names):
        booked.setdefault(w, {})[by] = n
    out = []
    for w, monday in weeks:
        r = base.get(w) or (w,) + (None,) * 8
        h = hours.get(w)
        out.append({"week": w, "monday": monday, "attended": r[1], "no_show": r[2], "no_show_rate": r[3],
                    "new_patients": r[4], "collections": r[5], "ad_spend": (r[6] or 0) + (r[7] or 0),
                    "ad_cost": r[8], "hours": h, "utilisation": h / CAPACITY_WEEK_H if h else None,
                    "booked": booked.get(w, {})})
    return out


def monthly(con, last_month, n):
    months = [m for (m,) in con.execute("""SELECT DISTINCT year_month FROM dim_date WHERE year_month <= ?
                                           ORDER BY year_month DESC LIMIT ?""", (last_month, n))][::-1]
    marks = ",".join("?" * len(months))
    sched = {r[0]: r[1:] for r in con.execute(
        f"""SELECT year_month, SUM(attended), SUM(no_show), SUM(attended) + SUM(no_show), SUM(hours_attended)
            FROM v_schedule_monthly WHERE year_month IN ({marks}) GROUP BY 1""", months)}
    pnl = {r[0]: r[1:] for r in con.execute(
        f"SELECT year_month, revenue, operating_result, marketing FROM v_pnl_monthly WHERE year_month IN ({marks})",
        months)}
    cash = dict(con.execute(f"SELECT year_month, SUM(amount) FROM v_collections_monthly "
                            f"WHERE year_month IN ({marks}) GROUP BY 1", months))
    booked = {}
    for m, by, k in con.execute(f"SELECT year_month, booked_by, COUNT(*) FROM v_appointment "
                                f"WHERE year_month IN ({marks}) GROUP BY 1, 2", months):
        booked.setdefault(m, {})[by] = k
    out = []
    for m in months:
        s, p = sched.get(m, (None,) * 4), pnl.get(m, (None,) * 3)
        out.append({"month": m, "attended": s[0], "no_show_rate": s[1] / s[2] if s[2] else None, "hours": s[3],
                    "revenue": p[0], "operating_result": p[1], "marketing": -p[2] if p[2] else None,
                    "collections": cash.get(m), "booked": booked.get(m, {})})
    return out


def lead_time(con):
    rows = con.execute("SELECT booked_by, lead_time_band, base, attendance_rate FROM v_attendance_by_lead_time "
                       "WHERE lead_time_band <> 'unknown' ORDER BY lead_time_band").fetchall()
    bands = sorted({r[1] for r in rows})
    return bands, {(r[0], r[1]): (r[2], r[3]) for r in rows}


def by_provider(con, month):
    return con.execute("""SELECT p.provider_id, d.specialty, SUM(p.amount), COUNT(*) FROM v_payment p
                          LEFT JOIN dim_provider d USING (provider_id) WHERE p.year_month = ?
                          GROUP BY 1, 2 ORDER BY 3 DESC""", (month,)).fetchall()


def card_recon(con, last_month, n=3):
    return con.execute("""SELECT year_month, pos_net, bank_card_payout, difference FROM v_card_reconciliation
                          WHERE year_month <= ? ORDER BY year_month DESC LIMIT ?""", (last_month, n)).fetchall()[::-1]


def freshness(con):
    out = dict(con.execute("SELECT 'EHR (' || source || ')', MAX(date) FROM fact_appointment GROUP BY source"))
    out["Bank"] = con.execute("SELECT MAX(date) FROM fact_bank_transaction").fetchone()[0]
    out["Card POS"] = con.execute("SELECT MAX(date) FROM fact_card_sale").fetchone()[0]
    return out


def load_errors(con):
    return [r[0] for r in con.execute("""SELECT source FROM etl_load WHERE status = 'error'
                                         AND id IN (SELECT MAX(id) FROM etl_load GROUP BY source)""")]


# ------------------------------------------------------------------ formatting
def money(v):
    if v is None:
        return "–"
    return f"CLP {v / 1e6:.1f}M" if abs(v) >= 1e6 else f"CLP {v:,.0f}"


def pct(v, d=0):
    return "–" if v is None else f"{v * 100:.{d}f} %"


def num(v, d=0):
    return "–" if v is None else f"{v:,.{d}f}"


def week_label(monday):
    d = dt.date.fromisoformat(monday)
    return f"{MON[d.month - 1]} {d.day}"


def month_label(ym):
    return f"{MON[int(ym[5:]) - 1]} ’{ym[2:4]}"


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


STATUS = {"good": ("✓", "On target"), "warning": ("▲", "Close"), "critical": ("✕", "Off target"),
          "neutral": ("•", "For context")}


def status(value, target, lower_is_better=False, tolerance=.15):
    if value is None:
        return "neutral"
    if (value <= target) if lower_is_better else (value >= target):
        return "good"
    near = value <= target * (1 + tolerance) if lower_is_better else value >= target * (1 - tolerance)
    return "warning" if near else "critical"


# ------------------------------------------------------------------ the seven questions
def questions(weeks, recon, fresh, errors, controls, week_end):
    s, prev = weeks[-1], weeks[-5:-1]
    p4 = lambda k: avg([w[k] for w in prev])
    booked = s["booked"]
    total = sum(booked.values())
    alarms = [f"load failed: {e}" for e in errors]
    alarms += [f"control failed: {name}" for name, ok, _ in controls if ok is False]
    for source, days in (("EHR (new)", 7), ("Bank", 45), ("Card POS", 45)):
        last = fresh.get(source)
        if not last or (week_end - dt.date.fromisoformat(last)).days > days:
            alarms.append(f"{source} is stale (last record {last or 'never'})")
    if s["collections"] and p4("collections") and s["collections"] < .7 * p4("collections"):
        alarms.append("collections more than 30 % below the 4-week average")
    gap = None
    if recon and recon[-1][1]:
        m, pos, bank, diff = recon[-1]
        gap = abs(diff or 0) / pos if bank is not None else None
        card = (f"{month_label(m)}: the POS paid out {money(pos)} net and the bank received {money(bank)} "
                f"({pct(gap, 1)} apart; payouts cross month ends, so small gaps are timing).")
    else:
        card = "No card data for the last closed month."
    return [
        ("Did enough cash come in?",
         f"Collections {money(s['collections'])} against a break-even pace of {money(WEEKLY_PACE)} a week "
         f"(previous 4-week average {money(p4('collections'))}).",
         status(s["collections"], WEEKLY_PACE)),
        ("Was the chair capacity used?",
         f"{num(s['hours'], 1)} h attended of {CAPACITY_WEEK_H} ({pct(s['utilisation'])}; previous average "
         f"{pct(p4('utilisation'))}; target {pct(TARGETS['utilisation'])}).",
         status(s["utilisation"], TARGETS["utilisation"])),
        ("Did patients show up?",
         f"No-show {pct(s['no_show_rate'], 1)} ({s['no_show'] or 0} of {(s['attended'] or 0) + (s['no_show'] or 0)}); "
         f"target ≤ {pct(TARGETS['no_show'])}; previous average {pct(p4('no_show_rate'), 1)}.",
         status(s["no_show_rate"], TARGETS["no_show"], lower_is_better=True)),
        ("How many new patients, at what ad cost?",
         f"{s['new_patients'] or 0} new patients seen (previous average {num(p4('new_patients'), 1)}). Ad spend per "
         f"new patient {money(s['ad_cost'])} (target ≤ {money(TARGETS['ad_cost_per_new_patient'])}). "
         f"Correlation, not attribution.",
         status(s["ad_cost"], TARGETS["ad_cost_per_new_patient"], lower_is_better=True)),
        ("Who is booking?",
         "Appointments created this week: " + ", ".join(f"{label} {booked.get(k, 0)}" for k, label in CHANNELS
                                                         if booked.get(k)) + f" (total {total}).",
         "neutral"),
        ("Does the card money reach the bank?", card, status(gap, TARGETS["card_gap"], lower_is_better=True)),
        ("Is anything broken or stale?",
         "; ".join(alarms) + "." if alarms else "No alarms: every load ran, all seven controls pass, sources are current.",
         "critical" if alarms else "good"),
    ]


# ------------------------------------------------------------------ SVG charts
W, H, LEFT, TOP, BOTTOM = 420, 170, 6, 20, 26


def _bar(x, y0, y1, bw, tip, cls="s1", top_round=True):
    """A bar from the baseline y0 to y1 with a 4px rounded data end, square at the baseline."""
    up = y1 < y0
    h = abs(y0 - y1)
    r = min(4, h, bw / 2) if top_round else 0
    if up:
        d = f"M{x:.1f},{y0:.1f}V{y1 + r:.1f}Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f}H{x + bw - r:.1f}" \
            f"Q{x + bw:.1f},{y1:.1f} {x + bw:.1f},{y1 + r:.1f}V{y0:.1f}Z"
    else:
        d = f"M{x:.1f},{y0:.1f}V{y1 - r:.1f}Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f}H{x + bw - r:.1f}" \
            f"Q{x + bw:.1f},{y1:.1f} {x + bw:.1f},{y1 - r:.1f}V{y0:.1f}Z"
    return f'<path d="{d}" class="{cls}" data-tip="{html.escape(tip)}"/>'


def _scale(values, extra=()):
    hi = max([v for v in values if v is not None] + list(extra) + [0])
    lo = min([v for v in values if v is not None] + [0])
    span = (hi - lo) or 1
    return lambda v: TOP + (hi - v) / span * (H - TOP - BOTTOM)


def _xlabels(labels, slot, every):
    return "".join(f'<text x="{LEFT + i * slot + slot / 2:.1f}" y="{H - 8}" class="tick" text-anchor="middle">'
                   f'{html.escape(l)}</text>' for i, l in enumerate(labels) if (len(labels) - 1 - i) % every == 0)


def bars(values, labels, fmt, title, target=None, every=2):
    y = _scale(values, [target] if target else ())
    slot = (W - LEFT) / len(values)
    bw = min(24, slot * .6)
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{html.escape(title)}">']
    for i, (v, lab) in enumerate(zip(values, labels)):
        if v is not None:
            out.append(_bar(LEFT + i * slot + (slot - bw) / 2, y(0), y(v), bw, f"{lab}: {fmt(v)}",
                            "s1" if v >= 0 else "s1 neg"))
    if target:
        out.append(f'<line x1="{LEFT}" x2="{W}" y1="{y(target):.1f}" y2="{y(target):.1f}" class="target"/>')
    last = len(values) - 1
    if values[last] is not None:                                        # direct label on the current value only
        v = values[last]
        out.append(f'<text x="{LEFT + last * slot + (slot + bw) / 2:.1f}" y="{(y(v) - 5 if v >= 0 else y(v) + 13):.1f}" '
                   f'class="value" text-anchor="end">{html.escape(fmt(v))}</text>')
    out.append(f'<line x1="{LEFT}" x2="{W}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="base"/>')
    out.append(_xlabels(labels, slot, every) + "</svg>")
    return "".join(out)


def stacked(rows, labels, keys, title, every=2):
    totals = [sum(r.get(k, 0) for k, _ in keys) for r in rows]
    y = _scale(totals)
    slot = (W - LEFT) / len(rows)
    bw = min(24, slot * .6)
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{html.escape(title)}">']
    for i, (r, lab) in enumerate(zip(rows, labels)):
        x, base = LEFT + i * slot + (slot - bw) / 2, 0
        present = [(k, name, n) for (k, name), n in ((kk, r.get(kk[0], 0)) for kk in keys) if n]
        for j, (k, name, n) in enumerate(present):
            y0, y1 = y(base), y(base + n)
            if j:                                                       # 2px surface gap between segments
                y0 -= 2
            if y0 > y1:
                out.append(_bar(x, y0, y1, bw, f"{lab} · {name}: {n}", f"s{keys.index((k, name)) + 1}",
                                top_round=j == len(present) - 1))
            base += n
    out.append(f'<line x1="{LEFT}" x2="{W}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="base"/>')
    out.append(_xlabels(labels, slot, every) + "</svg>")
    return "".join(out)


def grouped(bands, data, series, title):
    y = _scale([1])
    slot = (W - LEFT) / len(bands)
    bw = min(24, slot * .3)
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{html.escape(title)}">']
    for i, band in enumerate(bands):
        x0 = LEFT + i * slot + (slot - 2 * bw - 2) / 2
        for j, (key, name) in enumerate(series):
            base, rate = data.get((key, band), (None, None))
            if rate is None:
                continue
            x = x0 + j * (bw + 2)
            out.append(_bar(x, y(0), y(rate), bw, f"{name}, booked {band[3:]} ahead: {pct(rate)} of {base}", f"s{j + 1}"))
            if key == "agency" and i in (0, len(bands) - 1):              # the story: the cold-lead decay
                out.append(f'<text x="{x + bw / 2:.1f}" y="{y(rate) - 5:.1f}" class="value" '
                           f'text-anchor="middle">{pct(rate)}</text>')
    out.append(f'<line x1="{LEFT}" x2="{W}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="base"/>')
    out.append(_xlabels([b[3:] for b in bands], slot, 1) + "</svg>")
    return "".join(out)


def legend(series):
    return '<div class="legend">' + "".join(f'<span><i class="sw s{i}"></i>{html.escape(n)}</span>'
                                            for i, (_, n) in enumerate(series, 1)) + "</div>"


def table(header, rows):
    th = "".join(f"<th>{html.escape(h)}</th>" for h in header)
    tr = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>'


def panel(title, chart, header, rows, note="", extra="", target=None):
    note = f'<p class="note">{html.escape(note)}</p>' if note else ""
    target = f' <span class="tgt">— dashed line: {html.escape(target)}</span>' if target else ""
    return (f'<section class="panel"><h3>{html.escape(title)}{target}</h3>{extra}{chart}{note}'
            f'<details><summary>Data table</summary>{table(header, rows)}</details></section>')


def chip(state):
    icon, label = STATUS[state]
    return f'<span class="chip {state}"><b aria-hidden="true">{icon}</b>{label}</span>'


def kpi(title, value, detail, state):
    return (f'<div class="kpi"><div class="kpi-t">{html.escape(title)}</div><div class="kpi-v">{html.escape(value)}'
            f'</div><div class="kpi-d">{html.escape(detail)}</div>{chip(state)}</div>')


# ------------------------------------------------------------------ page
CSS = """
:root{color-scheme:light;--bg:#f5f4f1;--surface:#fcfcfb;--line:#e3e2dd;--text:#0b0b0b;--text-2:#52514e;
--muted:#6f6e69;--s1:#2a78d6;--s2:#eb6834;--good:#0ca30c;--warning:#fab219;--critical:#d03b3b;--neutral:#8a8984}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--bg:#111110;
--surface:#1a1a19;--line:#33332f;--text:#f0efec;--text-2:#c3c2b7;--muted:#9a998f;--s1:#3987e5;--s2:#d95926}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111110;--surface:#1a1a19;--line:#33332f;--text:#f0efec;
--text-2:#c3c2b7;--muted:#9a998f;--s1:#3987e5;--s2:#d95926}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 12px}h3{font-size:14px;margin:0 0 8px;color:var(--text-2)}
.sub{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:12px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,200px),1fr));gap:12px}
.panel,.kpi{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.kpi-t{font-size:13px;color:var(--text-2)}.kpi-v{font-size:26px;font-weight:650;margin:2px 0}
.kpi-d{font-size:12px;color:var(--muted);margin-bottom:8px}
.chip{display:inline-flex;gap:6px;align-items:center;font-size:12px;color:var(--text-2)}
.chip b{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;color:#fff;font-size:11px}
.chip.good b{background:var(--good)}.chip.warning b{background:var(--warning);color:#0b0b0b}
.chip.critical b{background:var(--critical)}.chip.neutral b{background:var(--neutral)}
ol.q{margin:0;padding-left:22px}ol.q li{margin:0 0 12px}ol.q .qt{font-weight:600;margin-right:8px}
ol.q p{margin:2px 0 0;color:var(--text-2)}
.chart{width:100%;height:auto;display:block}
.chart path.s1{fill:var(--s1)}.chart path.s2{fill:var(--s2)}.chart path:hover{opacity:.8}
.chart .base{stroke:var(--line);stroke-width:1}.chart .target{stroke:var(--muted);stroke-width:1.5;stroke-dasharray:4 4}
.chart text{font-size:12px;fill:var(--muted)}.chart text.value{fill:var(--text);font-weight:600}
.legend{display:flex;gap:14px;font-size:12px;color:var(--text-2);margin-bottom:4px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.sw.s1{background:var(--s1)}.sw.s2{background:var(--s2)}
.tgt{font-weight:400;color:var(--muted)}.note{font-size:12px;color:var(--muted);margin:6px 0 0}
details{margin-top:8px;font-size:12px}summary{cursor:pointer;color:var(--text-2)}
table{border-collapse:collapse;width:100%;font-size:12px;margin-top:6px}
th,td{text-align:left;padding:4px 6px;border-bottom:1px solid var(--line)}td{font-variant-numeric:tabular-nums}
.wide{overflow-x:auto}
#tip{position:fixed;pointer-events:none;background:var(--text);color:var(--surface);font-size:12px;padding:4px 8px;
border-radius:6px;display:none;z-index:9}
footer{margin-top:32px;font-size:12px;color:var(--muted)}
"""

TIP_JS = """<script>
const tip=document.getElementById('tip');
document.addEventListener('mousemove',e=>{const t=e.target.closest('[data-tip]');
if(!t){tip.style.display='none';return}tip.textContent=t.dataset.tip;tip.style.display='block';
tip.style.left=Math.min(e.clientX+12,innerWidth-tip.offsetWidth-8)+'px';tip.style.top=(e.clientY+14)+'px'});
</script>"""


def render(week, weeks, months, bands, attendance, providers, prov_month, recon, qs, controls):
    s = weeks[-1]
    wl = [week_label(w["monday"]) for w in weeks]
    ml = [month_label(m["month"]) for m in months]
    kpis = "".join([
        kpi("Collections this week", money(s["collections"]), f"pace {money(WEEKLY_PACE)}/week", qs[0][2]),
        kpi("Chair utilisation", pct(s["utilisation"]), f"{num(s['hours'], 1)} of {CAPACITY_WEEK_H} h", qs[1][2]),
        kpi("No-show", pct(s["no_show_rate"], 1), f"target ≤ {pct(TARGETS['no_show'])}", qs[2][2]),
        kpi("New patients", num(s["new_patients"]), f"ad cost {money(s['ad_cost'])} each", qs[3][2]),
    ])
    qhtml = "".join(f'<li><span class="qt">{html.escape(q)}</span>{chip(st)}<p>{html.escape(a)}</p></li>'
                    for q, a, st in qs)
    channels = [c for c in CHANNELS if any(m["booked"].get(c[0]) for m in months)]
    lead_series = [("agency", "Agency"), ("staff", "Clinic staff")]
    weekly_panels = "".join([
        panel("Collections (EHR payments)", bars([w["collections"] for w in weeks], wl, money, "Weekly collections",
                                                 WEEKLY_PACE),
              ["Week", "Collections"], [(w["week"], money(w["collections"])) for w in weeks],
              target=f"break-even pace {money(WEEKLY_PACE)}"),
        panel("No-show rate", bars([w["no_show_rate"] for w in weeks], wl, lambda v: pct(v, 1), "Weekly no-show",
                                   TARGETS["no_show"]),
              ["Week", "No-show", "Base"], [(w["week"], pct(w["no_show_rate"], 1),
                                             (w["attended"] or 0) + (w["no_show"] or 0)) for w in weeks],
              target=f"target {pct(TARGETS['no_show'])}"),
        panel("Chair utilisation", bars([w["utilisation"] for w in weeks], wl, pct, "Weekly utilisation",
                                        TARGETS["utilisation"]),
              ["Week", "Hours", "Utilisation"], [(w["week"], num(w["hours"], 1), pct(w["utilisation"])) for w in weeks],
              target=f"target {pct(TARGETS['utilisation'])}"),
        panel("New patients", bars([w["new_patients"] for w in weeks], wl, num, "Weekly new patients"),
              ["Week", "New patients", "Ad spend"], [(w["week"], w["new_patients"] or 0, money(w["ad_spend"]))
                                                     for w in weeks],
              "First attended visit in the patient's whole history."),
    ])
    monthly_panels = "".join([
        panel("Completed visits", bars([m["attended"] for m in months], ml, num, "Monthly completed visits"),
              ["Month", "Visits"], [(m["month"], m["attended"]) for m in months]),
        panel("No-show rate", bars([m["no_show_rate"] for m in months], ml, lambda v: pct(v, 1), "Monthly no-show",
                                   TARGETS["no_show"]),
              ["Month", "No-show"], [(m["month"], pct(m["no_show_rate"], 1)) for m in months],
              target=f"target {pct(TARGETS['no_show'])}"),
        panel("Cash operating result (bank)", bars([m["operating_result"] for m in months], ml, money,
                                                   "Monthly operating result"),
              ["Month", "Revenue", "Marketing", "Operating result"],
              [(m["month"], money(m["revenue"]), money(m["marketing"]), money(m["operating_result"])) for m in months],
              "Revenue minus operating costs from the bank statement; owner draws shown apart."),
        panel("Who booked the appointments", stacked([m["booked"] for m in months], ml, channels, "Bookings by channel"),
              ["Month"] + [n for _, n in channels], [[m["month"]] + [m["booked"].get(k, 0) for k, _ in channels]
                                                     for m in months], extra=legend(channels)),
    ])
    lead_panel = panel(
        "Attendance by who booked and how far ahead", grouped(bands, attendance, lead_series, "Attendance by lead time"),
        ["Booked by", "Lead time", "Base", "Attended"],
        [(n, b[3:], attendance.get((k, b), (0,))[0], pct(attendance.get((k, b), (None, None))[1]))
         for k, n in lead_series for b in bands],
        "All history. Agency leads booked far ahead rarely came: the finding behind the scheduling redesign.",
        legend(lead_series))
    prov_panel = (f'<section class="panel"><h3>Collections by provider ({month_label(prov_month)})</h3>'
                  + table(["Provider", "Specialty", "Collected", "Payments"],
                          [(p, sp or "", money(a), n) for p, sp, a, n in providers]) + "</section>")
    recon_panel = (f'<section class="panel"><h3>Card POS vs bank</h3>'
                   + table(["Month", "POS net", "Bank payouts", "Difference"],
                           [(month_label(m), money(a), money(b), money(c)) for m, a, b, c in recon]) + "</section>")
    ctrl = "".join(f"<li>{'OK' if ok else 'SKIP' if ok is None else 'FAIL'} · {html.escape(n)}: {html.escape(d)}</li>"
                   for n, ok, d in controls)
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Clinic Ops Dashboard</title>
<style>{CSS}</style></head><body><main>
<h1>Clinic Ops Dashboard</h1>
<div class="sub">Week {week} (from {week_label(s['monday'])}) · generated {generated} · synthetic data · aggregates only, no patient keys</div>
<h2>This week</h2><div class="kpis">{kpis}</div>
<h2>The seven questions</h2><section class="panel"><ol class="q">{qhtml}</ol></section>
<h2>Last {WEEKS} weeks</h2><div class="grid">{weekly_panels}</div>
<h2>Last {MONTHS} months</h2><div class="grid">{monthly_panels}</div>
<h2>Behind the numbers</h2><div class="grid">{lead_panel}{prov_panel}{recon_panel}</div>
<footer><b>Controls</b><ul>{ctrl}</ul>Targets are illustrative. Built by dashboard/build.py from output/clinic.db.</footer>
</main><div id="tip" role="tooltip"></div>{TIP_JS}</body></html>"""


def pulse_md(week, qs):
    lines = [f"# Weekly pulse {week}", "", "Synthetic data. Aggregates only.", ""]
    for i, (q, a, st) in enumerate(qs, 1):
        icon, label = STATUS[st]
        lines += [f"## {i}. {q}", f"**{icon} {label}.** {a}", ""]
    return "\n".join(lines)


def main(argv=()):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week")
    a = ap.parse_args(list(argv))
    con = sqlite3.connect(os.path.join(OUT, "clinic.db"))
    with contextlib.redirect_stdout(io.StringIO()):
        ok, controls = validate.run(con)
    if not ok:
        con.close()
        stale = os.path.join(OUT, "dashboard", "index.html")
        if os.path.exists(stale):                # an old dashboard next to bad data would still be read
            os.remove(stale)
        print("Dashboard NOT built: at least one control fails (decision 6). Fix the data and rerun.")
        return 1
    week = a.week or default_week(con)
    weeks = weekly(con, week_rows(con, week, WEEKS))
    monday = dt.date.fromisoformat(weeks[-1]["monday"])
    this_month = monday.isoformat()[:7]
    closed = (monday.replace(day=1) - dt.timedelta(days=1)).isoformat()[:7]
    months = monthly(con, this_month, MONTHS)
    bands, attendance = lead_time(con)
    recon = card_recon(con, closed)
    qs = questions(weeks, recon, freshness(con), load_errors(con), controls, monday + dt.timedelta(days=6))
    page = render(week, weeks, months, bands, attendance, by_provider(con, this_month), this_month, recon, qs, controls)
    con.close()
    for folder, name, text in ((os.path.join(OUT, "dashboard"), "index.html", page),
                               (os.path.join(OUT, "pulse"), f"{week}.md", pulse_md(week, qs))):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    print(f"dashboard: output/dashboard/index.html · pulse: output/pulse/{week}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
