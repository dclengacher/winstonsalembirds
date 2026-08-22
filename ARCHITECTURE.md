# winstonsalembirds.com — Master Architecture Doc

> **Purpose of this file:** a single, always-up-to-date map of the codebase so anyone (human or AI assistant) can get oriented in under a minute — what exists, where it lives, and how the pieces connect. Update it whenever structure changes (new route, new template, new pipeline stage). Treat it as the source of truth that outlives any one chat session.
>
> Last verified against commit: `f6daf69` ("Revert \"add missing viewport meta tag to dashboard/alltime/history...\""), 2026-08-22 — a fresh line-by-line pass over every file in the repo, not just a diff of the previous version of this doc. Section 7 (deployment/ops) is the exception: this pass had no SSH access to the Pi, so that section still reflects the last SSH-confirmed snapshot (2026-08-20) except where the code itself proves a claim wrong.

## 1. What this project is

A live birdwatching dashboard for a backyard station in Winston-Salem, NC. A Raspberry Pi 4 runs [BirdNET-Pi](https://github.com/mcguirepr89/BirdNET-Pi) (Cornell Lab acoustic bird ID) plus an Ecowitt WittBoy weather station and a Pi camera. This repo is the **Flask web app** that turns that Pi's SQLite data into the public site, plus the **MLOps pipeline** that trains the statistical models behind the "Bird Models" / "Dueling Models" pages.

Beyond serving pages, the same Flask process also: polls the eBird API hourly for nearby sightings, runs a background loop that captures still-image snapshots from the Pi camera (cycling through several crops) for the dashboard's live-cam widget, computes sunrise/sunset for the dashboard's "daylight dial," accepts weather-station webhook posts, accepts email signups, and serves a sitemap/robots.txt for SEO. None of this is a separate service — it's all threads and routes inside `app/main.py`.

The app itself runs **on the Raspberry Pi**, not on a conventional host — see `app/main.py`'s hardcoded `/home/david/...` paths in section 4.

## 2. Directory tree

```
.
├── app/                        # The Flask web application (what serves the live site)
│   ├── main.py                 # Single-file Flask app: routes, DB queries, page data-prep,
│   │                            # eBird polling thread, camera-snapshot thread
│   ├── push_alltime.py         # Cron/manual script: pushes all-time species table to Cloudflare Worker fallback
│   └── push_snapshot.py        # Cron/manual script: pushes "top species today" snapshot to the same fallback
│
├── templates/                  # Jinja2 HTML templates — ONE PER PAGE (see §5 for how "shared" they really are)
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
│   ├── site.css                 # UNUSED — see §5. No template links it; dead file left over from a reverted refactor.
│   └── history/                 # Photos referenced by history.html (build progression, hardware closeups)
│
├── mlops/
│   └── scripts/                 # Offline pipeline that produces the models bird_models/dueling_models read
│       ├── extract.py           # Pull raw detections + weather out of the BirdNET-Pi SQLite DB into hourly rows
│       ├── fit_baseline.py      # Early exploratory model (statsmodels), NOT wired into the registry/pipeline — scratch prototype
│       ├── train_categorical.py # Experiment-track model for the Dueling Models page only — never touches the production registry
│       ├── train_and_register.py# Trains + auto-promotes the PRODUCTION model (the one bird_models.html actually reads)
│       ├── trigger_check.py     # Nightly cron gate: decides whether enough new data justifies re-running extract+train_and_register
│       ├── check_result_attrs.py# Debug/inspection helper for fit_baseline.py's result object, not a pipeline stage
│       └── promote_fix.py       # One-off script (already run) fixing a bad v1→v2 model promotion
│
├── archive/                     # Retired code, kept for reference only — not imported by anything live
│   ├── dashboard.py             # Earlier standalone Flask dashboard (used an rpicam-vid+ffmpeg SRT live stream),
│   │                            # superseded by app/main.py's snapshot-polling approach
│   └── logger.py                # Earlier BME280 sensor logger (wiped the DB on every boot), superseded by the WittBoy integration
│
└── .gitignore                   # Excludes venvs, model artifacts, logs, live.jpg, .env, db files, .bak files, etc.
```

**Notably absent:** no `base.html`/layout template and no `{% extends %}` anywhere — each template is still a fully separate `.html` file. What's *no longer* accurate about that old description: it's not true anymore that nothing is shared. A large block of CSS (typography scale, the 6-button nav bar, and one mobile media query) is now byte-for-byte identical across all 7 templates — see §5 for how that's implemented (copy-pasted inline `<style>` blocks, not a linked stylesheet) and what's still inconsistent despite that. There is still no `requirements.txt`/Dockerfile/CI config in this repo.

## 3. Request flow (how a page actually gets built)

1. Browser hits a route in `app/main.py` (e.g. `GET /seasonal-trends`).
2. The route handler opens `sqlite3.connect(DB_FILE)` directly (no ORM) and/or calls a helper like `get_seasonal_trend_data()` / `get_dueling_data()` / `get_bird_clock_data()` / `load_mlops_data()` to shape query results (and, for the model pages, JSON files under `mlops/models/`) into template variables.
3. `render_template("<page>.html", ...)` renders the Jinja2 template — each template includes its **own** `<style>` block (see §5 for how much of that is actually duplicated-but-identical vs. genuinely per-page).

**Caching is not uniform.** Only `/`, `/alltime`, `/history`, and `/analytics` explicitly set `Cache-Control: no-store, must-revalidate` on the response. `/bird-models`, `/dueling-models`, `/seasonal-trends`, `/data/report/`, `/subscribe`, `/audio/<...>`, `/sitemap.xml`, and `/robots.txt` return plain Flask responses with no explicit cache header. (The previous version of this doc stated the no-store header applied to "the response," implying all pages — that was never true of the model pages.)

**Two background daemon threads run inside the same process**, started at import time (before the Flask app object even exists):
- An **eBird polling loop** (`run_ebird_loop`) hits the eBird API every hour for the 5 most recent nearby sightings and stores them in a module-level global (`ebird_sightings`), which `dashboard.html` renders if non-empty.
- A **camera snapshot loop** (`run_camera_loop`) shells out to `rpicam-still` every 60 seconds, cycling through 5 hardcoded ROI crops, and atomically replaces `static/live.jpg` — the image the dashboard's live-cam widget polls. Failures/timeouts are appended to a log file rather than raised.

At startup, `main.py` also runs `fuser -k 5000/tcp` to kill anything already bound to port 5000 before Flask binds — i.e. it assumes it's the only thing that should ever be listening there and will forcibly evict a prior instance (or anything else) on that port.

Two side-channel scripts (`push_alltime.py`, `push_snapshot.py`) run independently (cron, presumably) and POST pre-rendered HTML snippets via `curl` to a Cloudflare Worker (`birdnet-fallback.dclengacher.workers.dev`) — a fallback path so parts of the site can still show recent data if the Pi itself is unreachable.

`/data/report/` is a webhook-style endpoint the WittBoy weather station posts readings to directly. `/audio/<date>/<species>/<filename>` serves BirdNET's recorded audio clips with regex-validated path segments (date/species/filename each individually pattern-checked before hitting the filesystem). `/subscribe` (POST) stores an email address into a `subscribers` table for "notify me" signups from the dashboard. `/sitemap.xml` and `/robots.txt` are hand-built in-code, not files.

On the client side, `dashboard.html` fetches each bird's thumbnail image live from the **Wikipedia API** (by scientific name, two calls per row: a small thumbnail and a full-res link) after the page loads — that data is not server-rendered.

## 4. Known environment coupling (things that look like config but are hardcoded)

- `Flask(template_folder="/home/david/birdnet/templates", static_folder="/home/david/birdnet/static")` — absolute path to the Pi's filesystem.
- `DB_FILE = "/home/david/BirdNET-Pi/scripts/birds.db"` — the BirdNET-Pi SQLite database.
- `AUDIO_BASE = "/home/david/BirdSongs/Extracted/By_Date"` — served via `/audio/<date>/<species>/<filename>`.
- `IMAGE_PATH = "/home/david/birdnet/static/live.jpg"` — where the camera-snapshot thread writes; gitignored.
- `CAM_LOG = "/home/david/birdnet/logs/camera.log"` — camera-loop failure log; gitignored.
- `MLOPS_MODELS_DIR = "/home/david/birdnet/mlops/models"` — gitignored, populated by the mlops pipeline.
- Secrets (`EBIRD_API_KEY`, `CLOUDFLARE_WORKER_SECRET`) load from a local `.env` via `python-dotenv` — not in the repo.

This means the app is **not runnable as-is outside the Pi** without recreating that path layout, hardware (camera, WittBoy), and `.env`. Worth a documented setup script if that ever needs to change.

## 5. Frontend architecture

Every template is still its own standalone `.html` file — no `base.html`, no `{% extends %}`, and every page has its own inline `<style>` block rather than a `<link rel="stylesheet">`. That part of the old description still holds.

What's changed since the last pass (confirmed by diffing the `<style>` blocks across all 7 files):

- **The typography scale and nav bar are now genuinely consistent.** The first ~87 lines of every template's `<style>` block — `body`, `h1`, `h2`, `.page-subtitle`, `.nav-bar`, `.nav-link`, `.nav-link:hover`, and one `@media (max-width: 600px)` rule — are **byte-for-byte identical** across all 7 templates. The previous version of this doc reported `analytics.html` alone using a smaller/denser heading scale (`Arial`, `h1: 1.15em`, `h2: 0.95em`) with no mobile media query anywhere else — that issue is **fixed**: `analytics.html` now uses the same shared block as everyone else, and all 7 templates carry the same mobile media query. This is implemented by copy-pasting an identical block into each file's `<style>`, not by linking a shared stylesheet.
- **`static/site.css` exists but is dead code.** Git history shows a shared stylesheet was added (`3d0864a`), linked from templates (`3fbe98d`), reverted, reapplied, and finally abandoned in favor of inlining the same CSS directly into every template (`33228bf`, commit message: "remove external stylesheet dependency"). Nobody deleted the now-orphaned `static/site.css` file itself — no template references it (`grep` for `site.css` or `<link rel="stylesheet"` across `templates/` returns nothing).
- **The nav bar's link set correctly varies per page** (each page omits the link to itself, so it's 6 links out of 7 possible destinations) — that's intentional, not drift, and every page includes it (including `history.html`, which structures it with an extra inline `style="margin-bottom:15px;"` but the same `.nav-bar`/`.nav-link` classes).
- **`viewport` meta tag is inconsistently present**, and this is a live, unresolved issue: `analytics.html`, `bird_models.html`, `dueling_models.html`, and `seasonal_trends.html` have `<meta name="viewport" content="width=device-width, initial-scale=1">`; `dashboard.html`, `alltime.html`, and `history.html` do not. A commit adding the missing tag to those three (`eab8b49`) was reverted (`f6daf69`) as of the current HEAD, so as of this doc those 3 pages still lack it — meaning the shared mobile media query those pages carry won't behave as intended on an actual phone (mobile browsers without a viewport tag render at a virtual desktop-width viewport and scale down, rather than applying the `max-width: 600px` breakpoint against the real device width).
- **`.page-subtitle` is dead CSS.** It's defined identically in every template's shared block, but `grep -c 'class="page-subtitle"'` across all 7 templates returns 0 — every page's actual subtitle line uses one-off inline `style="..."` attributes instead of the class that exists specifically for that purpose.
- Smaller, page-by-page inconsistencies: `<html lang="en">` is set on `dashboard.html`, `alltime.html`, and `history.html` but not the other four (bare `<html>`); Google Analytics (`gtag.js`) is only loaded on `dashboard.html` and `alltime.html`, not the other five pages; Chart.js is loaded (via CDN, not vendored) on `dashboard.html`, `analytics.html`, and `dueling_models.html` only.

**If this gets revisited:** the duplication itself is no longer the acute problem it used to be (the shared block is consistent), but the duplication is still fragile — a future edit to one file's copy of that block and not the other six will silently reintroduce the old drift. Either commit to the copy-paste approach and delete the orphaned `static/site.css`, or finish the abandoned stylesheet migration and delete the duplicated blocks instead. Independently of that choice: add the missing `viewport` tags to `dashboard.html`/`alltime.html`/`history.html`, and either use `.page-subtitle` in the markup or delete it from the CSS.

## 6. MLOps pipeline

`mlops/scripts/extract.py` pulls detections + WittBoy weather from the BirdNET-Pi DB (since 2026-07-21), joins them at hourly grain, and explicitly fills in true zero-count rows for every daylight hour (5am–8pm) × every species combination — so "no calls that hour" is a real `0`, not a missing row.

Two independent modeling scripts both consume `extracted_data.csv` (each capped to the top 20 species by total count) and both fit a hierarchical Bayesian negative-binomial model in PyMC, but they serve different pages and are **not** the same pipeline stage:

- **`train_and_register.py` is the production pipeline.** It fits species-level random intercepts plus per-species linear+quadratic time-of-day slopes (with fixed effects for temp/humidity/pressure/rain), versions the result (`v{n}_{timestamp}`) under `mlops/models/`, and **auto-promotes** it to `current_production` in `registry.json` if there's no current model yet, or if its ELPD-per-row beats the current production model's. This is the model `bird_models.html` reads (`clock_data.current_version` in `app/main.py`'s `get_bird_clock_data()`), and also what `analytics.html` reads via `load_mlops_data()`.
- **`train_categorical.py` is a separate experiment track, by its own docstring: "Never touches the production registry."** It swaps the smooth quadratic time-of-day curve for a categorical (one-effect-per-hour) treatment, writes its own versioned output under `mlops/experiments/categorical_hour/` with its own `registry.json`, and exists solely so `/dueling-models` (via `get_dueling_data()`) can compare the two approaches' fit quality (ELPD) date-by-date. **Correction to the previous version of this doc:** it previously described `train_categorical.py` as "the nightly retrain referenced on `bird_models.html`" — that's wrong. `bird_models.html` is fed by `train_and_register.py`, not `train_categorical.py`.
- **`trigger_check.py`** is the actual gate in front of the production retrain: per its own docstring it runs nightly (21:15) and only invokes `extract.py` + `train_and_register.py` (via `mlops_venv`) if at least 50 new detection rows have accumulated since the currently-registered model was fit, or if there's no registry yet.
- **`fit_baseline.py`** and **`check_result_attrs.py`** are an earlier, simpler prototype (`statsmodels.PoissonBayesMixedGLM`) that prints a summary/attribute list to stdout and writes nothing to disk — they don't participate in the registry/versioning/promotion flow at all. Read as a scratch exploration that predates the PyMC pipeline, not an active stage.
- **`promote_fix.py`** is a one-off script (already run) that manually fixed a bad v1→v2 production promotion by hand-editing `registry.json` and both versions' `metadata.json`.

## 7. Deployment & ops

*(This section reflects the last SSH-confirmed snapshot, 2026-08-20; this pass had no shell access to the Pi to re-verify it live. The pipeline **behavior** described in §6 above, by contrast, is re-verified directly against the current scripts.)*

- **Host:** Debian 13 (trixie), Python 3.13.5, Raspberry Pi.
- **Process:** runs as systemd service `birdnet-flask.service` (part of a larger BirdNET-Pi service suite: `birdnet_analysis`, `birdnet_recording`, `birdnet_stats`, `chart_viewer`, `livestream`, `spectrogram_viewer`, `web_terminal`). Flask listens on `0.0.0.0:5000`.
- **Public exposure:** Caddy listens on `:80` in front of it; `birdnet-tunnel.service` (Cloudflare Tunnel) exposes it externally — no public port-forward.
- **Deploy mechanism:** `/home/david/birdnet` on the Pi *is* the git working copy of this repo (confirmed push access works from the Pi). Deploying = `git pull` on the Pi + restart `birdnet-flask.service`. No CI/CD or auto-deploy hook exists.
- **Dependencies:** no scoped `requirements.txt` — `pip freeze` on the Pi is the whole system environment (hundreds of unrelated GPIO/Adafruit/type-stub packages), not a clean manifest for just this Flask app. From this pass's import scan, the app and pipeline actually depend on: `flask`, `python-dotenv`, `requests`, `astral`, `pytz`, `sqlite3` (stdlib), `pandas`, `numpy`, `pymc`, `arviz`, and (only in the non-pipeline exploratory scripts) `statsmodels`. `smbus2`/`bme280` are only used by the retired `archive/logger.py`. Worth extracting a real `requirements.txt` at some point.
- **Cron (`crontab -u david`), as last confirmed via SSH:**
  - hourly: back up `birds.db`
  - every 5 min: merge new rows into `detections_alltime`
  - every 10 min: `push_snapshot.py` and `push_alltime.py` (Cloudflare Worker fallback)
  - `15 21 * * *`: `mlops/scripts/trigger_check.py` (via `mlops_venv`) — the actual gate for the production model retrain (see §6)
  - `55 23 * * *`: `mlops/scripts/train_categorical.py` (via `mlops_venv`) — feeds the `/dueling-models` experiment comparison, **not** `bird_models.html` (correcting the previous version of this doc — see §6)
- **Known drift risk:** the live `templates/` and `app/` dirs on the Pi contain several gitignored `.bak*` files (e.g. `main.py.bak` through `.bak8`, `dashboard.html.bak2-4`, `history.html.bak*`) — manual rollback copies from past edits, not tracked in git. As of the last SSH check, `git status` on the Pi showed a clean tree, so no *uncommitted* drift at that time — but these backups are a sign edits have sometimes happened directly on the Pi rather than through the repo, and this pass could not re-confirm the tree is still clean. Worth pushing any real change through git so this doc and the live site can't silently diverge.

---
*This doc is maintained by hand (or by an assistant with repo access) alongside code changes — it is not auto-generated. If you change routes, templates, or the pipeline, update the relevant section above in the same change.*
