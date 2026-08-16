"""
Run daily at 9:15pm via cron. Checks if enough new data has accumulated
since the last registered version before invoking train_and_register.py.
"""
import json
import subprocess
import sqlite3
from pathlib import Path

DB_PATH = "/home/david/BirdNET-Pi/scripts/birds.db"
MODELS_DIR = Path("/home/david/birdnet/mlops/models")
REGISTRY_PATH = MODELS_DIR / "registry.json"
MIN_NEW_ROWS = 50  # minimum new detection rows to justify a retrain

def get_total_detection_count():
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM detections_alltime").fetchone()[0]
    conn.close()
    return count

def main():
    if not REGISTRY_PATH.exists():
        print("No registry yet -- triggering first training run.")
        subprocess.run(["python3", "/home/david/birdnet/mlops/scripts/extract.py"])
        subprocess.run(["/home/david/birdnet/mlops_venv/bin/python3", "/home/david/birdnet/mlops/scripts/train_and_register.py"])
        return

    with open(REGISTRY_PATH) as f:
        registry = json.load(f)

    current = registry.get("current_production")
    if current is None:
        last_n_rows = 0
    else:
        with open(MODELS_DIR / current / "metadata.json") as f:
            last_n_rows = json.load(f).get("n_detections_total", 0)

    current_total = get_total_detection_count()
    new_rows = current_total - last_n_rows

    print(f"Detections at last registered fit: {last_n_rows}")
    print(f"Detections now: {current_total}")
    print(f"New rows: {new_rows}")

    if new_rows >= MIN_NEW_ROWS:
        print("Threshold met -- retraining.")
        subprocess.run(["python3", "/home/david/birdnet/mlops/scripts/extract.py"])
        subprocess.run(["/home/david/birdnet/mlops_venv/bin/python3", "/home/david/birdnet/mlops/scripts/train_and_register.py"])
    else:
        print("Not enough new data -- skipping retrain.")

if __name__ == "__main__":
    main()
