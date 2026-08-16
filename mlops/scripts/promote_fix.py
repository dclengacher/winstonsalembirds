"""One-off: fix v1 (invalid) vs v2 (valid) promotion."""
import json
from pathlib import Path

MODELS_DIR = Path("/home/david/birdnet/mlops/models")
REGISTRY_PATH = MODELS_DIR / "registry.json"

with open(REGISTRY_PATH) as f:
    registry = json.load(f)

v1_meta_path = MODELS_DIR / "v1_20260725_100935" / "metadata.json"
with open(v1_meta_path) as f:
    v1_meta = json.load(f)
v1_meta["promoted"] = False
with open(v1_meta_path, "w") as f:
    json.dump(v1_meta, f, indent=2)

v2_meta_path = MODELS_DIR / "v2_20260725_103759" / "metadata.json"
with open(v2_meta_path) as f:
    v2_meta = json.load(f)
v2_meta["promoted"] = True
with open(v2_meta_path, "w") as f:
    json.dump(v2_meta, f, indent=2)

registry["current_production"] = "v2_20260725_103759"
with open(REGISTRY_PATH, "w") as f:
    json.dump(registry, f, indent=2)

print("v1 demoted, v2 promoted to production.")
