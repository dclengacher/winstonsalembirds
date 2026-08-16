import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
import sqlite3
import subprocess

DB_FILE = "/home/david/BirdNET-Pi/scripts/birds.db"
WORKER_URL = "https://birdnet-fallback.dclengacher.workers.dev/api/update-alltime"
SECRET = os.environ["CLOUDFLARE_WORKER_SECRET"]

conn = sqlite3.connect(DB_FILE)
rows = conn.execute("""
    SELECT Com_Name, MAX(Date) as Last_Heard, COUNT(*) as Total_Count
    FROM detections_alltime
    GROUP BY Com_Name
    ORDER BY Total_Count DESC
    LIMIT 100
""").fetchall()
conn.close()

html = "<table><tr><th>#</th><th>Species</th><th>Last Heard</th><th>Total Detections</th></tr>"
for i, (name, last_heard, count) in enumerate(rows, start=1):
    html += f"<tr><td>{i}</td><td>{name}</td><td>{last_heard}</td><td>{count}</td></tr>"
html += "</table>"

subprocess.run([
    "curl", "-s", "-X", "POST", WORKER_URL,
    "-H", f"X-Auth-Key: {SECRET}",
    "-d", html
])

print(f"[ALLTIME] Pushed {len(rows)} species")
