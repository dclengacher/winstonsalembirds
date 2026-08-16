"""
Fit hierarchical negative-binomial model in PyMC, save all fit outputs
(fixed effects, random effects, fit diagnostics) immediately after sampling.
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
FEATURES = FIXED_FEATURES + ["time_of_day"]


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
    for col in FEATURES:
        mean, std = df[col].mean(), df[col].std()
        scale_params[col] = {"mean": mean, "std": std}
        df[col] = (df[col] - mean) / std
    df["time_of_day2"] = df["time_of_day"] ** 2

    species_names = sorted(df["Com_Name"].unique())
    species_idx_map = {name: i for i, name in enumerate(species_names)}
    species_idx = df["Com_Name"].map(species_idx_map).values
    n_species = len(species_names)

    with pm.Model() as model:
        intercept = pm.Normal("intercept", mu=0, sigma=2)
        betas = {
            feat: pm.Normal(f"beta_{feat}", mu=0, sigma=1) for feat in FIXED_FEATURES
        }
        beta_time = pm.Normal("beta_time_of_day", mu=0, sigma=1)
        beta_time2 = pm.Normal("beta_time_of_day2", mu=0, sigma=1)

        mu_species = pm.Normal("mu_species", mu=0, sigma=1)
        sigma_species = pm.HalfNormal("sigma_species", sigma=1)
        species_offset = pm.Normal(
            "species_offset", mu=mu_species, sigma=sigma_species, shape=n_species
        )

        sigma_time_species = pm.HalfNormal("sigma_time_species", sigma=0.5)
        b_time_species = pm.Normal(
            "b_time_species", mu=0, sigma=sigma_time_species, shape=n_species
        )
        sigma_time2_species = pm.HalfNormal("sigma_time2_species", sigma=0.5)
        b_time2_species = pm.Normal(
            "b_time2_species", mu=0, sigma=sigma_time2_species, shape=n_species
        )

        linpred = intercept + species_offset[species_idx]
        for feat in FIXED_FEATURES:
            linpred = linpred + betas[feat] * df[feat].values
        linpred = linpred + (beta_time + b_time_species[species_idx]) * df["time_of_day"].values
        linpred = linpred + (beta_time2 + b_time2_species[species_idx]) * df["time_of_day2"].values

        mu = pm.math.exp(linpred)
        alpha = pm.Exponential("alpha", 1.0)

        y_obs = pm.NegativeBinomial(
            "y_obs", mu=mu, alpha=alpha, observed=df["count"].values
        )

        trace = pm.sample(1000, tune=1000, chains=4, progressbar=False, random_seed=42)
        pm.compute_log_likelihood(trace)

    return trace, model, scale_params, df, species_names, species_idx


def extract_fixed_effects(trace):
    var_names = ["intercept"] + [f"beta_{f}" for f in FIXED_FEATURES] + ["beta_time_of_day", "beta_time_of_day2"]
    summary = az.summary(trace, var_names=var_names)
    return {
        idx: {"mean": float(row["mean"]), "sd": float(row["sd"])}
        for idx, row in summary.iterrows()
    }


def extract_random_effects(trace, species_names):
    offset_summary = az.summary(trace, var_names=["species_offset"])
    time_summary = az.summary(trace, var_names=["b_time_species"])
    time2_summary = az.summary(trace, var_names=["b_time2_species"])
    result = {}
    for i, name in enumerate(species_names):
        result[name] = {
            "offset_mean": float(offset_summary.iloc[i]["mean"]),
            "offset_sd": float(offset_summary.iloc[i]["sd"]),
            "time_slope_mean": float(time_summary.iloc[i]["mean"]),
            "time_slope_sd": float(time_summary.iloc[i]["sd"]),
            "time2_slope_mean": float(time2_summary.iloc[i]["mean"]),
            "time2_slope_sd": float(time2_summary.iloc[i]["sd"]),
        }
    return result


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


def predict(trace, df, species_idx):
    post = trace.posterior
    intercept = post["intercept"].mean().values
    species_offset = post["species_offset"].mean(dim=["chain", "draw"]).values
    b_time_species = post["b_time_species"].mean(dim=["chain", "draw"]).values
    b_time2_species = post["b_time2_species"].mean(dim=["chain", "draw"]).values
    betas = {feat: post[f"beta_{feat}"].mean().values for feat in FIXED_FEATURES}
    beta_time = post["beta_time_of_day"].mean().values
    beta_time2 = post["beta_time_of_day2"].mean().values

    linpred = intercept + species_offset[species_idx]
    for feat in FIXED_FEATURES:
        linpred = linpred + betas[feat] * df[feat].values
    linpred = linpred + (beta_time + b_time_species[species_idx]) * df["time_of_day"].values
    linpred = linpred + (beta_time2 + b_time2_species[species_idx]) * df["time_of_day2"].values
    return np.exp(linpred)


def main():
    df_raw = pd.read_csv(DATA_PATH)
    top20 = df_raw.groupby("Com_Name")["count"].sum().sort_values(ascending=False).head(20).index
    df_raw = df_raw[df_raw["Com_Name"].isin(top20)].reset_index(drop=True)
    trace, model, scale_params, df_scaled, species_names, species_idx = fit_model(df_raw)

    fixed_effects = extract_fixed_effects(trace)
    random_effects = extract_random_effects(trace, species_names)
    fit_diagnostics = extract_fit_diagnostics(trace, df_scaled)
    mu_pred = predict(trace, df_scaled, species_idx)

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

    with open(version_dir / "fit_diagnostics.json", "w") as f:
        json.dump(fit_diagnostics, f, indent=2)

    coef_significance = {}
    sig_vars = FIXED_FEATURES + ["time_of_day", "time_of_day2"]
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
        "features": FEATURES + ["time_of_day2"],
        "scale_params": scale_params,
        "elpd_loo": fit_diagnostics["elpd_loo"],
        "promoted": False,
    }
    with open(version_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    current = registry["current_production"]
    should_promote = current is None

    if not should_promote and current is not None:
        current_meta_path = MODELS_DIR / current / "metadata.json"
        with open(current_meta_path) as f:
            current_meta = json.load(f)
        current_elpd = current_meta.get("elpd_loo")
        if current_elpd is None:
            should_promote = fit_diagnostics["elpd_loo"] is not None
        else:
            should_promote = (fit_diagnostics["elpd_loo"]/fit_diagnostics["n_rows"]) > (current_elpd/current_meta["n_rows"])

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
