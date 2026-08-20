# Architecture (short)

Flask app on Pi (`/home/david/birdnet`). Routes/queries in `app/main.py`. 7 standalone templates in `templates/`, each with own inline `<style>` (no shared CSS — root cause of font-size drift). Only `analytics.html` has a mobile media query. Static assets in `static/`. MLOps pipeline in `mlops/scripts/` trains nightly models (cron 23:55). Deploy: `git pull` + restart `birdnet-flask.service`. Fronted by Caddy + Cloudflare Tunnel.
