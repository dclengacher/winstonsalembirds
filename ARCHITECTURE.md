# winstonsalembirds.com — Master Architecture Doc

> **Purpose of this file:** a single, always-up-to-date map of the codebase so anyone (human or AI assistant) can get oriented in under a minute — what exists, where it lives, and how the pieces connect. Update it whenever structure changes (new route, new template, new pipeline stage). Treat it as the source of truth that outlives any one chat session.
>
> Last verified against commit: `9de9196` ("make the 6-button nav bar identical and complete across every page"), 2026-08-20.

## 1. What this project is

A live birdwatching dashboard for a backyard station in Winston-Salem, NC. A Raspberry Pi 4 runs [BirdNET-Pi](https://github.com/mcguirepr89/BirdNET-Pi) (Cornell Lab acoustic bird ID) plus an Ecowitt WittBoy weather station and a webcam. This repo is the **Flask web app** that turns that Pi's SQLite data into the public site, plus the **MLOps pipeline** that trains the statistical models behind the "Bird Models" / "Dueling Models" pages.

The app itself runs **on the Raspberry Pi**, not on a conventional host — see `app/main.py`'s hardcoded `/home/david/...` paths in section 4.

## 2. Directory tree

```
.
├── app/                        # The Flask web application (what serves the live site)
│   ├── main.py                 # Single-file Flask app: all routes, all DB queries, all page data-prep
│   ├── push_alltime.py         # Cron/manual script: pushes all-time species table to Cloudflare Worker fallback
│   └── push_snapshot.py        # Cron/manual script: pushes "top species today" snapshot to the same fallback
│
├── templates/                  # Jinja2 HTML templates — ONE PER PAGE, each fully self-contained (see §5)
│   ├── dashboard.html          # "/" — live feed, today's detections, weather charts, awards, subscribe form
│   ├── alltime.html            # "/alltime" — all-time species leaderboard table
│   ├── bird_models.html        # "/bird-models" — per-species modeled hourly activity ("bird clock")
│   ├── dueling_models.html     # "/dueling-models" — side-by-side comparison of two statistical models
│   ├── seasonal_trends.html    # "/seasonal-trends" — 26-week rolling share-of-detections by species
│   ├── analytics.html          # "/analytics" — model/promotion internals, charts, formulas (the "nerd" page)
│   └── history.html            # "/history" — project build log / hardware story, with photos
│
├── static/
│   ├── Dashboard.png            # og:image / social preview screenshot
│   └── history/                 # Photos referenced by history.html (build progression, hardware closeups)
│
├── mlops/
│   └── scripts/                 # Offline pipeline that produces the models bird_models/dueling_models read
│       ├── extract.py           # Pull raw detections out of the BirdNET-Pi SQLite DB
│       ├── fit_baseline.py      # Fit the baseline (simpler) statistical model
│       ├── train_categorical.py # Train the categorical hourly-activity model
│       ├── train_and_register.py# Train + register a model version (the "current_version" seen on bird_models.html)
│       ├── trigger_check.py     # Decide whether conditions are met to kick off a retrain
│       ├── check_result_attrs.py# Debug/inspection helper for model result objects
│       └── promote_fix.py       # One-off script (already run) fixing a bad v1→v2 model promotion
│
├── archive/                     # Retired code, kept for reference only — not imported by anything live
│   ├── dashboard.py             # Earlier standalone Flask dashboard, superseded by app/main.py
│   └── logger.py                # Earlier BME280 sensor logger, superseded by the WittBoy integration
│
└── .gitignore                   # Excludes venvs, model artifacts, logs, live.jpg, .env, db files, etc.
```

Notably **absent**: no `base.html`/layout template, no shared `.css` file, no `requirements.txt`/`Dockerfile`/CI config in this repo (deployment/dependencies are presumably managed by hand on the Pi — worth confirming and documenting here once known).

## 3. Request flow (how a page actually gets built)

1. Browser hits a route in `app/main.py` (e.g. `GET /seasonal-trends`).
2. The route handler opens `sqlite3.connect(DB_FILE)` directly (no ORM) and/or calls a helper like `get_seasonal_trend_data()` / `get_dueling_data()` / `load_mlops_data()` to shape query results into template variables.
3. `render_template("<page>.html", ...)` renders the Jinja2 template — each template includes its **own** `<style>` block; nothing is shared (see §5).
4. Response gets `Cache-Control: no-store, must-revalidate` so pages always reflect live data.

Two side-channel scripts (`push_alltime.py`, `push_snapshot.py`) run independently (cron, presumably) and POST pre-rendered HTML snippets to a Cloudflare Worker (`birdnet-fallback.dclengacher.workers.dev`) — a fallback path so parts of the site can still show recent data if the Pi itself is unreachable.

`/data/report/` is a webhook-style endpoint the WittBoy weather station posts readings to directly.

## 4. Known environment coupling (things that look like config but are hardcoded)

- `Flask(template_folder="/home/david/birdnet/templates", static_folder="/home/david/birdnet/static")` — absolute path to the Pi's filesystem.
- `DB_FILE = "/home/david/BirdNET-Pi/scripts/birds.db"` — the BirdNET-Pi SQLite database.
- `AUDIO_BASE = "/home/david/BirdSongs/Extracted/By_Date"` — served via `/audio/<date>/<species>/<filename>`.
- `MLOPS_MODELS_DIR = "/home/david/birdnet/mlops/models"` — gitignored, populated by the mlops pipeline.
- Secrets (`EBIRD_API_KEY`, `CLOUDFLARE_WORKER_SECRET`) load from a local `.env` via `python-dotenv` — not in the repo.

This means the app is **not runnable as-is outside the Pi** without recreating that path layout and `.env`. Worth a documented setup script if that ever needs to change.

## 5. Frontend architecture — the root cause behind the styling issues we're fixing

Every template is a **fully standalone HTML document**: its own `<head>`, its own inline `<style>` block, no `<link rel="stylesheet">` to anything shared, and no template inheritance (`{% extends %}` is not used anywhere). This was true as of the last check across all 7 templates.

Consequences, confirmed by direct inspection (2026-08-20):

- **Font sizing drifts per page** because there is no shared type scale. Six of seven pages leave `h1`/`body` at browser defaults with plain `font-family: sans-serif`; `analytics.html` alone sets `font-family: Arial, sans-serif` and explicitly shrinks `h1` to `1.15em` / `h2` to `0.95em` — so that page's headings render visibly smaller/denser than every other page. Page subtitle paragraphs also use inconsistent one-off sizes across pages (`0.85rem`, `0.9rem`, `1rem`, `1.05rem` for what is conceptually the same element).
- **Only `analytics.html` has a `@media (max-width: 600px)` block.** The other six templates have zero mobile-specific rules, so nothing shrinks or reflows for phones on those pages.
- **The 6-button `.nav-bar`/`.nav-link` block is copy-pasted into all 7 templates** (identical CSS, byte-for-byte, as of the last nav-consistency commit). This is good news for a fix — one CSS change applied identically to all 7 files fixes every page at once — but it also means `.nav-link`'s generous `padding:14px 22px` and `min-width:140px` currently has **no mobile override anywhere**, so on a phone the row wraps into 2–3 tall rows and pushes page content apart, which is the vertical-crowding issue reported.

**Recommended fix (not yet applied):** extract the duplicated nav CSS + a small shared type scale into one real stylesheet (e.g. `static/site.css`), link it from all 7 templates, add one mobile media query that shrinks `.nav-link` padding/font-size and normalizes heading sizes site-wide. Because everything is currently duplicated per-page rather than templated, this is a mechanical, low-risk change — but it does require touching all 7 files (or introducing `{% extends %}`/Jinja includes) since there's no single layout file today.

## 6. MLOps pipeline (high level — scripts are not heavily commented, revisit if behavior needs more precision)

`mlops/scripts/extract.py` pulls detections from the BirdNET-Pi DB → `fit_baseline.py` / `train_categorical.py` fit the two competing statistical models referenced on `dueling_models.html` → `train_and_register.py` registers a trained model version (surfaced on `bird_models.html` as "model version {{ clock_data.current_version }}") → `trigger_check.py` presumably gates when a retrain should run (nightly, per the `bird_models.html` copy "retrained nightly"). `check_result_attrs.py` and `promote_fix.py` are debug/one-off utilities, not part of the steady-state pipeline.

## 7. Deployment & ops (confirmed via SSH, 2026-08-20)

- **Host:** Debian 13 (trixie), Python 3.13.5, Raspberry Pi.
- **Process:** runs as systemd service `birdnet-flask.service` (part of a larger BirdNET-Pi service suite: `birdnet_analysis`, `birdnet_recording`, `birdnet_stats`, `chart_viewer`, `livestream`, `spectrogram_viewer`, `web_terminal`). Flask listens on `0.0.0.0:5000`.
- **Public exposure:** Caddy listens on `:80` in front of it; `birdnet-tunnel.service` (Cloudflare Tunnel) exposes it externally — no public port-forward.
- **Deploy mechanism:** `/home/david/birdnet` on the Pi *is* the git working copy of this repo (confirmed push access works from the Pi). Deploying = `git pull` on the Pi + restart `birdnet-flask.service`. No CI/CD or auto-deploy hook exists.
- **Dependencies:** no scoped `requirements.txt` — `pip freeze` on the Pi is the whole system environment (hundreds of unrelated GPIO/Adafruit/type-stub packages), not a clean manifest for just this Flask app. Worth extracting a real `requirements.txt` at some point.
- **Cron (`crontab -u david`):**
  - hourly: back up `birds.db`
  - every 5 min: merge new rows into `detections_alltime`
  - every 10 min: `push_snapshot.py` and `push_alltime.py` (Cloudflare Worker fallback)
  - `15 21 * * *`: `mlops/scripts/trigger_check.py` (via `mlops_venv`)
  - `55 23 * * *`: `mlops/scripts/train_categorical.py` (via `mlops_venv`) — this is the nightly retrain referenced on `bird_models.html`
- **Known drift risk:** the live `templates/` and `app/` dirs on the Pi contain several gitignored `.bak*` files (e.g. `main.py.bak` through `.bak8`, `dashboard.html.bak2-4`, `history.html.bak*`) — manual rollback copies from past edits, not tracked in git. `git status` on the Pi currently shows a clean tree, so no *uncommitted* drift right now, but these backups are a sign edits have sometimes happened directly on the Pi rather than through the repo — worth pushing any real change through git so this doc and the live site can't silently diverge.

---
*This doc is maintained by hand (or by an assistant with repo access) alongside code changes — it is not auto-generated. If you change routes, templates, or the pipeline, update the relevant section above in the same change.*
