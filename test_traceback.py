from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

print("正在發送測試請求到 /liff ...")
try:
    response = client.get("/liff")
    print(f"狀態碼: {response.status_code}")
    if response.status_code == 500:
        print("!! 偵測到 500 錯誤 !!")
        # TestClient 通常會直接拋出異常，如果沒有，我們就看 body
        print(f"回應內容: {response.text}")
except Exception as e:
    print(f"捕捉到異常：\n{e}")
    import traceback
    traceback.print_exc()
