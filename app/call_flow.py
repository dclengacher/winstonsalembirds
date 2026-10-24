"""
/call-flow — "Who's calling": where the last 7 days of detections came from.

Sankey: status group -> species -> time of day.
  New arrivals: first ever detected within the last 30 days
  Fading out:   this week's count < half its average weekly count over the prior 4 weeks
  Regulars:     everything else
Pure counts from detections_alltime; no model involved.
"""
import sqlite3
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template

bp = Blueprint("call_flow", __name__)

DB_FILE = "/home/david/BirdNET-Pi/scripts/birds.db"
SHOW_TOP = {"new": 5, "reg": 7, "fade": 3}  # species shown individually; rest fold into "Other"
TIME_BUCKETS = [("Dawn", "5–8am", 0, 8), ("Morning", "9–11am", 9, 11),
                ("Midday", "noon–2pm", 12, 14), ("Evening", "3pm+", 15, 23)]


def get_call_flow_data():
    today = date.today()
    wk_start = (today - timedelta(days=7)).isoformat()
    prev_start = (today - timedelta(days=35)).isoformat()
    new_cutoff = (today - timedelta(days=30)).isoformat()

    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT Com_Name, CAST(substr(Time,1,2) AS INTEGER), COUNT(*) FROM detections_alltime "
        "WHERE Date >= ? GROUP BY 1, 2", (wk_start,)).fetchall()
    names = sorted({r[0] for r in rows})
    if not names:
        conn.close()
        return None
    ph = ",".join(["?"] * len(names))
    first_seen = dict(conn.execute(
        f"SELECT Com_Name, MIN(Date) FROM detections_alltime WHERE Com_Name IN ({ph}) GROUP BY 1",
        names).fetchall())
    prev4 = dict(conn.execute(
        f"SELECT Com_Name, COUNT(*) FROM detections_alltime WHERE Com_Name IN ({ph}) "
        f"AND Date >= ? AND Date < ? GROUP BY 1", names + [prev_start, wk_start]).fetchall())
    conn.close()

    totals = {}
    for n, _, c in rows:
        totals[n] = totals.get(n, 0) + c

    def group(n):
        if first_seen.get(n, "") >= new_cutoff:
            return "new"
        if totals[n] < 0.5 * prev4.get(n, 0) / 4:
            return "fade"
        return "reg"

    groups = {n: group(n) for n in names}
    label = {}
    for g in ("new", "reg", "fade"):
        members = sorted([n for n in names if groups[n] == g], key=lambda n: -totals[n])
        shown, rest = members[:SHOW_TOP[g]], members[SHOW_TOP[g]:]
        for n in shown:
            label[n] = n
        other = {"new": "Other new", "reg": "Other regulars", "fade": "Other fading"}[g]
        for n in rest:
            label[n] = f"{other} ({len(rest)})" if len(rest) > 1 else n

    def bucket(h):
        for i, (_, _, lo, hi) in enumerate(TIME_BUCKETS):
            if lo <= h <= hi:
                return i
        return 0

    flows = {}
    for n, h, c in rows:
        key = (label[n], bucket(h))
        flows[key] = flows.get(key, 0) + c
    species = {}
    for n in names:
        s = species.setdefault(label[n], {"name": label[n], "g": groups[n], "n": 0, "members": []})
        s["n"] += totals[n]
        s["members"].append(n)

    order = {"new": 0, "reg": 1, "fade": 2}
    species_list = sorted(species.values(), key=lambda s: (order[s["g"]], s["name"].startswith("Other"), -s["n"]))
    return {
        "since": (today - timedelta(days=7)).strftime("%b %-d"),
        "total": sum(totals.values()),
        "species": species_list,
        "flows": [{"s": s, "t": t, "n": c} for (s, t), c in flows.items()],
        "times": [{"name": a, "sub": b} for a, b, _, _ in TIME_BUCKETS],
    }


@bp.route("/call-flow")
def call_flow():
    page_generated = datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")
    return render_template("call_flow.html", data=get_call_flow_data(), page_generated=page_generated)
