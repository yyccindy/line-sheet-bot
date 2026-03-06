import os
import json
from flask import Flask, request, abort
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# 讀取 Google Service Account JSON
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_info(
    SERVICE_ACCOUNT_INFO, scopes=SCOPES
)

gc = gspread.authorize(creds)

# Google Sheet ID
SHEET_ID = os.environ["SHEET_ID"]

sheet = gc.open_by_key(SHEET_ID).sheet1


@app.route("/", methods=["GET"])
def home():
    return "ok"


@app.route("/webhook", methods=["POST"])
def webhook():

    body = request.json

    events = body.get("events", [])

    for event in events:

        if event["type"] != "message":
            continue

        if event["message"]["type"] != "text":
            continue

        text = event["message"]["text"]
        user_id = event["source"].get("userId", "")

        sheet.append_row([
            user_id,
            text
        ])

    return "OK"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
