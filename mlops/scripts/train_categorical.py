"""
Model comparison variant: hour treated as a categorical fixed effect + per-species
random deviation, instead of a quadratic curve. Supports bimodal/arbitrary shapes.
Runs nightly via cron, writes versioned output for the Dueling Models page.
Never touches the production registry.
"""
import json
import numpy as np
import pandas as pd
import pymc as pm
import arviz as az
from pathlib import Path
from datetime import datetime

DATA_PATH = "/home/david/birdnet/mlops/scripts/extracted_data.csv"
EXP_DIR = Path("/home/david/birdnet/mlops/experiments/categorical_hour")
EXP_DIR.mkdir(parents=True, exist_ok=True)
REGISTRY_PATH = EXP_DIR / "registry.json"

FIXED_FEATURES = ["tempf", "humidity", "baromrelin", "dailyrain"]


def load_registry():
    if REGISTRY_PATH.exists():
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    return {"versions": []}


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

    hours_sorted = sorted(df["time_of_day"].unique())
    hour_idx_map = {h: i for i, h in enumerate(hours_sorted)}
    hour_idx = df["time_of_day"].map(hour_idx_map).values
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
        hour_species = pm.Normal(
            "hour_species", mu=0, sigma=sigma_hour_species, shape=(n_species, n_hours)
        )

        linpred = intercept + species_offset[species_idx] + hour_effect[hour_idx]
        linpred = linpred + hour_species[species_idx, hour_idx]
        for feat in FIXED_FEATURES:
            linpred = linpred + betas[feat] * df[feat].values

        mu = pm.math.exp(linpred)
        alpha = pm.Exponential("alpha", 1.0)

        y_obs = pm.NegativeBinomial(
            "y_obs", mu=mu, alpha=alpha, observed=df["count"].values
        )

        trace = pm.sample(1000, tune=1000, chains=4, progressbar=False, random_seed=42)
        pm.compute_log_likelihood(trace)

    return trace, hours_sorted, species_names, scale_params


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    df_raw = pd.read_csv(DATA_PATH)
    top20 = df_raw.groupby("Com_Name")["count"].sum().sort_values(ascending=False).head(20).index
    df_raw = df_raw[df_raw["Com_Name"].isin(top20)].reset_index(drop=True)

    trace, hours_sorted, species_names, scale_params = fit_model(df_raw)

    loo = az.loo(trace)
    rhat_summary = az.rhat(trace)
    rhat_max = float(max(float(rhat_summary[var].max()) for var in rhat_summary.data_vars))

    fit_diagnostics = {
        "model": "categorical_hour",
        "elpd_loo": float(loo["elpd"]),
        "elpd_loo_se": float(loo["se"]),
        "p_loo": float(loo["p"]),
        "n_rows": len(df_raw),
        "n_species": len(species_names),
        "n_hours": len(hours_sorted),
        "rhat_max": rhat_max,
        "converged": rhat_max < 1.05,
    }

    fixed_summary = az.summary(trace, var_names=["intercept"] + [f"beta_{f}" for f in FIXED_FEATURES])
    fixed_effects = {
        idx: {"mean": float(row["mean"]), "sd": float(row["sd"])}
        for idx, row in fixed_summary.iterrows()
    }

    hour_effect_summary = az.summary(trace, var_names=["hour_effect"])
    hour_effect = {
        str(hours_sorted[i]): {"mean": float(row["mean"]), "sd": float(row["sd"])}
        for i, (_, row) in enumerate(hour_effect_summary.iterrows())
    }

    post = trace.posterior
    hour_species_mean = post["hour_species"].mean(dim=["chain", "draw"]).values
    random_effects = {}
    for si, name in enumerate(species_names):
        random_effects[name] = {
            str(hours_sorted[hi]): round(float(hour_species_mean[si, hi]), 4)
            for hi in range(len(hours_sorted))
        }

    registry = load_registry()
    version_num = len(registry["versions"]) + 1
    version_id = f"cv{version_num}_{timestamp}"
    version_dir = EXP_DIR / version_id
    version_dir.mkdir(parents=True, exist_ok=True)

    with open(version_dir / "fit_diagnostics.json", "w") as f:
        json.dump(fit_diagnostics, f, indent=2)
    with open(version_dir / "fixed_effects.json", "w") as f:
        json.dump(fixed_effects, f, indent=2)
    with open(version_dir / "hour_effect.json", "w") as f:
        json.dump(hour_effect, f, indent=2)
    with open(version_dir / "random_effects.json", "w") as f:
        json.dump(random_effects, f, indent=2)

    metadata = {
        "version_id": version_id,
        "fit_timestamp": timestamp,
        "n_rows": len(df_raw),
        "n_species": len(species_names),
        "scale_params": scale_params,
        "elpd_loo": fit_diagnostics["elpd_loo"],
        "elpd_loo_se": fit_diagnostics["elpd_loo_se"],
        "p_loo": fit_diagnostics["p_loo"],
        "rhat_max": fit_diagnostics["rhat_max"],
    }
    with open(version_dir / "metadata.json", "w") as f:
        json.dump(metadata, f, indent=2)

    registry["versions"].append(version_id)
    save_registry(registry)

    print(f"Registered {version_id}")
    print(f"ELPD (LOO): {fit_diagnostics['elpd_loo']} +/- {fit_diagnostics['elpd_loo_se']}")
    print(f"p_loo: {fit_diagnostics['p_loo']}")
    print(f"R-hat max: {fit_diagnostics['rhat_max']}")


if __name__ == "__main__":
    main()
