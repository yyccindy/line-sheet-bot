from flask import Flask, request
import json
import os
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
CREDS = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
GC = gspread.authorize(CREDS)

SHEET_ID = os.environ["SHEET_ID"]
SPREADSHEET = GC.open_by_key(SHEET_ID)

RAW = SPREADSHEET.worksheet("raw_log")
REPORT = SPREADSHEET.worksheet("parsed_report")

@app.route("/", methods=["GET"])
def home():
    return "ok"

@app.route("/callback", methods=["POST"])
def callback():

    data = request.json

    for ev in data["events"]:

        if ev["type"] != "message":
            continue

        if ev["message"]["type"] != "text":
            continue

        text = ev["message"]["text"]

        RAW.append_row(["", "", "", "", "", "", "", "", text])

    return "ok"

if __name__ == "__main__":

    app.run()
