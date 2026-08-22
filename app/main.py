import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
import os
import time
from datetime import datetime, timedelta, timezone
import sqlite3
import subprocess
import threading
import requests
from flask import Flask, render_template, make_response, request, Response

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
