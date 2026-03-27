import os
import re
import json
import base64
import random
import string
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from dotenv import load_dotenv
from openai import OpenAI

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent,
)

# ========= 基本設定 =========
load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise ValueError("請先在 .env 設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET")

if not OPENAI_API_KEY:
    raise ValueError("請先在 .env 設定 OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
app = Flask(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

USERS_FILE = "users.json"
PASSWORDS_FILE = "passwords.json"
FREE_TRIAL_COUNT = 3


# ========= JSON 工具 =========
def load_json_file(path: str, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default

    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default


def save_json_file(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_users():
    return load_json_file(USERS_FILE, {})


def save_users(data):
    save_json_file(USERS_FILE, data)


def load_passwords():
    return load_json_file(PASSWORDS_FILE, {})


def save_passwords(data):
    save_json_file(PASSWORDS_FILE, data)


# ========= 使用者 / 會員 =========
def get_user_record(user_id: str):
    users = load_users()

    if user_id not in users:
        users[user_id] = {
            "free_used": 0,
            "is_active": False,
            "expire_at": None,
            "activated_password": None,
            "created_at": datetime.now().isoformat()
        }
        save_users(users)

    return users[user_id]


def update_user_record(user_id: str, record: dict):
    users = load_users()
    users[user_id] = record
    save_users(users)


def is_membership_active(record: dict) -> bool:
    if not record.get("is_active"):
        return False

    expire_at = record.get("expire_at")
    if not expire_at:
        return False

    try:
        expire_dt = datetime.fromisoformat(expire_at)
    except ValueError:
        return False

    return datetime.now() <= expire_dt


def can_use_feature(user_id: str):
    record = get_user_record(user_id)

    if is_membership_active(record):
        return True, "member"

    free_used = record.get("free_used", 0)
    if free_used < FREE_TRIAL_COUNT:
        return True, "free"

    return False, "locked"


def consume_usage(user_id: str):
    record = get_user_record(user_id)

    if is_membership_active(record):
        return

    record["free_used"] = record.get("free_used", 0) + 1
    update_user_record(user_id, record)


def get_status_text(user_id: str):
    record = get_user_record(user_id)

    if is_membership_active(record):
        expire_str = datetime.fromisoformat(record["expire_at"]).strftime("%Y-%m-%d %H:%M")
        return f"✅ 會員有效中\n到期時間：{expire_str}"

    free_used = record.get("free_used", 0)
    remain = max(0, FREE_TRIAL_COUNT - free_used)
    return f"目前剩餘免費次數：{remain} 次"


# ========= 密碼系統 =========
def generate_password(length=8):
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=length))


def create_new_password(days=30):
    passwords = load_passwords()

    while True:
        new_pwd = generate_password(8)
        if new_pwd not in passwords:
            break

    passwords[new_pwd] = {
        "days": days,
        "used": False,
        "created_at": datetime.now().isoformat()
    }
    save_passwords(passwords)
    return new_pwd


def try_activate_password(user_id: str, text: str):
    passwords = load_passwords()
    record = get_user_record(user_id)

    password = text.strip().upper()

    if password not in passwords:
        return False, "密碼錯誤，請重新輸入。"

    pwd_info = passwords[password]
    if pwd_info.get("used"):
        return False, "這組密碼已經使用過了。"

    days = int(pwd_info.get("days", 30))
    expire_dt = datetime.now() + timedelta(days=days)

    record["is_active"] = True
    record["expire_at"] = expire_dt.isoformat()
    record["activated_password"] = password
    update_user_record(user_id, record)

    pwd_info["used"] = True
    passwords[password] = pwd_info
    save_passwords(passwords)

    expire_str = expire_dt.strftime("%Y-%m-%d %H:%M")
    return True, f"✅ 開通成功\n可使用到：{expire_str}"


# ========= 比價 =========
def parse_price(text: str):
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def search_shopee(product_name: str):
    keyword = quote(product_name)
    url = f"https://shopee.tw/search?keyword={keyword}"
    return {
        "platform": "蝦皮",
        "price": None,
        "url": url
    }


def search_momo(product_name: str):
    keyword = quote(product_name)
    url = f"https://www.momoshop.com.tw/search/searchShop.jsp?keyword={keyword}"

    try:
        res = requests.get(url, headers=HEADERS, timeout=8)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        candidates = re.findall(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{4,6}\b", text)
        prices = []

        for item in candidates:
            price = parse_price(item)
            if price and 200 <= price <= 200000:
                prices.append(price)

        lowest = min(prices) if prices else None

        return {
            "platform": "momo",
            "price": lowest,
            "url": url
        }
    except Exception as e:
        print("momo 抓取失敗：", repr(e))
        return {
            "platform": "momo",
            "price": None,
            "url": url
        }


def search_pchome(product_name: str):
    keyword = quote(product_name)
    api_url = f"https://ecshweb.pchome.com.tw/search/v3.3/all/results?q={keyword}&page=1&sort=sale/dc"
    search_url = f"https://24h.pchome.com.tw/search/?q={keyword}"

    try:
        res = requests.get(api_url, headers=HEADERS, timeout=8)
        res.raise_for_status()
        data = res.json()

        prices = []
        for item in data.get("prods", []):
            price = item.get("price")
            if isinstance(price, int) and 200 <= price <= 200000:
                prices.append(price)

        lowest = min(prices) if prices else None

        return {
            "platform": "PChome",
            "price": lowest,
            "url": search_url
        }
    except Exception as e:
        print("PChome 抓取失敗：", repr(e))
        return {
            "platform": "PChome",
            "price": None,
            "url": search_url
        }


def find_lowest_price(product_name: str):
    results = [
        search_shopee(product_name),
        search_momo(product_name),
        search_pchome(product_name),
    ]

    valid_results = [r for r in results if r["price"] is not None]
    lowest = min(valid_results, key=lambda x: x["price"]) if valid_results else None

    return lowest, results


def format_compare_message(product_name: str, lowest: dict | None, all_results: list[dict]) -> str:
    lines = [f"商品名稱：{product_name}", ""]

    if lowest:
        lines.append(f"🏆 最低價平台：{lowest['platform']}")
        lines.append(f"💰 最低價：約 NT${lowest['price']:,}")
        lines.append(f"🔗 連結：{lowest['url']}")
        lines.append("")
    else:
        lines.append("⚠️ 目前抓不到可比較的即時價格")
        lines.append("可先點下方平台搜尋連結查看")
        lines.append("")

    lines.append("📌 各平台搜尋：")
    for item in all_results:
        if item["price"] is not None:
            lines.append(f"{item['platform']}：NT${item['price']:,}")
        else:
            lines.append(f"{item['platform']}：抓不到價格")
        lines.append(item["url"])
        lines.append("")

    lines.append("👉 建議：實際進入頁面再確認尺寸、運費與賣家評價")
    return "\n".join(lines).strip()


# ========= Flask / Webhook =========
@app.route("/", methods=["GET"])
def home():
    return "LINE bot is running!"


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("簽章驗證失敗")
        abort(400)
    except Exception as e:
        print("Webhook 錯誤：", repr(e))
        abort(500)

    return "OK"


# ========= 文字訊息 =========
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        if text == "我的ID":
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"你的User ID：\n{user_id}")]
                )
            )
            return

        if text == "狀態":
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=get_status_text(user_id))]
                )
            )
            return

        if user_id == ADMIN_USER_ID and text == "建立密碼":
            new_pwd = create_new_password(30)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"✅ 新密碼已建立：\n\n{new_pwd}\n\n有效天數：30天")]
                )
            )
            return

        ok, msg = try_activate_password(user_id, text)
        if ok:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=msg)]
                )
            )
            return

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text="請直接傳商品圖片給我。\n\n可輸入：\n狀態 → 查看剩餘次數或會員期限\n我的ID → 查自己的User ID"
                    )
                ]
            )
        )


# ========= 圖片訊息 =========
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id

    ok, mode = can_use_feature(user_id)
    if not ok:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="❌ 已超過免費次數\n請輸入開通密碼，可使用 30 天。")]
                )
            )
        return

    consume_usage(user_id)

    remain_text = ""
    if mode == "free":
        record = get_user_record(user_id)
        remain = max(0, FREE_TRIAL_COUNT - record.get("free_used", 0))
        remain_text = f"\n免費剩餘次數：{remain} 次"

    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=f"🔍 辨識中，請稍等幾秒...{remain_text}")]
                )
            )
    except Exception as e:
        print("先回覆失敗：", repr(e))
        return

    try:
        with ApiClient(configuration) as api_client:
            blob_api = MessagingApiBlob(api_client)
            content = blob_api.get_message_content(message_id)

        if isinstance(content, bytes):
            image_data = content
        else:
            try:
                image_data = content.read()
            except Exception:
                image_data = bytes(content)

        base64_image = base64.b64encode(image_data).decode("utf-8")

        response = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "你是商品辨識助手。"
                                "請只回覆最適合拿來搜尋購物網站的商品名稱，不要加解釋。"
                                "例如：Nike Air Force 1、iPhone 15、AirPods Pro。"
                                "如果看不出品牌，就回商品通用名稱。"
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "請辨識這張圖片的商品名稱，只回答商品名稱。"
                        },
                        {
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{base64_image}",
                        },
                    ],
                },
            ],
        )

        try:
            product_name = response.output[0].content[0].text.strip()
        except Exception:
            print("OpenAI 原始回應：", response)
            raise ValueError("OpenAI 回應格式和預期不同")

        print("辨識商品：", product_name)

        lowest, all_results = find_lowest_price(product_name)
        result_text = format_compare_message(product_name, lowest, all_results)

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=result_text)]
                )
            )

    except Exception as e:
        print("圖片辨識 / 比價失敗：", repr(e))

        try:
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text="辨識或比價失敗，請再試一次。")]
                    )
                )
        except Exception as push_error:
            print("推送錯誤訊息失敗：", repr(push_error))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)