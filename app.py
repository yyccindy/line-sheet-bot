import os
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta

import requests
import gspread
import google.auth
from flask import Flask, request, abort

app = Flask(__name__)

# =========================
# Environment variables
# =========================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()

# 台灣時間
TW_TZ = timezone(timedelta(hours=8))

# =========================
# Google Sheets setup
# =========================
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds, _ = google.auth.default(scopes=scopes)
    return gspread.authorize(creds)


def get_worksheets():
    if not SPREADSHEET_ID:
        raise ValueError("Missing SPREADSHEET_ID")

    gc = get_gspread_client()
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    raw_ws = spreadsheet.worksheet("raw_log")
    form_ws = spreadsheet.worksheet("form_data")
    return raw_ws, form_ws


# =========================
# LINE helpers
# =========================
def verify_line_signature(body: str, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        print("Missing LINE_CHANNEL_SECRET")
        return False

    if not signature:
        print("Missing X-Line-Signature header")
        return False

    hash_bytes = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    computed_signature = base64.b64encode(hash_bytes).decode("utf-8")
    return hmac.compare_digest(computed_signature, signature)


def get_display_name(user_id: str) -> str:
    if not user_id or not LINE_CHANNEL_ACCESS_TOKEN:
        return ""

    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data.get("displayName", "")

        print("get_display_name failed:", r.status_code, r.text)
        return ""
    except Exception as e:
        print("get_display_name exception:", repr(e))
        return ""


def reply_text(reply_token: str, text: str):
    if not reply_token or not LINE_CHANNEL_ACCESS_TOKEN:
        return

    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": text,
            }
        ],
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code != 200:
            print("reply_text failed:", r.status_code, r.text)
    except Exception as e:
        print("reply_text exception:", repr(e))


# =========================
# Parsing helpers
# =========================
EXPECTED_FIELDS = [
    "服務廠",
    "專員",
    "車號",
    "錯誤件號",
    "錯誤工代",
    "正確件號",
    "正確工代",
]


def parse_multiline_text(text: str) -> dict:
    data = {}

    if not text:
        return data

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if ":" in line:
            key, value = line.split(":", 1)
        elif "：" in line:
            key, value = line.split("：", 1)
        else:
            continue

        key = key.strip()
        value = value.strip()

        if key in EXPECTED_FIELDS:
            data[key] = value

    return data


def now_tw_str() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def build_raw_row(user_id: str, text: str) -> list:
    return [
        now_tw_str(),
        user_id,
        text,
    ]


def build_form_row(user_id: str, display_name: str, text: str) -> list:
    parsed = parse_multiline_text(text)

    return [
        now_tw_str(),
        user_id,
        display_name,
        parsed.get("服務廠", ""),
        parsed.get("專員", ""),
        parsed.get("車號", ""),
        parsed.get("錯誤件號", ""),
        parsed.get("錯誤工代", ""),
        parsed.get("正確件號", ""),
        parsed.get("正確工代", ""),
    ]


def is_structured_case_text(text: str) -> bool:
    parsed = parse_multiline_text(text)
    matched_count = sum(1 for field in EXPECTED_FIELDS if parsed.get(field))
    return matched_count >= 3


# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def home():
    return "VERSION-20260309-NEW", 200


@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_data(as_text=True)
    signature = request.headers.get("X-Line-Signature", "")

    print("===== RAW BODY =====")
    print(body)

    if not verify_line_signature(body, signature):
        print("Invalid LINE signature")
        abort(400)

    try:
        payload = json.loads(body)
        events = payload.get("events", [])

        if not events:
            print("No events in payload, return 200 for verify")
            return "OK", 200

        raw_ws, form_ws = get_worksheets()

        for event in events:
            print("===== EVENT =====")
            print(json.dumps(event, ensure_ascii=False))

            if event.get("type") != "message":
                print("Skip non-message event")
                continue

            message = event.get("message", {})
            if message.get("type") != "text":
                print("Skip non-text message")
                continue

            source = event.get("source", {})
            user_id = source.get("userId", "")
            reply_token = event.get("replyToken", "")
            text = message.get("text", "").strip()

            print("TEXT:", text)
            print("USER_ID:", user_id)

            raw_row = build_raw_row(user_id, text)
            print("RAW_ROW:", raw_row)
            raw_ws.append_row(raw_row, value_input_option="USER_ENTERED")

            if is_structured_case_text(text):
                display_name = get_display_name(user_id)
                form_row = build_form_row(user_id, display_name, text)
                print("FORM_ROW:", form_row)
                form_ws.append_row(form_row, value_input_option="USER_ENTERED")

                reply_text(
                    reply_token,
                    "已收到資料 ✅\n若需補充內容，請直接再傳一次完整格式。"
                )
            else:
                reply_text(
                    reply_token,
                    "已收到訊息。\n請依照以下格式傳送：\n"
                    "服務廠: 台北\n"
                    "專員: Jason\n"
                    "車號: ABC123\n"
                    "錯誤件號: 111\n"
                    "錯誤工代: 222\n"
                    "正確件號: 333\n"
                    "正確工代: 444"
                )

        return "OK", 200

    except Exception as e:
        print("Webhook exception type:", type(e).__name__)
        print("Webhook exception detail:", repr(e))
        return "Internal Server Error", 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
