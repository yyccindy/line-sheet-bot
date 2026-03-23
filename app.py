import os
import json
import hmac
import hashlib
import base64
import random
import string
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
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "").strip()

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

STATE_FILLING_FORM = "filling_form"
STATE_ASK_HAS_IMAGE = "ask_has_image"
STATE_UPLOADING_IMAGES = "uploading_images"

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
    image_ws = spreadsheet.worksheet("image_log")
    return raw_ws, form_ws, image_ws


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


def get_line_image_content(message_id: str) -> bytes:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise ValueError("Missing LINE_CHANNEL_ACCESS_TOKEN")

    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }

    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.content


def upload_image_to_gcs(image_binary: bytes, filename: str) -> str:
    if not GCS_BUCKET_NAME:
        raise ValueError("Missing GCS_BUCKET_NAME")

    client = storage.Client()
    bucket = client.bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(filename)

    blob.upload_from_string(image_binary, content_type="image/jpeg")

    return f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{filename}"


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


def generate_random_suffix(length: int = 2) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def generate_case_id(form_ws) -> str:
    today_str = datetime.now(TW_TZ).strftime("%Y%m%d")

    try:
        records = form_ws.get_all_values()

        # 取 case_id 欄（第 1 欄），跳過標題列
        case_ids = [
            row[0].strip()
            for row in records[1:]
            if row and len(row) > 0 and row[0].strip()
        ]

        # 格式: CASE-20260323-001-A7
        today_case_ids = [
            cid for cid in case_ids
            if cid.startswith(f"CASE-{today_str}-")
        ]

        seq_numbers = []
        for cid in today_case_ids:
            try:
                parts = cid.split("-")
                if len(parts) >= 4:
                    seq_numbers.append(int(parts[2]))
            except Exception:
                continue

        next_seq = max(seq_numbers) + 1 if seq_numbers else 1
        suffix = generate_random_suffix(2)

        return f"CASE-{today_str}-{str(next_seq).zfill(3)}-{suffix}"

    except Exception as e:
        print("generate_case_id exception:", repr(e))
        suffix = generate_random_suffix(2)
        return f"CASE-{today_str}-001-{suffix}"


def build_raw_row(user_id: str, text: str) -> list:
    return [
        now_tw_str(),
        user_id,
        text,
    ]


def build_form_row_from_dict(
    user_id: str,
    display_name: str,
    case_id: str,
    parsed: dict,
    has_image: str
) -> list:
    return [
        case_id,
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
        has_image,
    ]


def build_image_row(
    case_id: str,
    user_id: str,
    display_name: str,
    message_id: str,
    image_url: str
) -> list:
    return [
        case_id,
        now_tw_str(),
        user_id,
        display_name,
        message_id,
        image_url,
    ]


def is_structured_case_text(text: str) -> bool:
    parsed = parse_multiline_text(text)
    matched_count = sum(1 for field in EXPECTED_FIELDS if parsed.get(field))
    return matched_count >= 3


def build_form_row_from_text(
    user_id: str,
    display_name: str,
    text: str,
    form_ws,
    has_image: str = "N"
) -> list:
    parsed = parse_multiline_text(text)
    case_id = generate_case_id(form_ws)
    return build_form_row_from_dict(user_id, display_name, case_id, parsed, has_image)


# =========================
# Conversation helpers
# =========================
def start_conversation(user_id: str, form_ws):
    user_state[user_id] = {
        "state": STATE_FILLING_FORM,
        "question_index": 0,
    }
    user_data[user_id] = {
        "case_id": generate_case_id(form_ws),
        "answers": {},
        "image_count": 0,
    }


def clear_conversation(user_id: str):
    user_state.pop(user_id, None)
    user_data.pop(user_id, None)


def has_active_conversation(user_id: str) -> bool:
    return user_id in user_state and user_id in user_data


def get_state(user_id: str) -> str:
    if user_id not in user_state:
        return ""
    return user_state[user_id].get("state", "")


def set_state(user_id: str, state: str):
    if user_id not in user_state:
        user_state[user_id] = {}
    user_state[user_id]["state"] = state


def get_question_index(user_id: str) -> int:
    if user_id not in user_state:
        return 0
    return user_state[user_id].get("question_index", 0)


def set_question_index(user_id: str, idx: int):
    if user_id not in user_state:
        user_state[user_id] = {}
    user_state[user_id]["question_index"] = idx


def get_current_question(user_id: str) -> str:
    idx = get_question_index(user_id)
    if 0 <= idx < len(QUESTION_FLOW):
        return QUESTION_FLOW[idx]
    return ""


def save_current_answer(user_id: str, answer: str):
    idx = get_question_index(user_id)
    if 0 <= idx < len(QUESTION_FLOW):
        field_name = QUESTION_FLOW[idx]
        user_data.setdefault(user_id, {})
        user_data[user_id].setdefault("answers", {})
        user_data[user_id]["answers"][field_name] = answer.strip()


def move_to_next_question(user_id: str):
    set_question_index(user_id, get_question_index(user_id) + 1)


def is_question_flow_complete(user_id: str) -> bool:
    return get_question_index(user_id) >= len(QUESTION_FLOW)


def get_case_id(user_id: str) -> str:
    return user_data.get(user_id, {}).get("case_id", "")


def get_answers(user_id: str) -> dict:
    return user_data.get(user_id, {}).get("answers", {})


def add_image_count(user_id: str):
    user_data.setdefault(user_id, {})
    current = user_data[user_id].get("image_count", 0)
    user_data[user_id]["image_count"] = current + 1


def get_image_count(user_id: str) -> int:
    return user_data.get(user_id, {}).get("image_count", 0)


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


def save_form_data(form_ws, user_id: str, has_image: str):
    display_name = get_display_name(user_id)
    case_id = get_case_id(user_id)
    answers = get_answers(user_id)

    form_row = build_form_row_from_dict(
        user_id=user_id,
        display_name=display_name,
        case_id=case_id,
        parsed=answers,
        has_image=has_image,
    )

    print("FORM_ROW:", form_row)
    form_ws.append_row(form_row, value_input_option="USER_ENTERED")


# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def home():
    return "VERSION-20260323-FINAL-FLOW-CASEID-B", 200


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

        raw_ws, form_ws, image_ws = get_worksheets()

        for event in events:
            print("===== EVENT =====")
            print(json.dumps(event, ensure_ascii=False))

            if event.get("type") != "message":
                print("Skip non-message event")
                continue

            message = event.get("message", {})
            message_type = message.get("type")

            source = event.get("source", {})
            user_id = source.get("userId", "")
            reply_token = event.get("replyToken", "")

            print("MESSAGE_TYPE:", message_type)
            print("USER_ID:", user_id)

            # =========================
            # 圖片訊息處理
            # =========================
            if message_type == "image":
                message_id = message.get("id", "")

                raw_row = build_raw_row(user_id, f"[IMAGE:{message_id}]")
                print("RAW_ROW:", raw_row)
                raw_ws.append_row(raw_row, value_input_option="USER_ENTERED")

                current_state = get_state(user_id)

                if current_state != STATE_UPLOADING_IMAGES:
                    reply_text(
                        reply_token,
                        "目前尚未進入圖片補充流程。\n"
                        "若要建立新回報，請輸入「開始回報」；\n"
                        "若要補充案件圖片，請先完成案件資料填寫。"
                    )
                    continue

                try:
                    image_binary = get_line_image_content(message_id)
                    filename = f"{get_case_id(user_id)}/{message_id}.jpg"
                    image_url = upload_image_to_gcs(image_binary, filename)
                    display_name = get_display_name(user_id)

                    image_row = build_image_row(
                        case_id=get_case_id(user_id),
                        user_id=user_id,
                        display_name=display_name,
                        message_id=message_id,
                        image_url=image_url,
                    )

                    print("IMAGE_ROW:", image_row)
                    image_ws.append_row(image_row, value_input_option="USER_ENTERED")

                    add_image_count(user_id)

                    reply_text(
                        reply_token,
                        f"已收到圖片 ✅\n目前已收到 {get_image_count(user_id)} 張。\n"
                        "若已上傳完畢，請輸入「完成」。"
                    )
                except Exception as e:
                    import traceback
                    print("Image handling exception:", repr(e))
                    traceback.print_exc()
                    reply_text(reply_token, "圖片處理失敗，請再重新上傳一次。")

                continue

            # =========================
            # 非文字 / 非圖片
            # =========================
            if message_type != "text":
                print("Skip unsupported message type:", message_type)
                continue

            text = message.get("text", "").strip()
            print("TEXT:", text)

            # 所有文字先存 raw_log
            raw_row = build_raw_row(user_id, text)
            print("RAW_ROW:", raw_row)
            raw_ws.append_row(raw_row, value_input_option="USER_ENTERED")

            # =========================
            # 指令：取消
            # =========================
            if text in ["取消", "取消回報", "結束"]:
                clear_conversation(user_id)
                reply_text(reply_token, "已取消本次回報。")
                continue

            # =========================
            # 指令：開始回報
            # =========================
            if text in ["開始回報", "我要回報", "開始填寫"]:
                start_conversation(user_id, form_ws)
                first_question = get_current_question(user_id)
                case_id = get_case_id(user_id)
                reply_text(
                    reply_token,
                    f"案件編號：{case_id}\n請輸入{first_question}"
                )
                continue

            current_state = get_state(user_id)

            # =========================
            # 狀態：填寫文字問題
            # =========================
            if current_state == STATE_FILLING_FORM:
                if not text:
                    reply_text(reply_token, "請直接輸入內容。")
                    continue

                save_current_answer(user_id, text)
                move_to_next_question(user_id)

                if is_question_flow_complete(user_id):
                    set_state(user_id, STATE_ASK_HAS_IMAGE)
                    reply_text(
                        reply_token,
                        "請問是否需要補充圖片？\n請回覆：是 / 否"
                    )
                else:
                    next_question = get_current_question(user_id)
                    reply_text(reply_token, f"請輸入{next_question}")

                continue

            # =========================
            # 狀態：詢問是否補圖
            # =========================
            if current_state == STATE_ASK_HAS_IMAGE:
                if text in ["是", "要", "有", "需要"]:
                    set_state(user_id, STATE_UPLOADING_IMAGES)
                    reply_text(
                        reply_token,
                        "請開始上傳圖片，可連續傳送多張。\n"
                        "全部上傳完畢後，請輸入「完成」。"
                    )
                    continue

                if text in ["否", "不用", "不需要", "沒有"]:
                    save_form_data(form_ws, user_id, has_image="N")
                    preview_text = format_preview(get_answers(user_id))
                    case_id = get_case_id(user_id)
                    clear_conversation(user_id)
                    reply_text(
                        reply_token,
                        f"案件編號：{case_id}\n"
                        "已收到資料 ✅\n以下為本次回報內容：\n\n"
                        f"{preview_text}\n\n"
                        "本次回報已完成。"
                    )
                    continue

                reply_text(reply_token, "請回覆「是」或「否」。")
                continue

            # =========================
            # 狀態：圖片上傳中
            # =========================
            if current_state == STATE_UPLOADING_IMAGES:
                if text == "完成":
                    save_form_data(form_ws, user_id, has_image="Y")
                    preview_text = format_preview(get_answers(user_id))
                    image_count = get_image_count(user_id)
                    case_id = get_case_id(user_id)
                    clear_conversation(user_id)

                    reply_text(
                        reply_token,
                        f"案件編號：{case_id}\n"
                        "已收到資料與圖片 ✅\n\n"
                        f"{preview_text}\n\n"
                        f"共收到 {image_count} 張圖片。\n"
                        "本次回報已完成。"
                    )
                    continue

                reply_text(
                    reply_token,
                    "目前為圖片上傳模式，請直接傳送圖片。\n"
                    "全部上傳完畢後請輸入「完成」。"
                )
                continue

            # =========================
            # 原本功能：直接貼完整格式
            # 只在沒有進行中流程時允許
            # =========================
            if is_structured_case_text(text):
                display_name = get_display_name(user_id)
                form_row = build_form_row_from_text(
                    user_id=user_id,
                    display_name=display_name,
                    text=text,
                    form_ws=form_ws,
                    has_image="N",
                )
                print("FORM_ROW_DIRECT:", form_row)
                form_ws.append_row(form_row, value_input_option="USER_ENTERED")

                reply_text(
                    reply_token,
                    f"案件編號：{form_row[0]}\n"
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
                    "正確工代:"
                )

        return "OK", 200

    except Exception as e:
        print("Webhook exception type:", type(e).__name__)
        print("Webhook exception detail:", repr(e))
        return "Internal Server Error", 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
