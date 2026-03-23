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
    {
        "field": "服務廠",
        "prompt": "1/7 請輸入【服務廠】\n例如：南港廠"
    },
    {
        "field": "專員",
        "prompt": "2/7 請輸入【專員姓名】\n例如：王小明"
    },
    {
        "field": "車號",
        "prompt": "3/7 請輸入【車號】\n例如：ABC-1234"
    },
    {
        "field": "錯誤件號",
        "prompt": "4/7 請輸入【錯誤件號】\n若有多個，請用逗號分隔\n例如：52119-0K902, 53301-0K420"
    },
    {
        "field": "錯誤工代",
        "prompt": "5/7 請輸入【錯誤工代】\n若有多個，請用逗號分隔\n例如：A12, B34"
    },
    {
        "field": "正確件號",
        "prompt": "6/7 請輸入【正確件號】\n若有多個，請用逗號分隔\n例如：52119-0K903, 53301-0K421"
    },
    {
        "field": "正確工代",
        "prompt": "7/7 請輸入【正確工代】\n若有多個，請用逗號分隔\n例如：A13, B35"
    },
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


def reply_texts(reply_token: str, texts: list[str]):
    if not reply_token or not LINE_CHANNEL_ACCESS_TOKEN:
        return

    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }

    messages = []
    for text in texts:
        if text and text.strip():
            messages.append({
                "type": "text",
                "text": text
            })

    if not messages:
        return

    payload = {
        "replyToken": reply_token,
        "messages": messages[:5]
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code != 200:
            print("reply_texts failed:", r.status_code, r.text)
    except Exception as e:
        print("reply_texts exception:", repr(e))


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
# Helpers
# =========================
def now_tw_str() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def generate_random_suffix(length: int = 2) -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def generate_case_id(form_ws) -> str:
    today_str = datetime.now(TW_TZ).strftime("%Y%m%d")

    try:
        records = form_ws.get_all_values()

        case_ids = [
            row[0].strip()
            for row in records[1:]
            if row and len(row) > 0 and row[0].strip()
        ]

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


def get_current_question_prompt(user_id: str) -> str:
    idx = get_question_index(user_id)
    if 0 <= idx < len(QUESTION_FLOW):
        return QUESTION_FLOW[idx]["prompt"]
    return ""


def save_current_answer(user_id: str, answer: str):
    idx = get_question_index(user_id)
    if 0 <= idx < len(QUESTION_FLOW):
        field_name = QUESTION_FLOW[idx]["field"]
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


def go_to_previous_question(user_id: str) -> bool:
    idx = get_question_index(user_id)

    if idx <= 0:
        return False

    new_idx = idx - 1
    set_question_index(user_id, new_idx)

    field_name = QUESTION_FLOW[new_idx]["field"]
    user_data.setdefault(user_id, {})
    user_data[user_id].setdefault("answers", {})
    user_data[user_id]["answers"].pop(field_name, None)

    return True


def reset_answers(user_id: str):
    case_id = get_case_id(user_id)

    user_state[user_id] = {
        "state": STATE_FILLING_FORM,
        "question_index": 0,
    }

    user_data[user_id] = {
        "case_id": case_id,
        "answers": {},
        "image_count": 0,
    }


def build_partial_preview(user_id: str) -> str:
    answers = get_answers(user_id)

    lines = []
    for q in QUESTION_FLOW:
        field = q["field"]
        value = answers.get(field, "（未填）")
        lines.append(f"{field}：{value}")

    return "\n".join(lines)


# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def home():
    return "VERSION-20260323-FINAL-FULL", 200


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
                        "請先輸入「開始回報」，完成案件資料填寫後再上傳圖片。"
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
                        f"已收到第 {get_image_count(user_id)} 張圖片 ✅\n"
                        "若還有圖片請繼續上傳；\n"
                        "全部完成後請輸入：完成"
                    )
                except Exception as e:
                    import traceback
                    print("Image handling exception:", repr(e))
                    traceback.print_exc()
                    reply_text(reply_token, "圖片處理失敗，請再重新上傳一次。")

                continue

            # =========================
            # 非文字 / 非圖片（例如貼圖、影片、檔案）
            # =========================
            if message_type != "text":
                print("Skip unsupported message type:", message_type)

                current_state = get_state(user_id)

                if current_state == STATE_FILLING_FORM:
                    reply_text(
                        reply_token,
                        "目前正在填寫案件資料，請直接輸入文字內容。\n\n"
                        "可用指令：上一題｜重填｜查看｜取消"
                    )
                elif current_state == STATE_ASK_HAS_IMAGE:
                    reply_text(
                        reply_token,
                        "請回覆「是」或「否」，確認是否需要補充圖片。"
                    )
                elif current_state == STATE_UPLOADING_IMAGES:
                    reply_text(
                        reply_token,
                        "目前為圖片上傳模式，請直接傳送圖片。\n"
                        "全部完成後請輸入：完成"
                    )
                else:
                    reply_text(
                        reply_token,
                        "若要建立案件回報，請輸入：開始回報"
                    )

                continue

            # =========================
            # 文字訊息處理
            # =========================
            text = message.get("text", "").strip()
            print("TEXT:", text)

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
                first_prompt = get_current_question_prompt(user_id)
                case_id = get_case_id(user_id)

                reply_texts(
                    reply_token,
                    [
                        "已開始案件回報 ✅",
                        f"案件編號：{case_id}\n\n"
                        "接下來我會依序詢問 7 項資料，\n"
                        "請直接回覆內容即可。\n\n"
                        "可用指令：\n"
                        "上一題｜重填｜查看｜取消",
                        first_prompt
                    ]
                )
                continue

            current_state = get_state(user_id)

            # =========================
            # 狀態：填寫文字問題
            # =========================
            if current_state == STATE_FILLING_FORM:
                # 指令：上一題
                if text == "上一題":
                    success = go_to_previous_question(user_id)

                    if not success:
                        reply_text(reply_token, "已經是第一題了，無法再返回。")
                        continue

                    prompt = get_current_question_prompt(user_id)
                    reply_text(reply_token, f"已返回上一題 👆\n\n{prompt}")
                    continue

                # 指令：重填
                if text in ["重填", "重新開始"]:
                    reset_answers(user_id)

                    prompt = get_current_question_prompt(user_id)
                    reply_text(
                        reply_token,
                        "已清空資料，重新開始填寫 ✨\n\n" + prompt
                    )
                    continue

                # 指令：查看
                if text == "查看":
                    preview = build_partial_preview(user_id)
                    reply_text(
                        reply_token,
                        "目前填寫內容如下：\n\n" + preview
                    )
                    continue

                if not text:
                    reply_text(reply_token, "此欄位尚未填寫，請直接輸入內容。")
                    continue

                save_current_answer(user_id, text)
                move_to_next_question(user_id)

                if is_question_flow_complete(user_id):
                    set_state(user_id, STATE_ASK_HAS_IMAGE)
                    reply_text(
                        reply_token,
                        "文字資料已填寫完成 ✅\n\n"
                        "請問是否需要補充圖片？\n"
                        "請回覆：是 / 否"
                    )
                else:
                    next_prompt = get_current_question_prompt(user_id)
                    reply_text(reply_token, next_prompt)

                continue

            # =========================
            # 狀態：詢問是否補圖
            # =========================
            if current_state == STATE_ASK_HAS_IMAGE:
                if text == "查看":
                    preview = build_partial_preview(user_id)
                    reply_text(
                        reply_token,
                        "目前填寫內容如下：\n\n" + preview + "\n\n請回覆：是 / 否"
                    )
                    continue

                if text in ["重填", "重新開始"]:
                    reset_answers(user_id)
                    prompt = get_current_question_prompt(user_id)
                    reply_text(
                        reply_token,
                        "已清空資料，重新開始填寫 ✨\n\n" + prompt
                    )
                    continue

                if text == "上一題":
                    set_state(user_id, STATE_FILLING_FORM)
                    set_question_index(user_id, len(QUESTION_FLOW) - 1)
                    field_name = QUESTION_FLOW[-1]["field"]
                    user_data.setdefault(user_id, {})
                    user_data[user_id].setdefault("answers", {})
                    user_data[user_id]["answers"].pop(field_name, None)

                    prompt = get_current_question_prompt(user_id)
                    reply_text(reply_token, f"已返回上一題 👆\n\n{prompt}")
                    continue

                if text in ["是", "要", "有", "需要"]:
                    set_state(user_id, STATE_UPLOADING_IMAGES)
                    reply_text(
                        reply_token,
                        "請開始上傳圖片，可連續傳送多張。\n\n"
                        "上傳完成後請輸入：完成\n"
                        "若要取消本次回報，請輸入：取消"
                    )
                    continue

                if text in ["否", "不用", "不需要", "沒有"]:
                    save_form_data(form_ws, user_id, has_image="N")
                    preview_text = format_preview(get_answers(user_id))
                    case_id = get_case_id(user_id)
                    clear_conversation(user_id)

                    reply_texts(
                        reply_token,
                        [
                            "案件回報完成 ✅",
                            f"案件編號：{case_id}",
                            f"以下為本次回報內容：\n\n{preview_text}"
                        ]
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

                    reply_texts(
                        reply_token,
                        [
                            "案件回報完成 ✅",
                            f"案件編號：{case_id}",
                            f"以下為本次回報內容：\n\n{preview_text}\n\n共收到 {image_count} 張圖片。"
                        ]
                    )
                    continue

                if text == "查看":
                    preview_text = format_preview(get_answers(user_id))
                    image_count = get_image_count(user_id)
                    reply_text(
                        reply_token,
                        f"目前案件內容如下：\n\n{preview_text}\n\n目前已收到 {image_count} 張圖片。"
                    )
                    continue

                reply_text(
                    reply_token,
                    "目前為圖片上傳模式，請直接傳送圖片。\n"
                    "全部完成後請輸入：完成"
                )
                continue

            # =========================
            # 沒有開始回報時的預設提示
            # =========================
            reply_text(
                reply_token,
                "若要建立案件回報，請輸入：開始回報\n\n"
                "系統會依序詢問：\n"
                "服務廠、專員、車號、錯誤件號、錯誤工代、正確件號、正確工代"
            )

        return "OK", 200

    except Exception as e:
        print("Webhook exception type:", type(e).__name__)
        print("Webhook exception detail:", repr(e))
        return "Internal Server Error", 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
