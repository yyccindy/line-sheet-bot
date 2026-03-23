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
from google.cloud import storage

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
# 對話狀態暫存（簡易版）
# ⚠ Cloud Run 重啟後可能會消失
# =========================
user_state = {}
user_data = {}

QUESTION_FLOW = [
    "服務廠",
    "專員",
    "車號",
    "錯誤件號",
    "錯誤工代",
    "正確件號",
    "正確工代",
]

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


def build_form_row_from_dict(user_id: str, display_name: str, parsed: dict) -> list:
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
# Conversation helpers
# =========================
def start_conversation(user_id: str):
    user_state[user_id] = 0
    user_data[user_id] = {}


def clear_conversation(user_id: str):
    user_state.pop(user_id, None)
    user_data.pop(user_id, None)


def is_in_conversation(user_id: str) -> bool:
    return user_id in user_state


def get_current_question(user_id: str) -> str:
    idx = user_state.get(user_id, 0)
    if 0 <= idx < len(QUESTION_FLOW):
        return QUESTION_FLOW[idx]
    return ""


def save_current_answer(user_id: str, answer: str):
    idx = user_state.get(user_id, 0)
    if 0 <= idx < len(QUESTION_FLOW):
        field_name = QUESTION_FLOW[idx]
        user_data.setdefault(user_id, {})
        user_data[user_id][field_name] = answer.strip()


def move_to_next_question(user_id: str):
    user_state[user_id] = user_state.get(user_id, 0) + 1


def is_conversation_complete(user_id: str) -> bool:
    return user_state.get(user_id, 0) >= len(QUESTION_FLOW)


def format_preview(data: dict) -> str:
    return (
        f"服務廠：{data.get('服務廠', '')}\n"
        f"專員：{data.get('專員', '')}\n"
        f"車號：{data.get('車號', '')}\n"
        f"錯誤件號：{data.get('錯誤件號', '')}\n"
        f"錯誤工代：{data.get('錯誤工代', '')}\n"
        f"正確件號：{data.get('正確件號', '')}\n"
        f"正確工代：{data.get('正確工代', '')}"
    )


# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def home():
    return "VERSION-20260320-QA-FLOW", 200


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

            # 所有文字都先存 raw_log
            raw_row = build_raw_row(user_id, text)
            print("RAW_ROW:", raw_row)
            raw_ws.append_row(raw_row, value_input_option="USER_ENTERED")

            # =========================
            # 指令：取消回報
            # =========================
            if text in ["取消", "取消回報", "結束"]:
                clear_conversation(user_id)
                reply_text(reply_token, "已取消本次回報。")
                continue

            # =========================
            # 指令：開始回報（一題一題問）
            # =========================
            if text in ["開始回報", "我要回報", "開始填寫"]:
                start_conversation(user_id)
                first_question = get_current_question(user_id)
                reply_text(reply_token, f"請輸入{first_question}")
                continue

            # =========================
            # 若使用者正在對話流程中
            # =========================
            if is_in_conversation(user_id):
                if not text:
                    reply_text(reply_token, "請直接輸入內容。")
                    continue

                save_current_answer(user_id, text)
                move_to_next_question(user_id)

                if is_conversation_complete(user_id):
                    collected_data = user_data.get(user_id, {})
                    display_name = get_display_name(user_id)
                    form_row = build_form_row_from_dict(user_id, display_name, collected_data)

                    print("FORM_ROW_FROM_QA:", form_row)
                    form_ws.append_row(form_row, value_input_option="USER_ENTERED")

                    preview_text = format_preview(collected_data)
                    clear_conversation(user_id)

                    reply_text(
                        reply_token,
                        "已收到資料 ✅\n以下為本次回報內容：\n\n"
                        f"{preview_text}\n\n"
                        "若需補充內容，請重新輸入「開始回報」。"
                    )
                else:
                    next_question = get_current_question(user_id)
                    reply_text(reply_token, f"請輸入{next_question}")

                continue

            # =========================
            # 原本功能：直接貼完整格式
            # =========================
            if is_structured_case_text(text):
                display_name = get_display_name(user_id)
                form_row = build_form_row(user_id, display_name, text)
                print("FORM_ROW:", form_row)
                form_ws.append_row(form_row, value_input_option="USER_ENTERED")

                reply_text(
                    reply_token,
                    "已收到資料 ✅\n若需補充內容，請直接再傳一次完整格式，或輸入「開始回報」逐題填寫。"
                )
            else:
                reply_text(
                    reply_token,
                    "已收到訊息。\n\n"
                    "若要逐題填寫，請輸入：開始回報\n\n"
                    "若要一次貼上，請依照以下格式傳送：\n"
                    "服務廠:\n"
                    "專員:\n"
                    "車號:\n"
                    "錯誤件號:\n"
                    "錯誤工代:\n"
                    "正確件號:\n"
                    "正確工代: "
                )

        return "OK", 200

    except Exception as e:
        print("Webhook exception type:", type(e).__name__)
        print("Webhook exception detail:", repr(e))
        return "Internal Server Error", 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
