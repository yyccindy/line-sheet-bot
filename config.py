import os
from datetime import timezone, timedelta

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "").strip()

TW_TZ = timezone(timedelta(hours=8))

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
