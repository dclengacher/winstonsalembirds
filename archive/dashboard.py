import os
import sqlite3
import subprocess
import time
from flask import Flask, render_template

DB_FILE = "/home/david/BirdNET-Pi/scripts/birds.db"

print("[INIT] Launching native headless rpicam video stream pipeline...")
try:
    cmd = (
        "rpicam-vid -n -t 0 --inline --width 640 --height 480 --framerate 15 -o - | "
        "ffmpeg -y -f h264 -i pipe:0 -c:v copy -f mpegts 'srt://127.0.0.1:4242?mode=listener'"
    )
    subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[INIT] Headless video pipeline running safely in background.")
except Exception as e:
    print(f"[WARN] Video pipeline failed to initialize: {e}")

app = Flask(__name__, template_folder="/home/david/birdnet/templates", static_folder="/home/david/birdnet/static")

@app.route("/")
def dashboard():
    conn = sqlite3.connect(DB_FILE)
    birds = conn.execute("""
        SELECT Com_Name, Sci_Name, MAX(Date || ' ' || Time) AS Last_Heard, COUNT(*) AS Total_Count, ROUND(AVG(Confidence) * 100, 0) AS Avg_Conf
        FROM detections GROUP BY Com_Name ORDER BY Last_Heard DESC LIMIT 10
    """).fetchall()

    chart = conn.execute("""
        SELECT substr(Time,1,2) || ':' || printf('%02d', (CAST(substr(Time,4,2) AS INTEGER)/15)*15) AS block, COUNT(DISTINCT Com_Name)
        FROM detections GROUP BY block ORDER BY block ASC;
    """).fetchall()

    weather_data = conn.execute("SELECT timestamp, CAST(temp AS INTEGER), CAST(humidity AS INTEGER), CAST(pressure AS INTEGER) FROM weather ORDER BY timestamp ASC").fetchall()
    conn.close()

    weather_labels = [row[0] for row in weather_data]
    temps = [row[1] for row in weather_data]
    humidities = [row[2] for row in weather_data]
    pressures = [row[3] for row in weather_data]

    return render_template(
        "dashboard.html", birds=birds, chart=chart, weather_labels=weather_labels,
        temps=temps, humidities=humidities, pressures=pressures
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
