import streamlit as st
import pandas as pd
import requests
import pgeocode
import smtplib
from email.mime.text import MIMEText
from apscheduler.schedulers.background import BackgroundScheduler
from supabase import create_client

# -----------------------------
# CONFIGURATION
# -----------------------------
SUPABASE_URL = "https://vkbmhedzzguegjyqpljy.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZrYm1oZWR6emd1ZWdqeXFwbGp5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjMyMzUyOTYsImV4cCI6MjA3ODgxMTI5Nn0.pCZYXEpbV8oQFExQeKbSsjSp-t5B9vQLTwO12EI1sy0"
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

EMAIL_SENDER = "fieldguard0@gmail.com"
EMAIL_PASSWORD = "bqvf eews ojzl wppi"

# -----------------------------
# NOAA WEATHER FETCH
# -----------------------------
def fetch_weather(zip_code, days_ahead=14):
    nomi = pgeocode.Nominatim("us")
    location = nomi.query_postal_code(zip_code)
    lat, lon = location.latitude, location.longitude
    if pd.isna(lat) or pd.isna(lon):
        return pd.DataFrame()

    points_url = f"https://api.weather.gov/points/{lat},{lon}"
    r = requests.get(points_url)
    r.raise_for_status()
    forecast_url = r.json()["properties"]["forecast"]
    forecast_data = requests.get(forecast_url).json()["properties"]["periods"]

    data, added_dates = [], set()
    for period in forecast_data:
        date = period["startTime"][:10]
        if date in added_dates or len(data) >= days_ahead:
            continue
        added_dates.add(date)
        temp = period["temperature"]
        humidity = 80
        precip = period.get("probabilityOfPrecipitation", {}).get("value", 0) or 0
        data.append({"date": date, "temp": temp, "humidity": humidity, "rainfall": precip})
    return pd.DataFrame(data)

# -----------------------------
# LATE BLIGHT RISK
# -----------------------------
def calculate_late_blight_risk(weather_df):
    def risk_row(row):
        return "HIGH" if 60 <= row["temp"] <= 80 and row["humidity"] > 80 and row["rainfall"] > 0.1 else "LOW"
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
    # Upsert user
    resp1 = supabase.table("users").upsert({"email": email, "zip_code": zip_code}).execute()
    print("UPSERT USER:", resp1)

    # Delete old forecasts
    resp2 = supabase.table("forecasts").delete().eq("email", email).execute()
    print("DELETE FORECAST:", resp2)

    # Insert forecast rows
    records = [
        {
            "email": email,
            "zip_code": zip_code,
            "date": row["date"],
            "temp": row["temp"],
            "humidity": row["humidity"],
            "rainfall": row["rainfall"],
            "risk": row["risk"]
        }
        for _, row in weather_df.iterrows()
    ]
    resp3 = supabase.table("forecasts").insert(records).execute()
    print("INSERT FORECAST:", resp3)

    # Send HIGH risk email if applicable
    high_risk = weather_df[weather_df["risk"] == "HIGH"]
    if not high_risk.empty:
        message = f"⚠️ High Late Blight risk forecast for {zip_code} on:\n" + "\n".join(high_risk["date"].tolist())
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

