import streamlit as st
import pandas as pd
import requests
import pgeocode
import smtplib
from email.mime.text import MIMEText
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from supabase import create_client
import os



# -----------------------------
# CONFIGURATION
# -----------------------------
from supabase import create_client

SUPABASE_URL = "https://vkbmhedzzguegjyqpljy.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZrYm1oZWR6emd1ZWdqeXFwbGp5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjMyMzUyOTYsImV4cCI6MjA3ODgxMTI5Nn0.pCZYXEpbV8oQFExQeKbSsjSp-t5B9vQLTwO12EI1sy0"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ⚠️ Email setup (replace with real credentials)
EMAIL_SENDER = "fieldguard0@gmail.com"
EMAIL_PASSWORD = "bqvf eews ojzl wppi"

# -----------------------------
# NOAA WEATHER FETCH
# -----------------------------
def fetch_weather(zip_code, days_ahead=14):
    """
    Fetch hourly weather data from Open‑Meteo and aggregate daily.
    Returns a DataFrame with: date, avg_temp (°F), max_temp, min_temp,
    avg_rh (%), avg_dewpoint (°F), total_rain (inches).
    """
    # Geocode ZIP → lat/lon
    nomi = pgeocode.Nominatim("us")
    location = nomi.query_postal_code(zip_code)
    lat, lon = location.latitude, location.longitude
    if pd.isna(lat) or pd.isna(lon):
        return pd.DataFrame()

    # Open-Meteo API: hourly variables
    url = (
        "https://api.open-meteo.com/v1/gfs"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,relative_humidity_2m,dew_point_2m,precipitation"
        f"&forecast_days={days_ahead}"
        "&temperature_unit=fahrenheit"
        "&precipitation_unit=inch"
        "&timezone=auto"
    )
    resp = requests.get(url)
    resp.raise_for_status()
    js = resp.json()

    hourly = js["hourly"]
    times = hourly["time"]
    temps = hourly["temperature_2m"]
    rhs = hourly["relative_humidity_2m"]
    dewpts = hourly["dew_point_2m"]
    precips = hourly["precipitation"]

    # Build a DataFrame for hourly data
    df_hour = pd.DataFrame({
        "time": pd.to_datetime(times),
        "temp": temps,
        "rh": rhs,
        "dewpoint": dewpts,
        "precip": precips,
    })

    # Aggregate to daily
    df_hour["date"] = df_hour["time"].dt.date
    daily = df_hour.groupby("date").agg({
        "temp": ["mean", "min", "max"],
        "rh": "mean",
        "dewpoint": "mean",
        "precip": "sum",
    }).reset_index()

    # Flatten multi-index
    daily.columns = [
        "date",
        "avg_temp", "min_temp", "max_temp",
        "avg_rh", "avg_dewpoint",
        "total_rain"
    ]

    return daily

# -----------------------------
# LATE BLIGHT RISK CALCULATION
# -----------------------------
def calculate_late_blight_risk(weather_df):
    """
    Calculate late blight risk using:
      - Temperature (daily avg / min / max)
      - Average relative humidity
      - Average dew point (as a proxy for leaf wetness)
      - Daily total precipitation
    Returns a DataFrame with a 'risk' column: LOW / MEDIUM / HIGH.
    """
    def risk_row(row):
        # Temperature criteria
        avg_t = row["avg_temp"]
        max_t = row["max_temp"]
        min_t = row["min_temp"]

        # Humidity & dewpoint
        rh = row["avg_rh"]
        dp = row["avg_dewpoint"]

        # Rain
        rain = row["total_rain"]

        # Conditions:
        # High risk: favorable temp, high humidity or dewpoint close to temp, and rain
        temp_ok = (min_t >= 55 and max_t <= 85)
        very_humid = (rh >= 90)
        dew_wet = False
        if pd.notna(dp):
            dew_wet = abs(dp - avg_t) <= 2  # dew point very close → likely dew

        rain_enough = rain >= 0.1  # at least 0.1 inch rain

        if temp_ok and (very_humid or dew_wet) and rain_enough:
            return "HIGH"

        # Medium risk
        med_temp = (min_t >= 50 and max_t <= 90)
        med_humid = (rh >= 85)
        med_rain = rain > 0

        if med_temp and (med_humid or dew_wet) and med_rain:
            return "MEDIUM"

        return "LOW"

    weather_df["risk"] = weather_df.apply(risk_row, axis=1)
    return weather_df

# -----------------------------
# EMAIL ALERTS
# -----------------------------
def send_email(to_email, message):
    msg = MIMEText(message)
    msg["Subject"] = "FieldGuard Late Blight Alert"
    msg["From"] = EMAIL_SENDER
    msg["To"] = to_email
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"Email failed: {e}")

# -----------------------------
# SUPABASE UPDATE
# -----------------------------
def update_user_forecast(email, zip_code, weather_df):
    # Clear previous forecasts for this email
    supabase.table("forecasts").delete().eq("email", email).execute()

    # Convert all rows into clean supabase-friendly records
    records = []
    for _, row in weather_df.iterrows():
        records.append({
            "email": email,
            "zip_code": zip_code,
            "date": str(row["date"]),             # Guarantees date format
            "temp": float(row["temp"]),           # numeric
            "humidity": float(row["humidity"]),   # numeric
            "rainfall": float(row["rainfall"]),   # numeric
            "risk": row["risk"]                   # text
        })

    # Insert all records in one batch
    res = supabase.table("forecasts").insert(records).execute()
    print("Supabase insert response:", res)  # logs success or error

    # Email alerts for high-risk days
    high_risk = weather_df[weather_df["risk"] == "HIGH"]
    if not high_risk.empty:
        message = f"⚠️ High Late Blight risk forecast for {zip_code} on:\n"
        message += "\n".join(high_risk["date"].tolist())
        send_email(email, message)

# -----------------------------
# SCHEDULER
# -----------------------------
scheduler = BackgroundScheduler()
scheduler.start()

def scheduled_job(email, zip_code):
    try:
        weather_df = fetch_weather(zip_code, days_ahead=14)
        if weather_df.empty:
            return
        weather_df = calculate_late_blight_risk(weather_df)
        update_user_forecast(email, zip_code, weather_df)
    except Exception as e:
        print(f"Error for {zip_code}: {e}")

# -----------------------------
# STREAMLIT APP
# -----------------------------
st.title("FieldGuard: Tomato Disease Predictor")
st.write("Enter your farm's ZIP code to receive late blight risk forecasts.")

email = st.text_input("Enter your email")
zip_code = st.text_input("Enter your ZIP code")

if st.button("Submit"):
    if not email or not zip_code:
        st.error("Please enter both ZIP code and email.")
    else:
        st.success(f"Thanks! We’ll monitor late blight risk for ZIP code {zip_code}.")

        # Schedule recurring job
        job_id = f"{email}_{zip_code}"
        if not scheduler.get_job(job_id):
            scheduler.add_job(
                scheduled_job,
                "interval",
                hours=12,
                args=[email, zip_code],
                id=job_id
            )

        # Run immediately once
        scheduled_job(email, zip_code)




#git add .
#git commit -m "Fix Streamlit app: dynamic sheets, scheduler, email alerts, no secrets"
#git push origin main

