from flask import Flask, request
import threading
import json
import os
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_info(
    SERVICE_ACCOUNT_INFO, scopes=SCOPES
)

gc = gspread.authorize(creds)
SHEET_ID = os.environ["SHEET_ID"]
sheet = gc.open_by_key(SHEET_ID).sheet1


def save_to_sheet(data):
    try:
        events = data.get("events", [])

        for event in events:
            if event.get("type") != "message":
                continue
            if event.get("message", {}).get("type") != "text":
                continue

            text = event["message"]["text"]
            user_id = event.get("source", {}).get("userId", "")

            sheet.append_row([user_id, text])
    except Exception as e:
        print("save_to_sheet error:", e)


@app.route("/", methods=["GET"])
def home():
    return "ok", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True) or {}

    # 背景執行，不讓 LINE 等
    threading.Thread(target=save_to_sheet, args=(data,)).start()

    # 立刻回 LINE
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
