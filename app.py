import json
import os

from flask import Flask, abort, request

from config import (
    CMD_CANCEL,
    CMD_START,
    STATE_ASK_HAS_IMAGE,
    STATE_FILLING_FORM,
    STATE_UPLOADING_IMAGES,
)
from conversation import (
    clear_conversation,
    get_case_id,
    get_current_question_prompt,
    get_state,
    handle_ask_has_image,
    handle_filling_form,
    handle_image_message,
    handle_non_text,
    handle_uploading_images,
    start_conversation,
)
from services import (
    build_raw_row,
    get_worksheets,
    reply_text,
    reply_texts,
    safe_append_row,
    verify_line_signature,
)

app = Flask(__name__)


def user_started_before(raw_ws, user_id: str) -> bool:
    """
    檢查這位使用者是否曾經傳過開始回報指令。
    只掃最近 50 筆 raw_log，避免每次都讀太多資料。
    """
    try:
        records = raw_ws.get_all_values()
        if not records:
            return False

        recent_rows = records[-50:]
        for row in reversed(recent_rows):
            if len(row) >= 3 and row[1] == user_id and row[2] in CMD_START:
                return True
        return False
    except Exception as e:
        print("user_started_before error:", repr(e))
        return False


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
            safe_append_row(raw_ws, build_raw_row(user_id, text))

            if text in CMD_CANCEL:
                current_state = get_state(user_id)

                if current_state:
                    clear_conversation(user_id)
                    reply_text(reply_token, "已取消本次回報。")
                else:
                    reply_text(
                        reply_token,
                        "目前沒有進行中的案件回報。\n若要建立新案件，請輸入：開始回報"
                    )
                continue

            if text in CMD_START:
                current_state = get_state(user_id)

                if current_state:
                    reply_texts(
                        reply_token,
                        [
                            "你目前已有進行中的案件回報，請先完成目前案件 🙏\n\n"
                            "可用指令：\n"
                            "查看｜上一題｜重填｜取消",
                            "請繼續回答目前這一題：\n" + get_current_question_prompt(user_id),
                        ],
                    )
                else:
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
                if user_started_before(raw_ws, user_id):
                    reply_texts(
                        reply_token,
                        [
                            "目前未查詢到進行中的案件回報。\n可能因閒置時間較久，系統已重新整理狀態。",
                            "若要重新建立案件，請輸入：開始回報",
                        ],
                    )
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
