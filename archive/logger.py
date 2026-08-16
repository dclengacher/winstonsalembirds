import time
import sqlite3
import smbus2
import bme280

DB_FILE = "/home/david/BirdNET-Pi/scripts/birds.db"

print("[INIT] Wiping all historical database tables for a fresh reboot session...")
try:
    conn = sqlite3.connect(DB_FILE)
    conn.execute("DELETE FROM weather;")
    conn.execute("DELETE FROM detections;")
    conn.commit()
    conn.close()
    print("[INIT] Database purged successfully.")
except Exception as e:
    print(f"[WARN] Database purge failed: {e}")

print("[INIT] Initializing BME280 sensor...")
bus = smbus2.SMBus(1)
calib = bme280.load_calibration_params(bus, 0x77)

TEST_INTERVAL = 10 
print(f"[START] System Logger Active. Interval: {TEST_INTERVAL}s")

while True:
    try:
        t_sum, h_sum, p_sum = 0, 0, 0
        for i in range(3):
            data = bme280.sample(bus, 0x77, calib)
            t_sum += (data.temperature * 9/5) + 32
            h_sum += data.humidity
            p_sum += data.pressure
            time.sleep(0.2)
            
        avg_t = int(round(t_sum / 3, 0))
        avg_h = int(round(h_sum / 3, 0))
        avg_p = int(round(p_sum / 3, 0))
        
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT OR REPLACE INTO weather (timestamp, temp, humidity, pressure) VALUES (time('now', 'localtime'), ?, ?, ?)", (avg_t, avg_h, avg_p))
        conn.commit()
        conn.close()
        
        print(f"[{time.strftime('%X')}] Sensor Sync Success -> Temp: {avg_t}°F | Hum: {avg_h}% | Pres: {avg_p}hPa")
        
    except Exception as e:
        print(f"[ERROR] Sensor crash: {e}")
        
    time.sleep(TEST_INTERVAL)
