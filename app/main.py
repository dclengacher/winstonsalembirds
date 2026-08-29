import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
import os
import time
from datetime import datetime, timedelta, timezone
import sqlite3
import subprocess
import threading
import re
import json as _json
import requests
from flask import Flask, render_template, make_response, request, Response, jsonify

from airline_codes import AIRLINE_CODES

EBIRD_API_KEY = os.environ["EBIRD_API_KEY"]

from astral import LocationInfo
from astral.sun import sun

LOC = LocationInfo("Winston-Salem", "USA", "America/New_York", 36.0999, -80.2442)

def get_sun_times():
    import pytz
    tz = pytz.timezone("America/New_York")
    s = sun(LOC.observer, date=datetime.now(tz).date(), tzinfo=tz)
    return {
        "sunrise": s["sunrise"].strftime("%H:%M"),
        "sunset": s["sunset"].strftime("%H:%M"),
        "sunrise_pct": (s["sunrise"].hour * 60 + s["sunrise"].minute) / 1440 * 100,
        "sunset_pct": (s["sunset"].hour * 60 + s["sunset"].minute) / 1440 * 100,
        "now_pct": (datetime.now(tz).hour * 60 + datetime.now(tz).minute) / 1440 * 100,
    }

# Synodic-month approximation: days since a known new moon, mod the moon's
# ~29.53-day cycle. No ephemeris/API needed -- accurate to well under a day,
# plenty for a "what does the moon look like tonight" display.
def get_moon_phase():
    import math
    synodic_month = 29.530588861
    known_new_moon = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    days_since = (datetime.now(timezone.utc) - known_new_moon).total_seconds() / 86400
    phase = (days_since % synodic_month) / synodic_month
    illumination = (1 - math.cos(2 * math.pi * phase)) / 2

    if phase < 0.03 or phase > 0.97:
        name = "New Moon"
    elif phase < 0.22:
        name = "Waxing Crescent"
    elif phase < 0.28:
        name = "First Quarter"
    elif phase < 0.47:
        name = "Waxing Gibbous"
    elif phase < 0.53:
        name = "Full Moon"
    elif phase < 0.72:
        name = "Waning Gibbous"
    elif phase < 0.78:
        name = "Last Quarter"
    else:
        name = "Waning Crescent"

    return {
        "illumination_pct": round(illumination * 100),
        "name": name,
        "waxing": phase < 0.5,
    }
EBIRD_LAT = 36.0999
EBIRD_LNG = -80.2442
ebird_sightings = []

def run_ebird_loop():
    global ebird_sightings
    while True:
        try:
            url = f"https://api.ebird.org/v2/data/obs/geo/recent?lat={EBIRD_LAT}&lng={EBIRD_LNG}&back=3&maxResults=5"
            resp = requests.get(url, headers={"X-eBirdApiToken": EBIRD_API_KEY}, timeout=10)
            data = resp.json()
            def _fmt_date(d):
                if not d:
                    return ""
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d %H:%M")
                    return dt.strftime("%-m/%-d/%y %H:%M")
                except ValueError:
                    try:
                        dt = datetime.strptime(d, "%Y-%m-%d")
                        return dt.strftime("%-m/%-d/%y")
                    except ValueError:
                        return d

            ebird_sightings = [
                {"name": o.get("comName"), "loc": o.get("locName", "").split(",")[0], "date": _fmt_date(o.get("obsDt"))}
                for o in data
            ]
            print(f"[{time.strftime('%X')}] eBird Sync -> {len(ebird_sightings)} sightings")
        except Exception as e:
            print(f"[ERROR] eBird fetch error: {e}")
        time.sleep(3600)

threading.Thread(target=run_ebird_loop, daemon=True).start()

AIRCRAFT_JSON_PATH = "/run/readsb/aircraft.json"  # readsb/tar1090 live snapshot
AIRCRAFT_TYPES_PATH = "/home/david/birdnet/data/aircraft_types.json"  # from scripts/build_aircraft_type_db.py
AIRCRAFT_TYPE_NAMES_PATH = "/home/david/birdnet/data/aircraft_type_names.json"  # from scripts/build_aircraft_type_db.py -- type_designator -> human name
PLANES_DB_FILE = "/home/david/birdnet/data/planes.db"
PLANES_POLL_INTERVAL_SECONDS = 20
RECENT_DETECTIONS_WINDOW_MINUTES = 720  # 12 hours -- /api/planes-live history table: a time window, not a row count, so a busy burst can't truncate it to a few minutes
NO_CALLSIGN_LABEL = "(no callsign)"  # shown instead of a raw ICAO hex when flight/registration are both blank -- a bare hex looks enough like a real callsign to mislead
DISPLAY_RANGE_MI = 7.0  # live radar/table are narrowed to this 3D slant range; the poll loop below still logs every aircraft regardless of distance

def _load_aircraft_types():
    try:
        with open(AIRCRAFT_TYPES_PATH) as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        print(f"[WARN] {AIRCRAFT_TYPES_PATH} not found -- run scripts/build_aircraft_type_db.py on the Pi")
        return {}

_aircraft_types = _load_aircraft_types()

def _load_aircraft_type_names():
    try:
        with open(AIRCRAFT_TYPE_NAMES_PATH) as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        print(f"[WARN] {AIRCRAFT_TYPE_NAMES_PATH} not found -- run scripts/build_aircraft_type_db.py on the Pi")
        return {}

_aircraft_type_names = _load_aircraft_type_names()

def _init_planes_db():
    os.makedirs(os.path.dirname(PLANES_DB_FILE), exist_ok=True)
    conn = sqlite3.connect(PLANES_DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plane_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            hex TEXT NOT NULL,
            flight TEXT,
            registration TEXT,
            type_designator TEXT,
            description TEXT,
            category TEXT,
            is_commercial INTEGER NOT NULL,
            airline_name TEXT,
            r_dst REAL,
            r_dir REAL,
            alt_baro INTEGER,
            baro_rate INTEGER,
            gs REAL,
            rssi REAL,
            min_distance_mi REAL
        )
    """)
    # CREATE TABLE IF NOT EXISTS above is a no-op against a table that
    # already exists from before min_distance_mi was added, so backfill the
    # column by hand for older DBs. Existing rows get NULL, which is the
    # correct "unknown" state -- the Recent Detections query's
    # min_distance_mi <= DISPLAY_RANGE_MI filter already excludes NULL rows,
    # same as _within_display_range()'s own None-excludes rule. Any row
    # that's still actively tracked gets min_distance_mi backfilled from its
    # current distance on its next poll (see run_planes_poll_loop).
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(plane_detections)").fetchall()}
    if "min_distance_mi" not in existing_cols:
        conn.execute("ALTER TABLE plane_detections ADD COLUMN min_distance_mi REAL")
    # detected_at is range-queried on every /api/planes-live poll (recent
    # detections window, now up to 12h) and every dashboard load (24h hourly
    # counts) -- index it so those stay cheap as the table grows.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plane_detections_detected_at ON plane_detections(detected_at)")
    conn.commit()
    conn.close()

_init_planes_db()

def _classify_flight(flight):
    # Commercial ICAO callsigns are a 3-letter operator code + flight number
    # (e.g. "DAL1423"). General-aviation tail numbers ("N123AA") don't match
    # that shape and fall through to (False, None) by default.
    callsign = (flight or "").strip().upper()
    m = re.match(r"^([A-Z]{3})\d", callsign)
    if m and m.group(1) in AIRLINE_CODES:
        return True, AIRLINE_CODES[m.group(1)]
    return False, None

NM_TO_MI = 1.150779448  # nautical miles -> statute miles
FT_TO_MI = 1.0 / 5280.0  # feet -> statute miles

def _slant_range_mi(r_dst_nm, alt_baro_ft):
    """True 3D straight-line distance in statute miles: sqrt(ground^2 +
    altitude^2), both converted to the same unit first -- ground distance
    (r_dst, nautical miles) and altitude (alt_baro, feet) arrive in
    different units from readsb, so a bare ground-only r_dst badly
    understates real range for anything at cruise altitude.
    None if either input is missing -- we'd rather exclude an aircraft from
    the DISPLAY_RANGE_MI filter than guess whether it's actually in range
    without real altitude data.

    Defined up here (rather than down by its other display-side callers)
    because run_planes_poll_loop() below also needs it to track
    min_distance_mi, and that loop's background thread starts at import
    time -- if this were defined later in the module, the thread's first
    iteration could run before that definition executes and hit a
    NameError."""
    if r_dst_nm is None or alt_baro_ft is None:
        return None
    import math
    ground_mi = r_dst_nm * NM_TO_MI
    alt_mi = alt_baro_ft * FT_TO_MI
    return math.sqrt(ground_mi ** 2 + alt_mi ** 2)

def _within_display_range(distance_mi):
    """Shared by get_live_aircraft_snapshot() (radar) and the Recent
    Detections table's inclusion rule, so both agree on the same threshold
    and computation. The table applies this to min_distance_mi (closest
    approach ever seen for that row) via SQL rather than calling this
    function directly, but it's the same "distance_mi is not None and
    distance_mi <= DISPLAY_RANGE_MI" rule either way."""
    return distance_mi is not None and distance_mi <= DISPLAY_RANGE_MI

_active_aircraft_hexes = set()
# hex -> id of that hex's currently-open plane_detections row (the row
# inserted when it was last added to _active_aircraft_hexes). Lets each
# later poll UPDATE the same row by primary key instead of re-querying
# "the most recent row for this hex" every cycle. Pruned in lockstep with
# _active_aircraft_hexes below; repopulated on INSERT via cur.lastrowid.
_active_aircraft_row_ids = {}

def run_planes_poll_loop():
    # Same shape as run_ebird_loop -- a daemon thread on its own poll
    # interval, wrapped in try/except so one bad read doesn't kill it.
    # Difference from eBird: this one writes to a database, so it needs to
    # track what was already active last cycle to only log genuinely new
    # detections rather than re-logging every still-visible aircraft.
    #
    # Aircraft that are still active (already logged earlier this same
    # tracking session) get their existing row's live/positional columns
    # UPDATEd in place every cycle instead of left untouched, so the stored
    # row keeps reflecting where the aircraft currently is rather than
    # freezing at wherever it happened to be at first detection. detected_at
    # is deliberately left alone -- it stays the original first-detection
    # timestamp.
    #
    # min_distance_mi tracks the closest approach ever seen for that row
    # (the true 3D slant range, same computation the display side uses) --
    # it only ever moves down, never back up as the aircraft drifts away
    # again. This is what the Recent Detections table's range filter checks
    # (see get_planes_live_payload), deliberately decoupled from the
    # r_dst/alt_baro columns above: those keep reflecting the LATEST
    # position (for the table's displayed Distance/Altitude/etc. values),
    # while min_distance_mi remembers the best-ever distance so a plane
    # that legitimately came within DISPLAY_RANGE_MI doesn't vanish from
    # the table the moment it flies back out past the threshold (or
    # flicker in/out from ordinary position noise near the boundary).
    global _active_aircraft_hexes, _active_aircraft_row_ids
    while True:
        try:
            with open(AIRCRAFT_JSON_PATH) as f:
                snapshot = _json.load(f)
            current = snapshot.get("aircraft", [])
            current_hexes = {a["hex"] for a in current if a.get("hex")}
            newly_seen = [a for a in current if a.get("hex") and a["hex"] not in _active_aircraft_hexes]
            still_active = [a for a in current if a.get("hex") and a["hex"] in _active_aircraft_hexes]

            if newly_seen or still_active:
                conn = sqlite3.connect(PLANES_DB_FILE)

                if newly_seen:
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    for a in newly_seen:
                        hex_code = a["hex"]
                        flight = (a.get("flight") or "").strip() or None
                        # Our merged tar1090-db lookup is authoritative when it
                        # has the hex; readsb's own r/t fields (when present)
                        # fill the gap otherwise. Some hex entries have a
                        # type_designator but no description (e.g. "C68A" with
                        # nothing friendly to show) -- the type-designator name
                        # lookup (keyed differently, built by the same script)
                        # fills that gap as a second-tier fallback.
                        lookup = _aircraft_types.get(hex_code, {})
                        registration = lookup.get("registration") or a.get("r")
                        type_designator = lookup.get("type_designator") or a.get("t")
                        description = lookup.get("description") or (
                            _aircraft_type_names.get(type_designator) if type_designator else None
                        )
                        is_commercial, airline_name = _classify_flight(flight)
                        # No distance filtering here, intentionally: the live
                        # page's radar/table are narrowed to DISPLAY_RANGE_MI,
                        # but this logging loop keeps recording every aircraft
                        # it sees regardless of distance, so the research
                        # dataset keeps growing with full-range data.
                        # min_distance_mi starts out equal to this first
                        # reading -- there's no earlier distance to compare
                        # against yet.
                        distance_mi = _slant_range_mi(a.get("r_dst"), a.get("alt_baro"))
                        cur = conn.execute(
                            """INSERT INTO plane_detections
                               (detected_at, hex, flight, registration, type_designator, description,
                                category, is_commercial, airline_name, r_dst, r_dir, alt_baro, baro_rate, gs, rssi, min_distance_mi)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (now_str, hex_code, flight, registration, type_designator, description,
                             a.get("category"), int(is_commercial), airline_name,
                             a.get("r_dst"), a.get("r_dir"), a.get("alt_baro"),
                             a.get("baro_rate"), a.get("gs"), a.get("rssi"), distance_mi),
                        )
                        _active_aircraft_row_ids[hex_code] = cur.lastrowid
                    print(f"[{time.strftime('%X')}] Planes poll -> {len(newly_seen)} newly detected ({len(current_hexes)} active)")

                if still_active:
                    # Same distance-unfiltered intent as the insert above --
                    # this refreshes the live columns regardless of range;
                    # only the display-side query filters by distance. One
                    # UPDATE per already-active aircraft, targeted by the
                    # row id cached at insert time (no per-row SELECT).
                    #
                    # min_distance_mi is a ratchet: the CASE only lowers it
                    # (new reading closer than what's stored), never raises
                    # it back up as the aircraft moves away again, and
                    # leaves it untouched if this reading's distance is
                    # unknown (missing r_dst/alt_baro). min_distance_mi
                    # IS NULL covers rows from before this column existed --
                    # first live reading after upgrade seeds it.
                    update_params = []
                    for a in still_active:
                        if a["hex"] not in _active_aircraft_row_ids:
                            continue
                        distance_mi = _slant_range_mi(a.get("r_dst"), a.get("alt_baro"))
                        update_params.append((
                            a.get("r_dst"), a.get("r_dir"), a.get("alt_baro"),
                            a.get("baro_rate"), a.get("gs"), a.get("rssi"),
                            distance_mi, distance_mi, distance_mi,
                            _active_aircraft_row_ids[a["hex"]],
                        ))
                    conn.executemany(
                        """UPDATE plane_detections
                           SET r_dst = ?, r_dir = ?, alt_baro = ?, baro_rate = ?, gs = ?, rssi = ?,
                               min_distance_mi = CASE
                                   WHEN ? IS NULL THEN min_distance_mi
                                   WHEN min_distance_mi IS NULL OR ? < min_distance_mi THEN ?
                                   ELSE min_distance_mi
                               END
                           WHERE id = ?""",
                        update_params,
                    )

                conn.commit()
                conn.close()

            _active_aircraft_hexes = current_hexes
            _active_aircraft_row_ids = {h: rid for h, rid in _active_aircraft_row_ids.items() if h in current_hexes}
        except FileNotFoundError:
            print(f"[WARN] {AIRCRAFT_JSON_PATH} not found -- readsb/tar1090 not running or not wired up yet")
        except Exception as e:
            print(f"[ERROR] Planes poll error: {e}")
        time.sleep(PLANES_POLL_INTERVAL_SECONDS)

threading.Thread(target=run_planes_poll_loop, daemon=True).start()

subprocess.run(['fuser', '-k', '5000/tcp'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1)

DB_FILE = "/home/david/BirdNET-Pi/scripts/birds.db"
IMAGE_PATH = "/home/david/birdnet/static/live.jpg"

os.makedirs(os.path.dirname(IMAGE_PATH), exist_ok=True)

def run_camera_loop():
    print("[CAM] Starting background snapshot loop...")
    views = [
        "",
        "",
        "--roi 0.115,0.115,0.769,0.769",
        "--roi 0.023,0.115,0.769,0.769",
        "--roi 0.213,0.115,0.769,0.769",
    ]
    i = 0
    CAM_LOG = "/home/david/birdnet/logs/camera.log"
    while True:
        roi = views[i % len(views)]
        tmp_path = IMAGE_PATH + ".tmp"
        cmd = f"rpicam-still -n --width 2304 --height 1296 -q 95 --autofocus-mode auto --autofocus-range full --autofocus-on-capture {roi} -o " + tmp_path
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=28)
            if result.returncode != 0:
                with open(CAM_LOG, "a") as f:
                    f.write(f"[{datetime.now()}] pos={i % len(views)} FAILED rc={result.returncode} stderr={result.stderr.strip()}\n")
            elif os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                os.replace(tmp_path, IMAGE_PATH)
        except subprocess.TimeoutExpired:
            with open(CAM_LOG, "a") as f:
                f.write(f"[{datetime.now()}] pos={i % len(views)} TIMED OUT\n")
        except Exception as e:
            print(f"[CAM ERROR] Snapshot failed: {e}")
        i += 1
        time.sleep(60)

threading.Thread(target=run_camera_loop, daemon=True).start()

app = Flask(__name__, template_folder="/home/david/birdnet/templates", static_folder="/home/david/birdnet/static")

def page_generated_now():
    return datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")

@app.route("/data/report/", methods=["POST"])
def wittboy_receive():
    try:
        d = request.form.to_dict()
        conn = sqlite3.connect(DB_FILE)
        conn.execute("""INSERT INTO wittboy (timestamp, tempf, humidity, baromrelin, winddir, windspeedmph, windgustmph, solarradiation, uv, rainrate, dailyrain)
                         VALUES (datetime('now','localtime'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (d.get('tempf'), d.get('humidity'), d.get('baromrelin'), d.get('winddir'),
                      d.get('windspeedmph'), d.get('windgustmph'), d.get('solarradiation'),
                      d.get('uv'), d.get('rrain_piezo'), d.get('drain_piezo')))
        conn.commit()
        conn.close()
        print(f"[{time.strftime('%X')}] WittBoy Sync -> Temp: {d.get('tempf')}F | Hum: {d.get('humidity')}%")
    except Exception as e:
        print(f"[ERROR] WittBoy receive error: {e}")
    return "OK", 200

@app.route("/")
def dashboard():
    conn = sqlite3.connect(DB_FILE)
    birds = conn.execute("""
        SELECT Com_Name, Sci_Name, MAX(Date || ' ' || Time) AS Last_Heard, COUNT(*) AS Total_Count, ROUND(AVG(Confidence) * 100, 0) AS Avg_Conf,
               (SELECT File_Name FROM detections d2 WHERE d2.Com_Name = detections.Com_Name ORDER BY d2.Date DESC, d2.Time DESC LIMIT 1) AS Latest_File,
               (SELECT Date FROM detections d3 WHERE d3.Com_Name = detections.Com_Name ORDER BY d3.Date DESC, d3.Time DESC LIMIT 1) AS Latest_Date
        FROM detections GROUP BY Com_Name ORDER BY Last_Heard DESC LIMIT 15
    """).fetchall()

    chart_raw = conn.execute("""
        SELECT Date || ' ' || substr(Time,1,2) || ':' || printf('%02d', (CAST(substr(Time,4,2) AS INTEGER)/15)*15) AS block,
               COUNT(DISTINCT Com_Name)
        FROM detections
        WHERE datetime(Date || ' ' || Time) >= datetime('now', 'localtime', '-24 hours')
        GROUP BY block
        ORDER BY block ASC;
    """).fetchall()
    _now_dt = datetime.now()
    _current_block = _now_dt.strftime("%Y-%m-%d %H:") + str((_now_dt.minute // 15) * 15).zfill(2)
    chart = [row for row in chart_raw if row[0] != _current_block]

    weather_data = conn.execute("""
        SELECT
            substr(timestamp,1,14) || printf('%02d', (CAST(substr(timestamp,15,2) AS INTEGER)/10)*10) || ':00' AS bucket,
            AVG(tempf), AVG(humidity), AVG(baromrelin), AVG(windspeedmph), AVG(windgustmph), AVG(dailyrain)
        FROM wittboy
        WHERE timestamp >= datetime('now', 'localtime', '-24 hours')
        GROUP BY bucket
        ORDER BY bucket ASC
    """).fetchall()
    conn.close()

    weather_labels = [row[0] for row in weather_data]
    temps = [round(row[1],1) if row[1] is not None else None for row in weather_data]
    humidities = [round(row[2],1) if row[2] is not None else None for row in weather_data]
    pressures = [round(row[3],2) if row[3] is not None else None for row in weather_data]
    windspeeds = [round(row[4],1) if row[4] is not None else None for row in weather_data]
    windgusts = [round(row[5],1) if row[5] is not None else None for row in weather_data]
    dailyrains = [round(row[6],2) if row[6] is not None else None for row in weather_data]

    image_version = int(os.path.getmtime(IMAGE_PATH)) if os.path.exists(IMAGE_PATH) else int(time.time())
    current_image = f"live.jpg?v={image_version}"
    page_generated = page_generated_now()

    conn2 = sqlite3.connect(DB_FILE)
    early_bird_row = conn2.execute("SELECT Com_Name, Sci_Name, substr(Time,1,5) FROM detections WHERE Date = date('now','localtime') ORDER BY Time ASC LIMIT 1").fetchone()
    conn2.close()
    early_bird = {"com_name": early_bird_row[0], "sci_name": early_bird_row[1], "time": early_bird_row[2]} if early_bird_row else None
    conn3 = sqlite3.connect(DB_FILE)
    night_owl_row = conn3.execute("SELECT Com_Name, Sci_Name, substr(Time,1,5) FROM detections WHERE Date = date('now','localtime','-1 day') AND Time <= '20:00:00' ORDER BY Time DESC LIMIT 1").fetchone()
    conn3.close()
    night_owl = {"com_name": night_owl_row[0], "sci_name": night_owl_row[1], "time": night_owl_row[2]} if night_owl_row else None
    sun_times = get_sun_times()
    moon_phase = get_moon_phase()

    resp = make_response(render_template(
        "dashboard.html", birds=birds, chart=chart, weather_labels=weather_labels,
        temps=temps, humidities=humidities, pressures=pressures,
        windspeeds=windspeeds, windgusts=windgusts,
        dailyrains=dailyrains,
        current_image=current_image, page_generated=page_generated,
        ebird_sightings=ebird_sightings, sun_times=sun_times, moon_phase=moon_phase,
        early_bird=early_bird, night_owl=night_owl
    ))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

@app.route("/alltime")
def alltime():
    conn = sqlite3.connect(DB_FILE)
    birds = conn.execute("""
        SELECT Com_Name, Sci_Name, MAX(Date || ' ' || Time) AS Last_Heard, COUNT(*) AS Total_Count
        FROM detections_alltime GROUP BY Com_Name ORDER BY Total_Count DESC LIMIT 100
    """).fetchall()
    conn.close()
    resp = make_response(render_template("alltime.html", birds=birds, page_generated=page_generated_now()))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

@app.route("/history")
def history():
    resp = make_response(render_template("history.html", page_generated=page_generated_now()))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

from flask import send_from_directory, abort
import re as _re

AUDIO_BASE = "/home/david/BirdSongs/Extracted/By_Date"

@app.route("/audio/<date>/<species>/<filename>")
def audio(date, species, filename):
    if not _re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", date):
        abort(400)
    if not _re.match(r"^[A-Za-z0-9_\-]+$", species):
        abort(400)
    if not _re.match(r"^[A-Za-z0-9_\-:. ]+\.mp3$", filename):
        abort(400)
    folder = f"{AUDIO_BASE}/{date}/{species}"
    return send_from_directory(folder, filename)


import json as _json
from pathlib import Path as _Path

MLOPS_MODELS_DIR = _Path("/home/david/birdnet/mlops/models")

def load_mlops_data():
    registry_path = MLOPS_MODELS_DIR / "registry.json"
    if not registry_path.exists():
        return None
    with open(registry_path) as f:
        registry = _json.load(f)

    current = registry.get("current_production")
    if current is None:
        return None

    version_dir = MLOPS_MODELS_DIR / current
    with open(version_dir / "metadata.json") as f:
        current_meta = _json.load(f)
    ff_path = version_dir / "fixed_effects.json"
    coef_path = ff_path if ff_path.exists() else version_dir / "coefficients.json"
    with open(coef_path) as f:
        coefficients = _json.load(f)

    import csv as _csv
    hourly = {}
    with open(version_dir / "predictions.csv") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            hb = row["hour_bin"]
            hourly.setdefault(hb, {"actual": 0.0, "predicted": 0.0})
            hourly[hb]["actual"] += float(row["count"])
            hourly[hb]["predicted"] += float(row["predicted"])

    from datetime import datetime as _dt2
    def _parse_hb(h):
        try:
            return _dt2.strptime(h, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return _dt2.strptime(h, "%Y-%m-%d %H:%M")
    hourly_parsed = {hb: (_parse_hb(hb), v) for hb, v in hourly.items()}

    # Hour-of-day view: collapse every date onto a single 5am-8pm axis, averaged
    # across all dates present in this version's predictions.csv.
    HOD_HOURS = list(range(5, 21))
    hod_acc = {h: {"actual": [], "predicted": []} for h in HOD_HOURS}
    for dt, v in hourly_parsed.values():
        if dt.hour in hod_acc:
            hod_acc[dt.hour]["actual"].append(v["actual"])
            hod_acc[dt.hour]["predicted"].append(v["predicted"])
    hod_labels = [f"{h:02d}:00" for h in HOD_HOURS]
    hod_actual = [round(sum(hod_acc[h]["actual"]) / len(hod_acc[h]["actual"]), 1) if hod_acc[h]["actual"] else 0 for h in HOD_HOURS]
    hod_predicted = [round(sum(hod_acc[h]["predicted"]) / len(hod_acc[h]["predicted"]), 1) if hod_acc[h]["predicted"] else 0 for h in HOD_HOURS]

    FRIENDLY_NAMES = {
        "tempf": "Temp",
        "humidity": "Humidity",
        "baromrelin": "Pressure",
        "dailyrain": "Rain",
        "time_of_day": "Time of Day",
        "time_of_day2": "Time of Day (curve)",
        "beta_tempf": "Temp",
        "beta_humidity": "Humidity",
        "beta_baromrelin": "Pressure",
        "beta_dailyrain": "Rain",
        "beta_time_of_day": "Time of Day",
        "beta_time_of_day2": "Time of Day (curve)",
    }
    coef_keys = [k for k in coefficients if k not in ("Intercept", "intercept")]
    coef_labels = [FRIENDLY_NAMES.get(k, k) for k in coef_keys]
    coef_means = [round(coefficients[k]["mean"], 3) for k in coef_keys]
    coef_sds = [round(coefficients[k]["sd"], 3) for k in coef_keys]

    sig_path = version_dir / "coef_significance.json"
    coef_probs = [None] * len(coef_keys)
    if sig_path.exists():
        with open(sig_path) as f:
            sig = _json.load(f)
        coef_probs = [round(sig.get(k.replace("beta_", ""), {}).get("prob_same_sign", 0), 3) for k in coef_keys]

    version_table = []
    for v in list(reversed(registry["versions"]))[:10]:
        with open(MLOPS_MODELS_DIR / v / "metadata.json") as f:
            m = _json.load(f)
        raw_metric = m.get("elpd_loo", m.get("log_likelihood"))
        fit_metric = (raw_metric / m["n_rows"]) if raw_metric is not None else None
        fit_metric_label = "Model Fit Score" if "elpd_loo" in m else "Log-likelihood/row"
        version_table.append({
            "version_id": m["version_id"],
            "fit_timestamp": m["fit_timestamp"],
            "n_rows": m["n_rows"],
            "fit_metric": round(fit_metric, 4) if fit_metric is not None else None,
            "fit_metric_label": fit_metric_label,
            "promoted": m["promoted"],
        })

    return {
        "hod_labels": hod_labels,
        "hod_actual": hod_actual,
        "hod_predicted": hod_predicted,
        "coef_labels": coef_labels,
        "coef_means": coef_means,
        "coef_sds": coef_sds,
        "coef_probs": coef_probs,
        "version_table": version_table,
        "current_version": current,
    }
import math as _math

def get_bird_clock_data():
    registry_path = MLOPS_MODELS_DIR / "registry.json"
    if not registry_path.exists():
        return None
    with open(registry_path) as f:
        registry = _json.load(f)
    current = registry.get("current_production")
    if current is None:
        return None
    version_dir = MLOPS_MODELS_DIR / current
    with open(version_dir / "fixed_effects.json") as f:
        fixed = _json.load(f)
    with open(version_dir / "random_effects.json") as f:
        random_fx = _json.load(f)
    with open(version_dir / "metadata.json") as f:
        meta = _json.load(f)
    time_scale = meta["scale_params"]["time_of_day"]
    beta_time = fixed["beta_time_of_day"]["mean"]
    beta_time2 = fixed["beta_time_of_day2"]["mean"]
    hours = list(range(5, 21))
    species_rows = []
    for name, fx in random_fx.items():
        curve = []
        for h in hours:
            t = (h - time_scale["mean"]) / time_scale["std"]
            slope = beta_time + fx["time_slope_mean"]
            slope2 = beta_time2 + fx["time2_slope_mean"]
            linpred = fx["offset_mean"] + slope * t + slope2 * (t ** 2)
            curve.append(_math.exp(linpred))
        total = sum(curve)
        peak_idx = curve.index(max(curve))
        window = curve[max(0, peak_idx - 1):peak_idx + 2]
        pct_in_peak = round(100 * sum(window) / total, 0) if total > 0 else 0
        peak_hour = hours[peak_idx]
        species_rows.append({
            "name": name,
            "curve": [round(v, 2) for v in curve],
            "peak_hour": peak_hour,
            "pct_in_peak": int(pct_in_peak),
        })
    conn = sqlite3.connect(DB_FILE)
    counts = dict(conn.execute("SELECT Com_Name, COUNT(*) FROM detections_alltime WHERE Com_Name IN (%s) GROUP BY Com_Name" % ",".join(["?"] * len(random_fx)), list(random_fx.keys())).fetchall())
    conn.close()
    for row in species_rows:
        row["n_detections"] = counts.get(row["name"], 0)
    species_rows.sort(key=lambda r: r["peak_hour"])
    return {"hours": hours, "species": species_rows, "current_version": current}


def get_seasonal_trend_data():
    # Rolling window of the most recent 26 fully-CLOSED Monday-start weeks (never
    # includes the current in-progress week -- this page only updates once a week
    # ends). Each week's value is that species' share of the WEEK'S total detections
    # across all species, not raw counts and not a share of the species' own
    # all-time total -- dividing by the week's total cancels out anything that
    # shifts detection volume for every species equally that week (a mic change,
    # a stretch of rainy/windy days), so it doesn't masquerade as real migration.
    registry_path = MLOPS_MODELS_DIR / "registry.json"
    if not registry_path.exists():
        return None
    with open(registry_path) as f:
        registry = _json.load(f)
    current = registry.get("current_production")
    if current is None:
        return None
    with open(MLOPS_MODELS_DIR / current / "random_effects.json") as f:
        random_fx = _json.load(f)
    species_names = list(random_fx.keys())

    today = datetime.now().date()
    this_monday = today - timedelta(days=today.weekday())
    last_closed_monday = this_monday - timedelta(days=7)
    candidate_starts = [last_closed_monday - timedelta(weeks=i) for i in range(25, -1, -1)]

    conn = sqlite3.connect(DB_FILE)
    weeks = []
    weekly_pct = {name: [] for name in species_names}
    window_totals = {name: 0 for name in species_names}
    for ws in candidate_starts:
        we = ws + timedelta(days=6)
        rows = conn.execute(
            "SELECT Com_Name, COUNT(*) FROM detections_alltime WHERE Date >= ? AND Date <= ? GROUP BY Com_Name",
            (ws.isoformat(), we.isoformat())
        ).fetchall()
        total = sum(c for _, c in rows)
        if total == 0:
            continue  # no data yet this far back -- window grows toward 26 over time
        counts = dict(rows)
        weeks.append(ws)
        for name in species_names:
            n = counts.get(name, 0)
            weekly_pct[name].append(round(100 * n / total, 2))
            window_totals[name] += n
    conn.close()

    if not weeks:
        return None

    species_rows = []
    for name in species_names:
        curve = weekly_pct[name]
        peak_idx = curve.index(max(curve)) if curve else 0
        species_rows.append({
            "name": name,
            "curve": curve,
            "peak_week_label": weeks[peak_idx].strftime("%-m/%-d"),
            "pct_in_peak": round(curve[peak_idx]) if curve else 0,
            "n_detections": window_totals[name],
            "_peak_idx": peak_idx,
        })
    species_rows.sort(key=lambda r: r["_peak_idx"])
    for row in species_rows:
        del row["_peak_idx"]

    # Bars within a group are scaled against ONE shared linear max, not each
    # species' own max -- these percentages are directly comparable across
    # species (unlike /bird-models' curves), so a common bird's dominant week
    # should visibly dwarf a rare bird's, not get stretched to look the same.
    global_max = max((max(c) for c in weekly_pct.values() if c), default=0)

    # If one species' peak towers over everyone else's (>=2x the next-highest
    # peak in the window), sharing one linear scale crushes every other
    # species into a hairline. Pull that species into its own row, scaled
    # against its own max -- which is also the true global max, so nothing
    # about ITS bars is distorted -- and give the remaining species their own
    # shared linear max, so they stay honestly comparable to EACH OTHER
    # without being squashed by the outlier. If nothing clears that 2x bar,
    # everyone stays together on one shared scale, same as before.
    outlier = None
    rest_rows = species_rows
    if species_rows and global_max > 0:
        top_row = max(species_rows, key=lambda r: max(r["curve"]) if r["curve"] else 0)
        others_max = max(
            (max(r["curve"]) for r in species_rows if r is not top_row and r["curve"]),
            default=0,
        )
        if others_max == 0 or global_max >= 2 * others_max:
            outlier = top_row
            rest_rows = [r for r in species_rows if r is not top_row]

    rest_max = max((max(r["curve"]) for r in rest_rows if r["curve"]), default=0)

    return {
        "week_labels": [w.strftime("%-m/%-d") for w in weeks],
        "species": rest_rows,
        "outlier": outlier,
        "global_max": global_max,
        "rest_max": rest_max,
    }


def _live_aircraft_entry(plane):
    """Minimal per-aircraft radar entry: hex (trail key), callsign (blip
    label), and position (ground_distance_mi/r_dir_deg) for plotting -- all
    the radar actually plots. Resolved type/registration/airline info used
    to be computed here too, but that only ever fed the "Last Plane
    Detected" spotlight card (removed: with multiple simultaneous aircraft,
    re-picking whichever one's packet arrived most recently every 5s poll
    just looked like flicker, and it was redundant with the radar/table
    anyway). Re-add via _aircraft_types/_classify_flight if a future radar
    feature (e.g. a blip tooltip) needs it.

    Two distances are returned and they are NOT interchangeable:
    - distance_mi is the true 3D slant range (ground + altitude combined,
      via _slant_range_mi) -- this is what _within_display_range() filters
      on, and it's the figure the Recent Detections table shows.
    - ground_distance_mi is the 2D ground-track distance only (r_dst
      converted to statute miles, no altitude component) -- this is what
      the radar uses to place the blip. A standard radar plots ground
      position, not slant range: a plane directly overhead at cruise
      altitude has ~0 ground distance and belongs near the center of the
      scope, even though its slant range (mostly altitude) can be several
      miles. Plotting by slant range would push that overhead plane out
      near the edge, which reads as "far away" when it's actually right
      above the station.

    callsign falls back to NO_CALLSIGN_LABEL rather than the raw hex when
    flight is blank -- a bare hex like "A3EDAA" is shaped enough like a
    real callsign to mislead."""
    hex_code = plane.get("hex")
    flight = (plane.get("flight") or "").strip() or None
    r_dst = plane.get("r_dst")
    distance_mi = _slant_range_mi(r_dst, plane.get("alt_baro"))
    ground_distance_mi = r_dst * NM_TO_MI if r_dst is not None else None
    r_dir = plane.get("r_dir")
    return {
        "hex": hex_code,
        "callsign": flight.upper() if flight else NO_CALLSIGN_LABEL,
        "distance_mi": round(distance_mi, 1) if distance_mi is not None else None,
        "ground_distance_mi": round(ground_distance_mi, 1) if ground_distance_mi is not None else None,
        "r_dir_deg": round(r_dir) if r_dir is not None else None,
    }

def get_live_aircraft_snapshot():
    """Every aircraft currently in the live readsb snapshot that's within
    DISPLAY_RANGE_MI (true 3D distance), resolved and sorted nearest-first.
    This is a display-only filter for the radar -- the background poll loop
    (run_planes_poll_loop) logs every aircraft it sees regardless of
    distance, unaffected by this. Empty list (not an error) if aircraft.json
    is missing/unreadable, lists nothing right now, or nothing currently
    visible happens to be within range."""
    try:
        with open(AIRCRAFT_JSON_PATH) as f:
            snapshot = _json.load(f)
        aircraft = snapshot.get("aircraft", [])
    except (FileNotFoundError, _json.JSONDecodeError, OSError):
        aircraft = []

    entries = [_live_aircraft_entry(a) for a in aircraft if a.get("hex")]
    entries = [e for e in entries if _within_display_range(e["distance_mi"])]
    entries.sort(key=lambda e: e["distance_mi"])
    return entries

def get_hourly_plane_counts():
    conn = sqlite3.connect(PLANES_DB_FILE)
    chart = conn.execute("""
        SELECT strftime('%Y-%m-%d %H:00', detected_at) AS bucket, COUNT(*)
        FROM plane_detections
        WHERE detected_at >= datetime('now', 'localtime', '-24 hours')
        GROUP BY bucket
        ORDER BY bucket ASC
    """).fetchall()
    conn.close()
    return {
        "hour_labels": [row[0][-5:] for row in chart],
        "hourly_counts": [row[1] for row in chart],
    }

def get_planes_live_payload():
    """Single-response payload for the /api/planes-live poll: every aircraft
    currently visible within DISPLAY_RANGE_MI (for the radar), the last
    RECENT_DETECTIONS_WINDOW_MINUTES of logged rows that ever came within
    DISPLAY_RANGE_MI at some point during tracking (for the recent-detections
    table), and hourly counts (for the trend chart, NOT range-filtered --
    it's a long-run activity trend, not a "what's nearby" view) --
    everything the frontend's 5-second refresh needs to update the whole
    page in one round trip. No day-total figure here -- the "Planes Logged
    Today" stat card that used it was removed.

    Note this function reads only from plane_detections (the logged
    history) plus the live aircraft.json snapshot via
    get_live_aircraft_snapshot() -- it has no other state, so the radar
    (aircraft) is unaffected by anything below.

    The radar (aircraft) filters live positions by _within_display_range()
    on each aircraft's current distance. The table (recent) filters by
    min_distance_mi -- the closest approach ever recorded for that row,
    maintained by run_planes_poll_loop -- rather than the row's current/
    latest distance. This is deliberate: with run_planes_poll_loop
    refreshing r_dst/alt_baro on every poll, filtering the table on the
    LATEST distance would make a row flicker out of the table the moment a
    plane that had flown within range drifts back out past
    DISPLAY_RANGE_MI (or flicker in/out near the boundary from ordinary
    position noise), even though it was legitimately nearby moments
    earlier. Filtering on min_distance_mi instead means a row stays
    eligible for the rest of its tracking session once it's ever come
    within range, while the columns it displays (distance/altitude/speed)
    still reflect the LATEST reading -- inclusion and display are
    deliberately decoupled here."""
    aircraft = get_live_aircraft_snapshot()
    hourly = get_hourly_plane_counts()

    conn = sqlite3.connect(PLANES_DB_FILE)
    recent_rows = conn.execute("""
        SELECT detected_at, hex, flight, registration, type_designator, description,
               is_commercial, airline_name, r_dst, gs, alt_baro, min_distance_mi
        FROM plane_detections
        WHERE detected_at >= datetime('now', 'localtime', ?)
          AND min_distance_mi <= ?
        ORDER BY id DESC
    """, (f"-{RECENT_DETECTIONS_WINDOW_MINUTES} minutes", DISPLAY_RANGE_MI)).fetchall()
    conn.close()

    recent = []
    for (detected_at, hex_code, flight, registration, type_designator, description,
         is_commercial, airline_name, r_dst, gs, alt_baro, min_distance_mi) in recent_rows:
        # Latest distance, for display only -- inclusion was already
        # decided by the SQL filter above on min_distance_mi. This can be
        # None if the aircraft's most recent poll happened to be missing
        # r_dst/alt_baro; the frontend already renders that as "--".
        distance_mi = _slant_range_mi(r_dst, alt_baro)
        try:
            detected_epoch = int(datetime.strptime(detected_at, "%Y-%m-%d %H:%M:%S").timestamp())
        except ValueError:
            detected_epoch = None
        flight = (flight or "").strip() or None
        callsign = flight.upper() if flight else (registration or NO_CALLSIGN_LABEL)
        recent.append({
            "detected_at_epoch": detected_epoch,
            "callsign": callsign,
            "is_commercial": bool(is_commercial),
            "airline_name": airline_name,
            "type_name": description or type_designator or "Unknown aircraft",
            "altitude_ft": alt_baro,
            "distance_mi": round(distance_mi, 1) if distance_mi is not None else None,
            # Closest approach ever recorded for this row (true 3D slant range),
            # ratcheted down by run_planes_poll_loop -- always <= the current
            # distance_mi above. Same filter the SQL uses to decide inclusion.
            "closest_distance_mi": round(min_distance_mi, 1) if min_distance_mi is not None else None,
            "ground_speed_kt": round(gs) if gs is not None else None,
        })

    return {
        "aircraft": aircraft,
        "recent": recent,
        "hour_labels": hourly["hour_labels"],
        "hourly_counts": hourly["hourly_counts"],
    }


WITTBOY_VARS = ["tempf", "humidity", "baromrelin", "winddir", "windspeedmph", "windgustmph", "solarradiation", "uv", "rainrate", "dailyrain"]

def get_wittboy_correlation():
    import pandas as pd
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query(f"SELECT {', '.join(WITTBOY_VARS)} FROM wittboy", conn)
    conn.close()
    corr = df.corr().round(2)
    return {"labels": WITTBOY_VARS, "matrix": corr.values.tolist()}


def get_dueling_data():
    bayes_dir = MLOPS_MODELS_DIR
    cat_dir = MLOPS_MODELS_DIR.parent / "experiments" / "categorical_hour"
    bayes_reg_path = bayes_dir / "registry.json"
    cat_reg_path = cat_dir / "registry.json"
    bayes_by_date = {}
    if bayes_reg_path.exists():
        with open(bayes_reg_path) as f:
            bayes_reg = _json.load(f)
        for v in bayes_reg["versions"]:
            fd_path = bayes_dir / v / "fit_diagnostics.json"
            meta_path = bayes_dir / v / "metadata.json"
            if not fd_path.exists() or not meta_path.exists():
                continue
            with open(fd_path) as f:
                fd = _json.load(f)
            with open(meta_path) as f:
                meta = _json.load(f)
            date_key = meta["fit_timestamp"][:8]
            bayes_by_date[date_key] = {
                "version": v, "timestamp": meta["fit_timestamp"],
                "elpd_per_row": fd["elpd_loo"] / fd["n_rows"],
                "se_per_row": fd["elpd_loo_se"] / fd["n_rows"],
                "n_rows": fd["n_rows"], "p_loo": fd.get("p_loo"),
            }
    cat_by_date = {}
    if cat_reg_path.exists():
        with open(cat_reg_path) as f:
            cat_reg = _json.load(f)
        for v in cat_reg["versions"]:
            fd_path = cat_dir / v / "fit_diagnostics.json"
            if not fd_path.exists():
                continue
            with open(fd_path) as f:
                fd = _json.load(f)
            date_key = v.split("_")[1] if "_" in v else ""
            cat_by_date[date_key] = {
                "version": v, "timestamp": date_key,
                "elpd_per_row": fd["elpd_loo"] / fd["n_rows"],
                "se_per_row": fd["elpd_loo_se"] / fd["n_rows"],
                "n_rows": fd["n_rows"], "p_loo": fd.get("p_loo"),
            }
    bayes_points = []
    cat_points = []
    for date_key in sorted(set(bayes_by_date) & set(cat_by_date)):
        b = bayes_by_date[date_key]
        c = cat_by_date[date_key]
        if b["n_rows"] != c["n_rows"]:
            continue
        bayes_points.append(b)
        cat_points.append(c)
    verdict = None
    if bayes_points and cat_points:
        b = bayes_points[-1]
        c = cat_points[-1]
        diff = c["elpd_per_row"] - b["elpd_per_row"]
        combined_se = ((b["se_per_row"] ** 2) + (c["se_per_row"] ** 2)) ** 0.5
        n_se = abs(diff) / combined_se if combined_se > 0 else 0
        if n_se < 1.0:
            verdict = {"label": "Too close to call", "detail": "The two models' fit quality is within statistical noise of each other tonight, on the exact same data.", "leader": None}
        else:
            leader = "Categorical" if diff > 0 else "Bayesian (quadratic)"
            verdict = {"label": f"{leader} fits better tonight", "detail": f"Difference is about {round(n_se,1)} standard errors on identical data, a real (if modest) edge.", "leader": leader}
    return {"bayes": bayes_points, "categorical": cat_points, "verdict": verdict}
@app.route("/analytics")
def analytics():
    mlops_data = load_mlops_data()
    corr_data = get_wittboy_correlation()

    resp = make_response(render_template("analytics.html", mlops=mlops_data, corr=corr_data, page_generated=page_generated_now()))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.route("/bird-models")
def bird_models():
    clock_data = get_bird_clock_data()
    return render_template("bird_models.html", clock_data=clock_data, page_generated=page_generated_now())
@app.route("/dueling-models")
def dueling_models():
    duel_data = get_dueling_data()
    return render_template("dueling_models.html", duel_data=duel_data, page_generated=page_generated_now())

@app.route("/seasonal-trends")
def seasonal_trends():
    trend_data = get_seasonal_trend_data()
    return render_template("seasonal_trends.html", trend_data=trend_data, page_generated=page_generated_now())

@app.route("/planes-detected")
def planes_detected():
    live = get_planes_live_payload()
    return render_template("planes_detected.html", live=live, page_generated=page_generated_now())

@app.route("/api/planes-live")
def api_planes_live():
    resp = make_response(jsonify(get_planes_live_payload()))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

@app.route("/sitemap.xml")
def sitemap():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://winstonsalembirds.com/</loc></url>
  <url><loc>https://winstonsalembirds.com/alltime</loc></url>
  <url><loc>https://winstonsalembirds.com/history</loc></url>
  <url><loc>https://winstonsalembirds.com/analytics</loc></url>
</urlset>"""
    return Response(xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    txt = """User-agent: *
Allow: /
Sitemap: https://winstonsalembirds.com/sitemap.xml
"""
    return Response(txt, mimetype="text/plain")


@app.route("/subscribe", methods=["POST"])
def subscribe():
    email = request.form.get("email", "").strip().lower()
    if not email or "@" not in email:
        return "invalid", 400
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT OR IGNORE INTO subscribers (email) VALUES (?)", (email,))
        conn.commit()
        conn.close()
        return "OK", 200
    except Exception as e:
        print(f"[ERROR] Subscribe error: {e}")
        return "error", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
