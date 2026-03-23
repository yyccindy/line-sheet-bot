from config import (
    CMD_NO,
    CMD_RESET,
    CMD_YES,
    QUESTION_FLOW,
    STATE_ASK_HAS_IMAGE,
    STATE_FILLING_FORM,
    STATE_UPLOADING_IMAGES,
)
from services import (
    build_form_row,
    build_image_row,
    build_raw_row,
    generate_case_id,
    get_display_name,
    get_line_image_content,
    reply_text,
    reply_texts,
    upload_image_to_gcs,
)

# =========================
# In-memory state
# =========================
user_state = {}
user_data = {}


# =========================
# State helpers
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
# Persistence
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
