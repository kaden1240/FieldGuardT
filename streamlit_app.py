import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import smtplib
from email.mime.text import MIMEText
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from datetime import datetime, timedelta
import pgeocode
import json
import os
# -----------------------------
# CONFIGURATION
# -----------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# ✅ Dual-mode credentials: local file or cloud file
if os.path.exists("service_account.json"):
    # Local or cloud deployment if the JSON is present
    creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
else:
    raise FileNotFoundError("service_account.json not found. Place it in your project root.")

# Authorize Google Sheets
gc = gspread.authorize(creds)
sheet = gc.open("FieldGuard_Users").sheet1

# ⚠️ Replace these with your real email + app password later
EMAIL_SENDER = "your_email@gmail.com"
EMAIL_PASSWORD = "your_email_password"



# -----------------------------
# NOAA WEATHER FETCH
# -----------------------------
def fetch_weather(zip_code, days_ahead=14):
    """
    Fetch NOAA/NWS forecast for given ZIP code.
    Returns dataframe with temp (F), humidity (%), precipitation (%)
    """
    # Convert ZIP -> lat/lon
    nomi = pgeocode.Nominatim('us')
    location = nomi.query_postal_code(zip_code)
    lat, lon = location.latitude, location.longitude
    if pd.isna(lat) or pd.isna(lon):
        return pd.DataFrame()  # Invalid ZIP

    # NWS API: get forecast points
    points_url = f"https://api.weather.gov/points/{lat},{lon}"
    r = requests.get(points_url)
    r.raise_for_status()
    forecast_url = r.json()["properties"]["forecast"]
    forecast_data = requests.get(forecast_url).json()["properties"]["periods"]

    # Prepare dataframe (limit to next `days_ahead` days)
    data = []
    added_dates = set()
    for period in forecast_data:
        date = period["startTime"][:10]
        if date in added_dates or len(data) >= days_ahead:
            continue
        added_dates.add(date)

        temp = period["temperature"]
        temp_unit = period["temperatureUnit"]

        # NWS does NOT provide humidity → approximate
        humidity = 80  
        precip = period.get("probabilityOfPrecipitation", {}).get("value", 0) or 0

        data.append({
            "date": date,
            "temp": temp,
            "humidity": humidity,
            "rainfall": precip
        })

    return pd.DataFrame(data)

# -----------------------------
# LATE BLIGHT RISK
# -----------------------------
def calculate_late_blight_risk(weather_df):
    def risk_row(row):
        if 60 <= row["temp"] <= 80 and row["humidity"] > 80 and row["rainfall"] > 0.1:
            return "HIGH"
        else:
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
# GOOGLE SHEET UPDATE
# -----------------------------
def update_user_sheet(email, zip_code, weather_df):
    sheet_name = f"{email}_{zip_code}_LateBlight"
    try:
        sh = gc.open(sheet_name)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(sheet_name)
        sh.share(email, perm_type="user", role="writer")
    ws = sh.sheet1
    ws.clear()

    # Update weather + risk table
    ws.update([weather_df.columns.values.tolist()] + weather_df.values.tolist())

    # Email alert for high risk
    high_risk = weather_df[weather_df["risk"] == "HIGH"]
    if not high_risk.empty:
        message = f"⚠️ High Late Blight risk forecast for {zip_code} on:\n"
        message += "\n".join(high_risk["date"].tolist())
        send_email(email, message)
    else:
        clear_message = [["Note", f"Your farm seems clear of late blight risk for the next {len(weather_df)} days."]]
        ws.append_rows(clear_message)

# -----------------------------
# SCHEDULER
# -----------------------------
scheduler = BackgroundScheduler()

def scheduled_job(email, zip_code):
    try:
        weather_df = fetch_weather(zip_code, days_ahead=14)
        if weather_df.empty:
            return
        weather_df = calculate_late_blight_risk(weather_df)
        update_user_sheet(email, zip_code, weather_df)
    except Exception as e:
        print(f"Error for {zip_code}: {e}")

scheduler.start()

# -----------------------------
# STREAMLIT APP
# -----------------------------
st.title("FieldGuard: Tomato Disease Predictor")
st.write("Enter your farm's ZIP code to receive late blight risk forecasts.")

zip_code = st.text_input("Enter your ZIP code")
email = st.text_input("Enter your email")

if st.button("Submit"):
    if not zip_code or not email:
        st.error("Please enter both ZIP code and email.")
    else:
        st.success(f"Thanks! We’ll monitor late blight risk for ZIP code {zip_code}.")

        # Schedule twice daily checks (every 12 hours)
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