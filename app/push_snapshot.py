import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
import sqlite3
import subprocess
from datetime import datetime

DB_FILE = "/home/david/BirdNET-Pi/scripts/birds.db"
WORKER_URL = "https://birdnet-fallback.dclengacher.workers.dev/api/update-snapshot"
SECRET = os.environ["CLOUDFLARE_WORKER_SECRET"]

conn = sqlite3.connect(DB_FILE)

session_date_row = conn.execute("""
    SELECT Date FROM detections_alltime
    GROUP BY Date
    HAVING COUNT(DISTINCT Com_Name) >= 5
    ORDER BY Date DESC LIMIT 1
""").fetchone()

if session_date_row:
    session_date = session_date_row[0]
    today = datetime.strptime(session_date, "%Y-%m-%d").strftime("%B %d, %Y")
else:
    session_date = None
    today = "an earlier session"

if session_date:
    rows = conn.execute("""
        SELECT Com_Name, COUNT(*) as cnt
        FROM detections_alltime
        WHERE Date = ?
        GROUP BY Com_Name
        ORDER BY cnt DESC
        LIMIT 10
    """, (session_date,)).fetchall()
else:
    rows = []

conn.close()

if rows:
    html = f"<p><strong>Top species from {today}:</strong></p><table>"
    html += "<tr><th>Species</th><th>Detections</th></tr>"
    for name, count in rows:
        html += f"<tr><td>{name}</td><td>{count}</td></tr>"
    html += "</table>"
else:
    html = f"<p>No qualifying session found yet.</p>"

subprocess.run([
    "curl", "-s", "-X", "POST", WORKER_URL,
    "-H", f"X-Auth-Key: {SECRET}",
    "-d", html
])

print(f"[SNAPSHOT] Pushed {len(rows)} species for {today}")
