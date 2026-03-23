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

TW_TZ = timezone(timedelta(hours=8))

# =========================
# In-memory state
# =========================
user_state = {}
user_data = {}

STATE_FILLING_FORM = "filling_form"
STATE_ASK_HAS_IMAGE = "ask_has_image"
STATE_UPLOADING_IMAGES = "uploading_images"

CMD_CANCEL = {"取消", "取消回報", "結束"}
CMD_START = {"開始回報", "我要回報", "開始填寫"}
CMD_RESET = {"重填", "重新開始"}
CMD_YES = {"是", "要", "有", "需要"}
CMD_NO = {"否", "不用", "不需要", "沒有"}

QUESTION_FLOW = [
    {"field": "服務廠", "prompt": "1/7 請輸入【服務廠】\n例如：南港廠"},
    {"field": "專員", "prompt": "2/7 請輸入【專員姓名】\n例如：王小明"},
    {"field": "車號", "prompt": "3/7 請輸入【車號】\n例如：ABC-1234"},
    {"field": "錯誤件號", "prompt": "4/7 請輸入【錯誤件號】\n若有多個，請用逗號分隔\n例如：52119-0K902, 53301-0K420"},
    {"field": "錯誤工代", "prompt": "5/7 請輸入【錯誤工代】\n若有多個，請用逗號分隔\n例如：A12, B34"},
    {"field": "正確件號", "prompt": "6/7 請輸入【正確件號】\n若有多個，請用逗號分隔\n例如：52119-0K903, 53301-0K421"},
    {"field": "正確工代", "prompt": "7/7 請輸入【正確工代】\n若有多個，請用逗號分隔\n例如：A13, B35"},
]

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


def upload_image_to_gcs(image_binary: bytes, filename: str) -> str:
    if not GCS_BUCKET_NAME:
        raise ValueError("Missing GCS_BUCKET_NAME")

    bucket = storage.Client().bucket(GCS_BUCKET_NAME)
    blob = bucket.blob(filename)
    blob.upload_from_string(image_binary, content_type="image/jpeg")
    return f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{filename}"


# =========================
# Helpers
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


# =========================
# Conversation state
# =========================
def start_conversation(user_id: str, form_ws):
    user_state[user_id] = {"state": STATE_FILLING_FORM, "question_index": 0}
    user_data[user_id] = {
        "case_id": generate_case_id(form_ws),
        "answers": {},
        "image_count": 0,
    }


def clear_conversation(user_id: str):
    user_state.pop(user_id, None)
    user_data.pop(user_id, None)


def get_state(user_id: str) -> str:
    return user_state.get(user_id, {}).get("state", "")


def set_state(user_id: str, state: str):
    user_state.setdefault(user_id, {})["state"] = state


def get_question_index(user_id: str) -> int:
    return user_state.get(user_id, {}).get("question_index", 0)


def set_question_index(user_id: str, idx: int):
    user_state.setdefault(user_id, {})["question_index"] = idx


def get_case_id(user_id: str) -> str:
    return user_data.get(user_id, {}).get("case_id", "")


def get_answers(user_id: str) -> dict:
    return user_data.get(user_id, {}).get("answers", {})


def get_image_count(user_id: str) -> int:
    return user_data.get(user_id, {}).get("image_count", 0)


def add_image_count(user_id: str):
    user_data.setdefault(user_id, {}).setdefault("image_count", 0)
    user_data[user_id]["image_count"] += 1


def get_current_question_prompt(user_id: str) -> str:
    idx = get_question_index(user_id)
    return QUESTION_FLOW[idx]["prompt"] if 0 <= idx < len(QUESTION_FLOW) else ""


def save_current_answer(user_id: str, answer: str):
    idx = get_question_index(user_id)
    if 0 <= idx < len(QUESTION_FLOW):
        field = QUESTION_FLOW[idx]["field"]
        user_data.setdefault(user_id, {}).setdefault("answers", {})
        user_data[user_id]["answers"][field] = answer.strip()


def move_to_next_question(user_id: str):
    set_question_index(user_id, get_question_index(user_id) + 1)


def is_question_flow_complete(user_id: str) -> bool:
    return get_question_index(user_id) >= len(QUESTION_FLOW)


def go_to_previous_question(user_id: str) -> bool:
    idx = get_question_index(user_id)
    if idx <= 0:
        return False

    new_idx = idx - 1
    set_question_index(user_id, new_idx)
    field = QUESTION_FLOW[new_idx]["field"]
    user_data[user_id]["answers"].pop(field, None)
    return True


def reset_answers(user_id: str):
    case_id = get_case_id(user_id)
    user_state[user_id] = {"state": STATE_FILLING_FORM, "question_index": 0}
    user_data[user_id] = {"case_id": case_id, "answers": {}, "image_count": 0}


# =========================
# Presentation helpers
# =========================
def format_preview(data: dict) -> str:
    return "\n".join([
        f"服務廠：{data.get('服務廠', '')}",
        f"專員：{data.get('專員', '')}",
        f"車號：{data.get('車號', '')}",
        f"錯誤件號：{data.get('錯誤件號', '')}",
        f"錯誤工代：{data.get('錯誤工代', '')}",
        f"正確件號：{data.get('正確件號', '')}",
        f"正確工代：{data.get('正確工代', '')}",
    ])


def build_partial_preview(user_id: str) -> str:
    answers = get_answers(user_id)
    return "\n".join(
        f"{q['field']}：{answers.get(q['field'], '（未填）')}"
        for q in QUESTION_FLOW
    )


def send_current_question(reply_token: str, user_id: str, prefix: str | None = None):
    prompt = get_current_question_prompt(user_id)
    texts = [prefix, f"請繼續回答目前這一題：\n{prompt}"] if prefix else [prompt]
    reply_texts(reply_token, texts)


def send_view_and_continue(reply_token: str, user_id: str):
    reply_texts(
        reply_token,
        [
            "目前填寫內容如下：\n\n" + build_partial_preview(user_id),
            "請繼續回答目前這一題：\n" + get_current_question_prompt(user_id),
        ],
    )


def send_view_and_yesno(reply_token: str, user_id: str):
    reply_texts(
        reply_token,
        [
            "目前填寫內容如下：\n\n" + build_partial_preview(user_id),
            "請繼續回答是否需要補充圖片：\n請回覆：是 / 否",
        ],
    )


def send_image_mode_summary(reply_token: str, user_id: str):
    reply_texts(
        reply_token,
        [
            f"目前案件內容如下：\n\n{format_preview(get_answers(user_id))}\n\n目前已收到 {get_image_count(user_id)} 張圖片。",
            "若還有圖片請繼續上傳；\n若沒有圖片要補充，也可直接輸入：完成",
        ],
    )


# =========================
# Persistence helpers
# =========================
def save_form_data(form_ws, user_id: str, has_image: str):
    row = build_form_row(
        user_id=user_id,
        display_name=get_display_name(user_id),
        case_id=get_case_id(user_id),
        data=get_answers(user_id),
        has_image=has_image,
    )
    print("FORM_ROW:", row)
    form_ws.append_row(row, value_input_option="USER_ENTERED")


def finish_case(reply_token: str, form_ws, user_id: str):
    image_count = get_image_count(user_id)
    has_image = "Y" if image_count > 0 else "N"
    case_id = get_case_id(user_id)
    preview = format_preview(get_answers(user_id))

    save_form_data(form_ws, user_id, has_image=has_image)
    clear_conversation(user_id)

    if image_count > 0:
        reply_texts(
            reply_token,
            [
                "案件回報完成 ✅",
                f"案件編號：{case_id}",
                f"以下為本次回報內容：\n\n{preview}\n\n共收到 {image_count} 張圖片。",
            ],
        )
    else:
        reply_texts(
            reply_token,
            [
                "案件回報完成 ✅",
                f"案件編號：{case_id}",
                f"以下為本次回報內容：\n\n{preview}\n\n本次未收到圖片。",
            ],
        )


# =========================
# Message handlers
# =========================
def handle_non_text(reply_token: str, user_id: str):
    state = get_state(user_id)

    if state == STATE_FILLING_FORM:
        send_current_question(
            reply_token,
            user_id,
            "目前正在填寫案件資料，請直接輸入文字內容。\n\n可用指令：上一題｜重填｜查看｜取消",
        )
    elif state == STATE_ASK_HAS_IMAGE:
        reply_text(reply_token, "請繼續回答是否需要補充圖片：\n請回覆：是 / 否")
    elif state == STATE_UPLOADING_IMAGES:
        reply_text(reply_token, "目前為圖片上傳模式，請直接傳送圖片。\n若沒有圖片要補充，也可直接輸入：完成")
    else:
        reply_text(reply_token, "若要建立案件回報，請輸入：開始回報")


def handle_image_message(raw_ws, image_ws, reply_token: str, user_id: str, message_id: str):
    raw_ws.append_row(build_raw_row(user_id, f"[IMAGE:{message_id}]"), value_input_option="USER_ENTERED")

    if get_state(user_id) != STATE_UPLOADING_IMAGES:
        reply_text(reply_token, "目前尚未進入圖片補充流程。\n請先輸入「開始回報」，完成案件資料填寫後再上傳圖片。")
        return

    try:
        image_binary = get_line_image_content(message_id)
        image_url = upload_image_to_gcs(image_binary, f"{get_case_id(user_id)}/{message_id}.jpg")
        row = build_image_row(
            case_id=get_case_id(user_id),
            user_id=user_id,
            display_name=get_display_name(user_id),
            message_id=message_id,
            image_url=image_url,
        )
        image_ws.append_row(row, value_input_option="USER_ENTERED")
        add_image_count(user_id)

        reply_text(
            reply_token,
            f"已收到第 {get_image_count(user_id)} 張圖片 ✅\n若還有圖片請繼續上傳；\n全部完成後請輸入：完成",
        )
    except Exception as e:
        import traceback
        print("Image handling exception:", repr(e))
        traceback.print_exc()
        reply_text(reply_token, "圖片處理失敗，請再重新上傳一次。")


def handle_filling_form(reply_token: str, user_id: str, text: str):
    if text == "上一題":
        if go_to_previous_question(user_id):
            reply_text(reply_token, f"已返回上一題 👆\n\n{get_current_question_prompt(user_id)}")
        else:
            send_current_question(reply_token, user_id, "已經是第一題了，無法再返回。")
        return

    if text in CMD_RESET:
        reset_answers(user_id)
        reply_text(reply_token, "已清空資料，重新開始填寫 ✨\n\n" + get_current_question_prompt(user_id))
        return

    if text == "查看":
        send_view_and_continue(reply_token, user_id)
        return

    if not text:
        send_current_question(reply_token, user_id, "此欄位尚未填寫，請直接輸入內容。")
        return

    save_current_answer(user_id, text)
    move_to_next_question(user_id)

    if is_question_flow_complete(user_id):
        set_state(user_id, STATE_ASK_HAS_IMAGE)
        reply_text(reply_token, "文字資料已填寫完成 ✅\n\n請問是否需要補充圖片？\n請回覆：是 / 否")
    else:
        reply_text(reply_token, get_current_question_prompt(user_id))


def handle_ask_has_image(reply_token: str, user_id: str, text: str, form_ws):
    if text == "查看":
        send_view_and_yesno(reply_token, user_id)
        return

    if text in CMD_RESET:
        reset_answers(user_id)
        reply_text(reply_token, "已清空資料，重新開始填寫 ✨\n\n" + get_current_question_prompt(user_id))
        return

    if text == "上一題":
        set_state(user_id, STATE_FILLING_FORM)
        set_question_index(user_id, len(QUESTION_FLOW) - 1)
        last_field = QUESTION_FLOW[-1]["field"]
        user_data[user_id]["answers"].pop(last_field, None)
        reply_text(reply_token, f"已返回上一題 👆\n\n{get_current_question_prompt(user_id)}")
        return

    if text in CMD_YES:
        set_state(user_id, STATE_UPLOADING_IMAGES)
        reply_text(
            reply_token,
            "可開始上傳圖片，可連續傳送多張。\n\n若沒有圖片要補充，也可直接輸入：完成\n若要取消本次回報，請輸入：取消",
        )
        return

    if text in CMD_NO:
        save_form_data(form_ws, user_id, has_image="N")
        case_id = get_case_id(user_id)
        preview = format_preview(get_answers(user_id))
        clear_conversation(user_id)
        reply_texts(
            reply_token,
            ["案件回報完成 ✅", f"案件編號：{case_id}", f"以下為本次回報內容：\n\n{preview}"],
        )
        return

    reply_text(reply_token, "請回覆「是」或「否」。\n\n請繼續回答是否需要補充圖片：\n請回覆：是 / 否")


def handle_uploading_images(reply_token: str, user_id: str, text: str, form_ws):
    if text == "完成":
        finish_case(reply_token, form_ws, user_id)
        return

    if text == "查看":
        send_image_mode_summary(reply_token, user_id)
        return

    reply_text(reply_token, "目前為圖片上傳模式，請直接傳送圖片。\n若還有圖片請繼續上傳；\n若沒有圖片要補充，也可直接輸入：完成")


# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def home():
    return "VERSION-OPTIMIZED", 200


@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_data(as_text=True)
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body, signature):
        abort(400)

    try:
        payload = json.loads(body)
        events = payload.get("events", [])
        if not events:
            return "OK", 200

        raw_ws, form_ws, image_ws = get_worksheets()

        for event in events:
            if event.get("type") != "message":
                continue

            message = event.get("message", {})
            message_type = message.get("type")
            user_id = event.get("source", {}).get("userId", "")
            reply_token = event.get("replyToken", "")

            if message_type == "image":
                handle_image_message(raw_ws, image_ws, reply_token, user_id, message.get("id", ""))
                continue

            if message_type != "text":
                handle_non_text(reply_token, user_id)
                continue

            text = message.get("text", "").strip()
            raw_ws.append_row(build_raw_row(user_id, text), value_input_option="USER_ENTERED")

            if text in CMD_CANCEL:
                clear_conversation(user_id)
                reply_text(reply_token, "已取消本次回報。")
                continue

            if text in CMD_START:
                start_conversation(user_id, form_ws)
                reply_texts(
                    reply_token,
                    [
                        "已開始案件回報 ✅",
                        f"案件編號：{get_case_id(user_id)}\n\n接下來我會依序詢問 7 項資料，\n請直接回覆內容即可。\n\n可用指令：\n上一題｜重填｜查看｜取消",
                        get_current_question_prompt(user_id),
                    ],
                )
                continue

            state = get_state(user_id)

            if state == STATE_FILLING_FORM:
                handle_filling_form(reply_token, user_id, text)
            elif state == STATE_ASK_HAS_IMAGE:
                handle_ask_has_image(reply_token, user_id, text, form_ws)
            elif state == STATE_UPLOADING_IMAGES:
                handle_uploading_images(reply_token, user_id, text, form_ws)
            else:
                reply_text(
                    reply_token,
                    "若要建立案件回報，請輸入：開始回報\n\n系統會依序詢問：\n服務廠、專員、車號、錯誤件號、錯誤工代、正確件號、正確工代",
                )

        return "OK", 200

    except Exception as e:
        print("Webhook exception type:", type(e).__name__)
        print("Webhook exception detail:", repr(e))
        return "Internal Server Error", 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
