"""LoRa coverage map page: /meshmap"""
import sqlite3, time
from bisect import bisect_left
from datetime import datetime
from flask import Blueprint, render_template

bp = Blueprint("meshmap", __name__)
DB = "/home/david/mesh/meshmap.db"
STATION = [36.0860416, -80.2477816]
EXACT = 120      # s: position this close in time counts as measured
MAX_SPAN = 3600  # s: max gap to interpolate across

def locate(ts, pos, ts_list):
    i = bisect_left(ts_list, ts)
    b = pos[i - 1] if i > 0 else None
    a = pos[i] if i < len(pos) else None
    near = min((p for p in (b, a) if p), key=lambda p: abs(p[0] - ts), default=None)
    if near is None:
        return None
    if abs(near[0] - ts) <= EXACT:
        return near[1], near[2], False
    if b and a and a[0] - b[0] <= MAX_SPAN:
        f = (ts - b[0]) / (a[0] - b[0])
        return b[1] + f * (a[1] - b[1]), b[2] + f * (a[2] - b[2]), True
    if abs(near[0] - ts) <= MAX_SPAN / 2:
        return near[1], near[2], True
    return None

@bp.route("/meshmap")
def meshmap():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cols = [r[1] for r in con.execute("PRAGMA table_info(sends)")]
    extra = "snr, rssi, msg" if "msg" in cols else "NULL, NULL, NULL"
    sends = con.execute(f"SELECT seq, ts_sent, status, {extra} FROM sends ORDER BY seq").fetchall()
    pos = con.execute("SELECT ts, AVG(lat), AVG(lon) FROM positions GROUP BY ts ORDER BY ts").fetchall()
    con.close()
    ts_list = [p[0] for p in pos]
    now = time.time()
    points = []
    for seq, ts, status, snr, rssi, msg in sends:
        if status == "pending":
            if now - ts < 180:
                continue
            status = "fail"
        loc = locate(ts, pos, ts_list)
        if loc is None:
            continue
        lat, lon, est = loc
        points.append(dict(seq=seq, status=status, lat=lat, lon=lon, est=est,
                           snr=snr, rssi=rssi, msg=msg or "",
                           time=datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")))
    track = [dict(lat=p[1], lon=p[2]) for p in pos[-3000:]]
    return render_template("meshmap.html", station=STATION, points=points, track=track,
                           n_ack=sum(p["status"] == "ack" for p in points),
                           n_fail=sum(p["status"] != "ack" for p in points),
                           n_track=len(pos), updated=datetime.now().strftime("%H:%M"))
