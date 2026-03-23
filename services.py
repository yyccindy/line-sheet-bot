import base64
import hashlib
import hmac
import random
import string
from datetime import datetime

import google.auth
import gspread
import requests
from google.cloud import storage

from config import (
    GCS_BUCKET_NAME,
    LINE_CHANNEL_ACCESS_TOKEN,
    LINE_CHANNEL_SECRET,
    SPREADSHEET_ID,
    TW_TZ,
)


# =========================
# Google Sheets
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
    return (
        spreadsheet.worksheet("raw_log"),
        spreadsheet.worksheet("form_data"),
        spreadsheet.worksheet("image_log"),
    )


# =========================
# LINE helpers
# =========================
def verify_line_signature(body: str, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, signature)


def line_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }


def get_display_name(user_id: str) -> str:
    if not user_id or not LINE_CHANNEL_ACCESS_TOKEN:
        return ""

    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/profile/{user_id}",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("displayName", "")
        print("get_display_name failed:", r.status_code, r.text)
    except Exception as e:
        print("get_display_name exception:", repr(e))
    return ""


def reply_texts(reply_token: str, texts: list[str]):
    if not reply_token or not LINE_CHANNEL_ACCESS_TOKEN:
        return

    messages = [{"type": "text", "text": t} for t in texts if t and t.strip()]
    if not messages:
        return

    payload = {"replyToken": reply_token, "messages": messages[:5]}
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=line_headers(),
            json=payload,
            timeout=10,
        )
        if r.status_code != 200:
            print("reply_texts failed:", r.status_code, r.text)
    except Exception as e:
        print("reply_texts exception:", repr(e))


def reply_text(reply_token: str, text: str):
    reply_texts(reply_token, [text])


def get_line_image_content(message_id: str) -> bytes:
    r = requests.get(
        f"https://api-data.line.me/v2/bot/message/{message_id}/content",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.content


# =========================
# GCS
# =========================
def upload_image_to_gcs(image_binary: bytes, filename: str) -> str:
    if not GCS_BUCKET_NAME:
        raise ValueError("Missing GCS_BUCKET_NAME")

    bucket = storage.Client().bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(filename)
    blob.upload_from_string(image_binary, content_type="image/jpeg")
    return f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{filename}"


# =========================
# Common helpers
# =========================
def now_tw_str() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def generate_random_suffix(length: int = 2) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def generate_case_id(form_ws) -> str:
    today_str = datetime.now(TW_TZ).strftime("%Y%m%d")
    try:
        records = form_ws.get_all_values()[1:]
        seqs = []

        for row in records:
            if not row or not row[0].strip():
                continue
            cid = row[0].strip()
            if not cid.startswith(f"CASE-{today_str}-"):
                continue
            parts = cid.split("-")
            if len(parts) >= 4 and parts[2].isdigit():
                seqs.append(int(parts[2]))

        next_seq = max(seqs) + 1 if seqs else 1
        return f"CASE-{today_str}-{str(next_seq).zfill(3)}-{generate_random_suffix(2)}"
    except Exception as e:
        print("generate_case_id exception:", repr(e))
        return f"CASE-{today_str}-001-{generate_random_suffix(2)}"


def build_raw_row(user_id: str, text: str) -> list:
    return [now_tw_str(), user_id, text]


def build_form_row(user_id: str, display_name: str, case_id: str, data: dict, has_image: str) -> list:
    return [
        case_id,
        now_tw_str(),
        user_id,
        display_name,
        data.get("服務廠", ""),
        data.get("專員", ""),
        data.get("車號", ""),
        data.get("錯誤件號", ""),
        data.get("錯誤工代", ""),
        data.get("正確件號", ""),
        data.get("正確工代", ""),
        has_image,
    ]


def build_image_row(case_id: str, user_id: str, display_name: str, message_id: str, image_url: str) -> list:
    return [case_id, now_tw_str(), user_id, display_name, message_id, image_url]


def safe_append_row(ws, row):
    try:
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print("safe_append_row exception:", repr(e))
