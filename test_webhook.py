import requests
import json
import os
from dotenv import load_dotenv
import base64
import hashlib
import hmac

# 載入環境變數 (為了拿 Channel Secret 算簽名)
load_dotenv()
channel_secret = os.getenv('LINE_CHANNEL_SECRET')

# 本地端伺服器網址
url = "http://127.0.0.1:8000/callback"

# 模擬一段 LINE 傳來的 JSON 資料 (有人說了「哈囉」)
body = {
    "destination": "xxxxxxxxxx",
    "events": [
        {
            "replyToken": "0f3779fba3b349968c5d07db31eab56f",
            "type": "message",
            "mode": "active",
            "timestamp": 1462629479859,
            "source": {
                "type": "user",
                "userId": "U4af4980629..." # 假的使用者 ID
            },
            "message": {
                "id": "325708",
                "type": "text",
                "text": "哈囉"
            }
        }
    ]
}
body_str = json.dumps(body)

# 模擬 LINE 計算數位簽名 (X-Line-Signature)
hash = hmac.new(channel_secret.encode('utf-8'),
                body_str.encode('utf-8'), hashlib.sha256).digest()
signature = base64.b64encode(hash).decode('utf-8')

headers = {
    "Content-Type": "application/json",
    "X-Line-Signature": signature
}

# 發送模擬請求！
print(f"正在發送測試請求到 {url} ...")
try:
    response = requests.post(url, headers=headers, data=body_str)
    print(f"伺服器回傳狀態碼: {response.status_code}")
    print(f"伺服器回傳內容: {response.text}")
except Exception as e:
    print(f"發送失敗: {e}")