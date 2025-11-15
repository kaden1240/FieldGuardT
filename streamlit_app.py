import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import smtplib
from email.mime.text import MIMEText
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from datetime import datetime, timedelta
import pgeocode
import os

# -----------------------------
# CONFIGURATION
# -----------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# ✅ Credentials (local file)
if os.path.exists("service_account.json"):
    creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
else:
    raise FileNotFoundError("service_account.json not found. Place it in your project root.")

gc = gspread.authorize(creds)

FOLDER_ID = "1YdgZqHvXwpwEvEfJaAG884m-Y9AG2hSi"

# ⚠️ Email setup (replace with real credentials)
EMAIL_SENDER = "fieldguard0@gmail.com"
EMAIL_PASSWORD = "bqvf eews ojzl wppi"

# -----------------------------
# DRIVE API
# -----------------------------
drive_service = build('drive', 'v3', credentials=creds)

# -----------------------------
# NOAA WEATHER FETCH
# -----------------------------
def fetch_weather(zip_code, days_ahead=14):
    nomi = pgeocode.Nominatim('us')
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
        humidity = 80  # approximate
        precip = period.get("probabilityOfPrecipitation", {}).get("value", 0) or 0
        data.append({"date": date, "temp": temp, "humidity": humidity, "rainfall": precip})
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
        sh = gc.create(sheet_name)  # create WITHOUT folder_id

        # Move sheet into the folder
        try:
            drive_service.files().update(
                fileId=sh.id,
                addParents=FOLDER_ID,
                removeParents='root',
                fields='id, parents'
            ).execute()
        except Exception as e:
            print(f"Warning: could not move sheet to folder: {e}")

    # Always share the sheet with the user
    try:
        sh.share(email, perm_type="user", role="writer")
    except Exception as e:
        print(f"Warning: could not share sheet with {email}: {e}")

    ws = sh.sheet1
    ws.clear()
    ws.update([weather_df.columns.values.tolist()] + weather_df.values.tolist())

    # Send HIGH risk email if applicable
    high_risk = weather_df[weather_df["risk"] == "HIGH"]
    if not high_risk.empty:
        message = f"⚠️ High Late Blight risk forecast for {zip_code} on:\n"
        message += "\n".join(high_risk["date"].tolist())
        send_email(email, message)
    else:
        ws.append_rows([["Note", f"Your farm seems clear of late blight risk for the next {len(weather_df)} days."]])

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
        update_user_sheet(email, zip_code, weather_df)
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

        # -----------------------------
        # CREATE/OPEN SHEET + SHARE + MOVE
        # -----------------------------
        sheet_name = f"{email}_{zip_code}_LateBlight"
        try:
            sh = gc.open(sheet_name)
        except gspread.SpreadsheetNotFound:
            sh = gc.create(sheet_name)  # no folder_id here

            # Move to folder
            try:
                drive_service.files().update(
                    fileId=sh.id,
                    addParents=FOLDER_ID,
                    removeParents='root',
                    fields='id, parents'
                ).execute()
            except Exception as e:
                print(f"Warning: could not move sheet to folder: {e}")

        # Share with user
        try:
            sh.share(email, perm_type="user", role="writer")
        except Exception as e:
            print(f"Warning: could not share sheet with {email}: {e}")

        # Send sheet link email
        try:
            sheet_url = sh.url
            send_email(
                email,
                f"Your FieldGuard tracking sheet has been created.\n\n"
                f"Access it here:\n{sheet_url}\n\n"
                f"We’ll update it every 12 hours."
            )
        except Exception as e:
            print(f"Warning: could not send sheet link email to {email}: {e}")

        # -----------------------------
        # SCHEDULE RECURRING JOB
        # -----------------------------
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

