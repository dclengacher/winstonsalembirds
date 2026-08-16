"""
Extract and join detections + wittboy weather data at hourly grain.
Includes TRUE ZEROS: for each date in range, generates all daylight hour bins
(5am-8pm, i.e. hour_of_day 5-20) crossed with every species ever detected,
so hours with no calls appear as count=0 rather than being absent.
"""
import sqlite3
import pandas as pd

DB_PATH = "/home/david/BirdNET-Pi/scripts/birds.db"

DAYLIGHT_START_HOUR = 5   # 5am
DAYLIGHT_END_HOUR = 20    # 8pm bin (8pm-9pm window)


def extract():
    conn = sqlite3.connect(DB_PATH)

    detections = pd.read_sql_query(
        "SELECT Date, Time, Com_Name, Confidence FROM detections_alltime WHERE Date >= '2026-07-21'", conn
    )
    weather = pd.read_sql_query(
        "SELECT timestamp, tempf, humidity, baromrelin, winddir, windspeedmph, windgustmph, solarradiation, uv, rainrate, dailyrain FROM wittboy", conn
    )
    conn.close()

    detections["datetime"] = pd.to_datetime(
        detections["Date"] + " " + detections["Time"]
    )
    detections["hour_bin"] = detections["datetime"].dt.floor("h")

    detections = detections[
        (detections["hour_bin"].dt.hour >= DAYLIGHT_START_HOUR)
        & (detections["hour_bin"].dt.hour <= DAYLIGHT_END_HOUR)
    ]

    counts = (
        detections.groupby(["hour_bin", "Com_Name"])
        .size()
        .reset_index(name="count")
    )

    all_species = detections["Com_Name"].unique()
    all_dates = pd.to_datetime(detections["hour_bin"].dt.date.unique())
    all_hours = range(DAYLIGHT_START_HOUR, DAYLIGHT_END_HOUR + 1)

    full_hour_bins = pd.to_datetime([
        pd.Timestamp(d) + pd.Timedelta(hours=h)
        for d in all_dates
        for h in all_hours
    ])

    grid = pd.MultiIndex.from_product(
        [full_hour_bins, all_species], names=["hour_bin", "Com_Name"]
    ).to_frame(index=False)

    merged_counts = grid.merge(counts, on=["hour_bin", "Com_Name"], how="left")
    merged_counts["count"] = merged_counts["count"].fillna(0).astype(int)

    weather["datetime"] = pd.to_datetime(weather["timestamp"])
    weather["hour_bin"] = weather["datetime"].dt.floor("h")
    weather_hourly = (
        weather.groupby("hour_bin")[["tempf", "humidity", "baromrelin", "winddir", "windspeedmph", "windgustmph", "solarradiation", "uv", "rainrate", "dailyrain"]]
        .mean()
        .reset_index()
    )

    merged = merged_counts.merge(weather_hourly, on="hour_bin", how="inner")
    merged["time_of_day"] = merged["hour_bin"].dt.hour

    return merged


if __name__ == "__main__":
    df = extract()
    print(df.shape)
    print("Zero-count rows:", (df["count"] == 0).sum(), "of", len(df))
    print(df.head(10))
    df.to_csv("/home/david/birdnet/mlops/scripts/extracted_data.csv", index=False)
