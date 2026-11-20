"""/mesh-status — live status of the meshlab exp2 4-node experiment.

Reads ~/mesh/exp2.db (written by meshlab's exp2.py) READ-ONLY.
Shows per-node delivery, signal and battery, plus the latest sends.
Node positions are deliberately NOT shown (the T1000 sits in David's car).
"""
import sqlite3
from datetime import datetime, timedelta

from flask import Blueprint, render_template

bp = Blueprint("mesh_status", __name__)
DB = "/home/david/mesh/exp2.db"
NODES = ["T1000", "Rak1", "Rak2"]
RELAY = {0x30: "H4", 0x09: "T1000", 0x02: "Rak1", 0xbf: "Rak2"}  # last byte of node ID


def _route(ok, hops, relay):
    if not ok:
        return "–"
    if not hops:
        return "direct"
    return f"via {RELAY.get((relay or 0) & 0xFF, '?')}" + (f" ({hops} hops)" if hops > 1 else "")


def _q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


@bp.route("/mesh-status")
def mesh_status():
    now = datetime.now()
    h1 = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    h24 = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    nodes, recent, cycles, err = [], [], 0, None
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        cycles = _q(con, "SELECT COUNT(DISTINCT cycle) FROM sends WHERE ts >= ?", (h24,))[0][0]
        for name in NODES:
            def rate(since):
                n, d = _q(con, "SELECT COUNT(*), COALESCE(SUM(delivered),0) FROM sends "
                               "WHERE dest_name=? AND ts>=?", (name, since))[0]
                return (round(100 * d / n) if n else None), n
            r1, n1 = rate(h1)
            r24, n24 = rate(h24)
            last_ok = _q(con, "SELECT MAX(ts) FROM sends WHERE dest_name=? AND delivered=1", (name,))[0][0]
            snr, lat = _q(con, "SELECT AVG(ack_snr), AVG(latency_s) FROM sends "
                               "WHERE dest_name=? AND delivered=1 AND ts>=?", (name, h24))[0]
            batt = _q(con, "SELECT batt, volt FROM sends WHERE dest_name=? AND batt IS NOT NULL "
                           "ORDER BY ts DESC LIMIT 1", (name,))
            age_min = None
            if last_ok:
                age_min = int((now - datetime.fromisoformat(last_ok)).total_seconds() // 60)
            status = ("ok" if age_min is not None and age_min <= 10 else
                      "warn" if age_min is not None and age_min <= 60 else "down")
            nodes.append(dict(
                name=name, status=status, r1=r1, n1=n1, r24=r24, n24=n24,
                last_ok=last_ok[11:16] if last_ok else "never", age_min=age_min,
                snr=round(snr, 1) if snr is not None else None,
                lat=round(lat, 1) if lat is not None else None,
                batt=batt[0][0] if batt else None,
                volt=round(batt[0][1], 2) if batt and batt[0][1] else None))
        for ts, name, ok, reason, snr, lat, hops, relay in _q(
                con, "SELECT ts, dest_name, delivered, reason, ack_snr, latency_s, ack_hops, relay_node "
                     "FROM sends ORDER BY ts DESC LIMIT 30"):
            recent.append(dict(time=ts[11:19], name=name, ok=ok, reason=reason,
                               snr=snr, lat=lat, route=_route(ok, hops, relay)))
        con.close()
    except Exception as e:
        err = str(e)
    return render_template("mesh_status.html", nodes=nodes, recent=recent, cycles=cycles,
                           err=err, updated=now.strftime("%H:%M:%S"))
