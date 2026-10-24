"""
/species-models — "reverse-engineered" per-species models.

The production GLMM (train_and_register.py, nb_glmm_hourcat_seasonal_v3) is one
integrated model: shared fixed effects (intercept, weather, season) plus
per-species random effects (offset, hourly deviations, season slopes). This
module collapses those pieces back into one standalone equation per species:

  log E[calls/hr] = (intercept + offset_s)
                  + hour_curve_s[h]                      # shared hour effect + species deviation
                  + (b_day + b_day_s) * d + (b_day2 + b_day2_s) * d^2
                  + sum(beta_w * z_w)                     # weather, shared by all species

where d = (days since 2026-01-01 - mean) / std (metadata scale_params). Weather
is held at its training average (z = 0) for the curves, so they show the
species' own hour/season pattern.

Self-contained Blueprint so main.py only needs two lines to register it.
"""
import json
import math
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Blueprint, render_template

bp = Blueprint("species_models", __name__)

DB_FILE = "/home/david/BirdNET-Pi/scripts/birds.db"
MLOPS_MODELS_DIR = Path("/home/david/birdnet/mlops/models")
DATA_START = date(2026, 7, 21)  # extract.py lower bound
SEASON_ORIGIN = date(2026, 1, 1)  # train_and_register.add_season origin
TOP_N = 20

# Readable weather nudges: (feature, step in raw units, label)
WEATHER_STEPS = [
    ("tempf", 10.0, "10°F warmer"),
    ("humidity", 10.0, "10 points more humid"),
    ("baromrelin", 0.1, "0.1 inHg higher pressure"),
    ("dailyrain", 0.5, "0.5 in more rain so far today"),
]


def _load(version_dir, name):
    p = version_dir / name
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def _season_z(day, scale):
    return ((day - SEASON_ORIGIN).days - scale["mean"]) / scale["std"]


def get_species_model_data():
    registry = _load(MLOPS_MODELS_DIR, "registry.json")
    if not registry or not registry.get("current_production"):
        return None
    version = registry["current_production"]
    vdir = MLOPS_MODELS_DIR / version
    fixed = _load(vdir, "fixed_effects.json")
    random_fx = _load(vdir, "random_effects.json")
    meta = _load(vdir, "metadata.json")
    diag = _load(vdir, "fit_diagnostics.json") or {}
    if not fixed or not random_fx or not meta:
        return None
    if "season_day" not in meta.get("scale_params", {}):
        return None  # older (pre-season) model format
    season_scale = meta["scale_params"]["season_day"]

    intercept = fixed["intercept"]["mean"]
    b_day = fixed["beta_season_day"]["mean"]
    b_day2 = fixed["beta_season_day2"]["mean"]

    # ---- actual detections (top N among modeled species) ----
    names = list(random_fx.keys())
    conn = sqlite3.connect(DB_FILE)
    ph = ",".join(["?"] * len(names))
    totals = dict(conn.execute(
        f"SELECT Com_Name, COUNT(*) FROM detections_alltime "
        f"WHERE Com_Name IN ({ph}) AND Date >= ? GROUP BY Com_Name",
        names + [DATA_START.isoformat()],
    ).fetchall())
    top = sorted(names, key=lambda n: totals.get(n, 0), reverse=True)[:TOP_N]
    ph = ",".join(["?"] * len(top))
    daily_rows = conn.execute(
        f"SELECT Com_Name, Date, COUNT(*) FROM detections_alltime "
        f"WHERE Com_Name IN ({ph}) AND Date >= ? "
        f"AND CAST(substr(Time,1,2) AS INTEGER) BETWEEN 5 AND 20 "
        f"GROUP BY Com_Name, Date",
        top + [DATA_START.isoformat()],
    ).fetchall()
    conn.close()
    daily_actual = {}
    for n, d, c in daily_rows:
        daily_actual.setdefault(n, {})[d] = c

    today = date.today()
    days = [DATA_START + timedelta(days=i) for i in range((today - DATA_START).days + 1)]
    day_labels = [d.isoformat() for d in days]
    z_today = _season_z(today, season_scale)

    species = []
    for rank, name in enumerate(top, 1):
        fx = random_fx[name]
        hours = sorted(int(h) for h in fx["hour_curve"])
        hc = {int(h): v for h, v in fx["hour_curve"].items()}
        base = intercept + fx["offset_mean"]
        s1 = b_day + fx["season_day_slope_mean"]
        s2 = b_day2 + fx["season_day2_slope_mean"]

        def season_term(z):
            return s1 * z + s2 * z * z

        # Hourly profile today, average weather
        hourly_today = [math.exp(base + hc[h] + season_term(z_today)) for h in hours]
        peak_i = max(range(len(hours)), key=lambda i: hourly_today[i])

        # Expected daily (5am-8pm) total across the season
        hour_sum = sum(math.exp(hc[h]) for h in hours)
        daily_pred = [math.exp(base + season_term(_season_z(d, season_scale))) * hour_sum for d in days]
        actual = [daily_actual.get(name, {}).get(lbl, 0) for lbl in day_labels]

        # Season status: slope of the log-curve today, per week
        dz_per_day = 1.0 / season_scale["std"]
        weekly_change = math.exp((s1 + 2 * s2 * z_today) * dz_per_day * 7) - 1
        peak_day_i = max(range(len(days)), key=lambda i: daily_pred[i])
        if peak_day_i == len(days) - 1 and weekly_change > 0.02:
            status = "Rising"
        elif peak_day_i == 0 and weekly_change < -0.02:
            status = "Fading"
        elif abs(weekly_change) <= 0.02:
            status = "Steady"
        elif weekly_change > 0:
            status = "Rising"
        else:
            status = "Fading"
        shape = "valley" if s2 > 0 else "hump"

        species.append({
            "rank": rank,
            "name": name,
            "n_detections": totals.get(name, 0),
            "hours": hours,
            "hourly_today": [round(v, 3) for v in hourly_today],
            "peak_hour": hours[peak_i],
            "daily_pred": [round(v, 2) for v in daily_pred],
            "daily_actual": actual,
            "pred_today": round(daily_pred[-1], 1),
            "status": status,
            "weekly_change_pct": round(100 * weekly_change),
            "season_peak": days[peak_day_i].strftime("%b %-d"),
            "season_shape": shape,
            "eq": {
                "base": round(base, 2),
                "base_sd": round(fx["offset_sd"], 2),
                "s1": round(s1, 2),
                "s2": round(s2, 2),
                "s1_sd": round(fx["season_day_slope_sd"], 2),
                "s2_sd": round(fx["season_day2_slope_sd"], 2),
            },
        })

    weather = []
    for feat, step, label in WEATHER_STEPS:
        key = f"beta_{feat}"
        if key not in fixed or feat not in meta["scale_params"]:
            continue
        sd = meta["scale_params"][feat]["std"]
        beta = fixed[key]["mean"]
        beta_sd = fixed[key]["sd"]
        eff = math.exp(beta * step / sd) - 1
        weather.append({
            "label": label,
            "pct": round(100 * eff),
            "beta": round(beta, 3),
            "is_clear": abs(beta) > 2 * beta_sd,
        })

    return {
        "version": version,
        "fit_time": datetime.strptime(meta["fit_timestamp"], "%Y%m%d_%H%M%S").strftime("%b %-d, %Y %-I:%M %p"),
        "n_species_model": len(random_fx),
        "n_rows": meta.get("n_rows"),
        "rhat_max": round(diag.get("rhat_max", float("nan")), 3) if diag else None,
        "intercept": round(intercept, 2),
        "b_day": round(b_day, 2),
        "b_day2": round(b_day2, 2),
        "day_labels": day_labels,
        "today": today.strftime("%b %-d"),
        "weather": weather,
        "species": species,
    }


@bp.route("/species-models")
def species_models():
    # Same format as main.page_generated_now (not imported: main.py runs as __main__)
    page_generated = datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")
    data = get_species_model_data()
    return render_template("species_models.html", data=data, page_generated=page_generated)
