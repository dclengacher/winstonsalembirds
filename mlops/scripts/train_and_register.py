"""
Fit hierarchical negative-binomial model in PyMC, save all fit outputs
(fixed effects, random effects, fit diagnostics) immediately after sampling.

Hour of day: categorical (hour_effect, zero-sum) + per-species hourly
deviations (zero-sum within species), replacing the quadratic curve. Won the
Dueling Models comparison every night Aug 14 - Oct 10; supports dawn peaks,
midday lulls and second peaks a quadratic can't.
Seasonality: season_day (centered/scaled day index) and its square. Fixed
effects give the average seasonal curve; per-species random slopes on both
let each species rise, peak and fade on its own schedule (partially pooled).
Species set: every species with >= MIN_SPECIES_DETECTIONS in the training
data (capped at MAX_SPECIES), not a frozen all-time top 20.
Promotion: newest converged fit wins. ELPD isn't comparable across fits on
different data, so it's recorded but no longer used as the gate.
"""
import json
import numpy as np
import pandas as pd
import pymc as pm
import arviz as az
import sqlite3
from pathlib import Path
from datetime import datetime

DATA_PATH = "/home/david/birdnet/mlops/scripts/extracted_data.csv"
DB_PATH = "/home/david/BirdNET-Pi/scripts/birds.db"
MODELS_DIR = Path("/home/david/birdnet/mlops/models")
REGISTRY_PATH = MODELS_DIR / "registry.json"

FIXED_FEATURES = ["tempf", "humidity", "baromrelin", "dailyrain"]
SEASON_FEATURES = ["season_day", "season_day2"]
MIN_SPECIES_DETECTIONS = 100
MAX_SPECIES = 30


def add_season(df, scale_params):
    # Under a year of data, sin/cos(day_of_year) are nearly collinear and NUTS
    # crawls. Use a centered/scaled day index + its square instead (x and x^2
    # are ~uncorrelated once centered). Switch back to sin/cos after a full year.
    day = (pd.to_datetime(df["hour_bin"]) - pd.Timestamp("2026-01-01")).dt.days.astype(float)
    m, sd = day.mean(), day.std()
    scale_params["season_day"] = {"mean": float(m), "std": float(sd)}
    df["season_day"] = (day - m) / sd
    df["season_day2"] = df["season_day"] ** 2
    return df


def load_registry():
    if REGISTRY_PATH.exists():
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    return {"versions": [], "current_production": None}


def save_registry(registry):
    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2)


def fit_model(df):
    scale_params = {}
    df = df.copy()
    for col in FIXED_FEATURES:
        mean, std = df[col].mean(), df[col].std()
        scale_params[col] = {"mean": mean, "std": std}
        df[col] = (df[col] - mean) / std
    df = add_season(df, scale_params)

    hours_sorted = sorted(int(h) for h in df["time_of_day"].unique())
    hour_idx_map = {h: i for i, h in enumerate(hours_sorted)}
    hour_idx = df["time_of_day"].astype(int).map(hour_idx_map).values
    n_hours = len(hours_sorted)

    species_names = sorted(df["Com_Name"].unique())
    species_idx_map = {name: i for i, name in enumerate(species_names)}
    species_idx = df["Com_Name"].map(species_idx_map).values
    n_species = len(species_names)

    with pm.Model() as model:
        intercept = pm.Normal("intercept", mu=0, sigma=2)
        betas = {
            feat: pm.Normal(f"beta_{feat}", mu=0, sigma=1) for feat in FIXED_FEATURES
        }
        hour_effect = pm.ZeroSumNormal("hour_effect", sigma=1, shape=n_hours)

        mu_species = pm.Normal("mu_species", mu=0, sigma=1)
        sigma_species = pm.HalfNormal("sigma_species", sigma=1)
        species_offset = pm.Normal(
            "species_offset", mu=mu_species, sigma=sigma_species, shape=n_species
        )

        sigma_hour_species = pm.HalfNormal("sigma_hour_species", sigma=0.5)
        hour_species = pm.ZeroSumNormal(
            "hour_species", sigma=sigma_hour_species, shape=(n_species, n_hours), n_zerosum_axes=1
        )

        beta_day = pm.Normal("beta_season_day", mu=0, sigma=1)
        beta_day2 = pm.Normal("beta_season_day2", mu=0, sigma=1)
        sigma_day_species = pm.HalfNormal("sigma_day_species", sigma=0.5)
        b_day_species = pm.Normal("b_day_species", mu=0, sigma=sigma_day_species, shape=n_species)
        sigma_day2_species = pm.HalfNormal("sigma_day2_species", sigma=0.5)
        b_day2_species = pm.Normal("b_day2_species", mu=0, sigma=sigma_day2_species, shape=n_species)

        linpred = intercept + species_offset[species_idx]
        for feat in FIXED_FEATURES:
            linpred = linpred + betas[feat] * df[feat].values
        linpred = linpred + hour_effect[hour_idx] + hour_species[species_idx, hour_idx]
        linpred = linpred + (beta_day + b_day_species[species_idx]) * df["season_day"].values
        linpred = linpred + (beta_day2 + b_day2_species[species_idx]) * df["season_day2"].values

        mu = pm.math.exp(linpred)
        alpha = pm.Exponential("alpha", 1.0)

        y_obs = pm.NegativeBinomial(
            "y_obs", mu=mu, alpha=alpha, observed=df["count"].values
        )

        trace = pm.sample(1000, tune=1000, chains=4, progressbar=False, random_seed=42)
        pm.compute_log_likelihood(trace)

    return trace, model, scale_params, df, species_names, species_idx, hours_sorted, hour_idx


def extract_fixed_effects(trace):
    var_names = ["intercept"] + [f"beta_{f}" for f in FIXED_FEATURES] + ["beta_season_day", "beta_season_day2"]
    summary = az.summary(trace, var_names=var_names)
    return {
        idx: {"mean": float(row["mean"]), "sd": float(row["sd"])}
        for idx, row in summary.iterrows()
    }


def extract_random_effects(trace, species_names, hours_sorted):
    offset_summary = az.summary(trace, var_names=["species_offset"])
    day_summary = az.summary(trace, var_names=["b_day_species"])
    day2_summary = az.summary(trace, var_names=["b_day2_species"])
    post = trace.posterior
    hour_fx = post["hour_effect"].mean(dim=["chain", "draw"]).values
    hour_sp = post["hour_species"].mean(dim=["chain", "draw"]).values
    result = {}
    for i, name in enumerate(species_names):
        result[name] = {
            "offset_mean": float(offset_summary.iloc[i]["mean"]),
            "offset_sd": float(offset_summary.iloc[i]["sd"]),
            "season_day_slope_mean": float(day_summary.iloc[i]["mean"]),
            "season_day_slope_sd": float(day_summary.iloc[i]["sd"]),
            "season_day2_slope_mean": float(day2_summary.iloc[i]["mean"]),
            "season_day2_slope_sd": float(day2_summary.iloc[i]["sd"]),
            # log-scale hourly shape (shared hour effect + this species' deviation)
            "hour_curve": {str(h): round(float(hour_fx[j] + hour_sp[i, j]), 4) for j, h in enumerate(hours_sorted)},
        }
    return result


def extract_hour_effect(trace, hours_sorted):
    summary = az.summary(trace, var_names=["hour_effect"])
    return {
        str(hours_sorted[i]): {"mean": float(row["mean"]), "sd": float(row["sd"])}
        for i, (_, row) in enumerate(summary.iterrows())
    }


def extract_fit_diagnostics(trace, df):
    loo = az.loo(trace)
    rhat_summary = az.rhat(trace)
    rhat_max = float(max(float(rhat_summary[var].max()) for var in rhat_summary.data_vars))
    return {
        "elpd_loo": float(loo["elpd"]),
        "elpd_loo_se": float(loo["se"]),
        "p_loo": float(loo["p"]),
        "n_rows": len(df),
        "n_species": int(df["Com_Name"].nunique()),
        "n_zero_rows": int((df["count"] == 0).sum()),
        "rhat_max": rhat_max,
        "converged": rhat_max < 1.05,
        "method": "pymc_nuts",
    }


def predict(trace, df, species_idx, hour_idx):
    post = trace.posterior
    m = lambda v: post[v].mean(dim=["chain", "draw"]).values
    linpred = m("intercept") + m("species_offset")[species_idx]
    for feat in FIXED_FEATURES:
        linpred = linpred + m(f"beta_{feat}") * df[feat].values
    linpred = linpred + m("hour_effect")[hour_idx] + m("hour_species")[species_idx, hour_idx]
    linpred = linpred + (m("beta_season_day") + m("b_day_species")[species_idx]) * df["season_day"].values
    linpred = linpred + (m("beta_season_day2") + m("b_day2_species")[species_idx]) * df["season_day2"].values
    return np.exp(linpred)


def main():
    df_raw = pd.read_csv(DATA_PATH)
    totals = df_raw.groupby("Com_Name")["count"].sum().sort_values(ascending=False)
    keep = totals[totals >= MIN_SPECIES_DETECTIONS].head(MAX_SPECIES).index
    df_raw = df_raw[df_raw["Com_Name"].isin(keep)].reset_index(drop=True)
    print(f"Training on {len(keep)} species, {len(df_raw)} rows")
    trace, model, scale_params, df_scaled, species_names, species_idx, hours_sorted, hour_idx = fit_model(df_raw)

    fixed_effects = extract_fixed_effects(trace)
    random_effects = extract_random_effects(trace, species_names, hours_sorted)
    hour_effect = extract_hour_effect(trace, hours_sorted)
    fit_diagnostics = extract_fit_diagnostics(trace, df_scaled)
    mu_pred = predict(trace, df_scaled, species_idx, hour_idx)

    registry = load_registry()
    version_num = len(registry["versions"]) + 1
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    version_id = f"v{version_num}_{timestamp}"
    version_dir = MODELS_DIR / version_id
    version_dir.mkdir(parents=True, exist_ok=True)

    predictions_df = df_scaled[["hour_bin", "Com_Name", "count"]].copy()
    predictions_df["predicted"] = mu_pred
    predictions_df.to_csv(version_dir / "predictions.csv", index=False)

    with open(version_dir / "fixed_effects.json", "w") as f:
        json.dump(fixed_effects, f, indent=2)

    with open(version_dir / "random_effects.json", "w") as f:
        json.dump(random_effects, f, indent=2)

    with open(version_dir / "hour_effect.json", "w") as f:
        json.dump(hour_effect, f, indent=2)

    with open(version_dir / "fit_diagnostics.json", "w") as f:
        json.dump(fit_diagnostics, f, indent=2)

    coef_significance = {}
    sig_vars = FIXED_FEATURES + SEASON_FEATURES
    for feat in sig_vars:
        samples = trace.posterior[f"beta_{feat}"].values.flatten()
        m = samples.mean()
        coef_significance[feat] = {
            "mean": float(m),
            "prob_same_sign": float((samples > 0).mean() if m > 0 else (samples < 0).mean())
        }
    with open(version_dir / "coef_significance.json", "w") as f:
        json.dump(coef_significance, f, indent=2)

    metadata = {
        "version_id": version_id,
        "fit_timestamp": timestamp,
        "n_rows": len(df_raw),
        "n_detections_total": sqlite3.connect(DB_PATH).execute("SELECT COUNT(*) FROM detections_alltime").fetchone()[0],
        "n_species": df_raw["Com_Name"].nunique(),
        "n_zero_rows": int((df_scaled["count"] == 0).sum()),
        "features": FIXED_FEATURES + ["hour_of_day (categorical)"] + SEASON_FEATURES,
        "hours": hours_sorted,
        "model_spec": "nb_glmm_hourcat_seasonal_v3",
        "scale_params": scale_params,
        "elpd_loo": fit_diagnostics["elpd_loo"],
        "promoted": False,
    }
    with open(version_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    should_promote = fit_diagnostics["converged"]
    if not should_promote:
        print("Not promoted: R-hat >= 1.05 (did not converge).")

    if should_promote:
        metadata["promoted"] = True
        with open(version_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)
        registry["current_production"] = version_id

    registry["versions"].append(version_id)
    save_registry(registry)

    print(f"Registered {version_id}")
    print(f"ELPD (LOO): {fit_diagnostics['elpd_loo']}")
    print(f"R-hat max: {fit_diagnostics['rhat_max']}")
    print(f"Current production model: {registry['current_production']}")


if __name__ == "__main__":
    main()
