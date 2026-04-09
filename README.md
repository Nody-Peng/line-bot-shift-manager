# LINE 辦公室代班管理機器人 (Shift Manager Bot)

這是一個專為辦公室/系辦設計的 LINE 代班管理系統，支援自動圖卡查詢、LIFF 代班申請以及一鍵接班確認。

## 🚀 快速啟動教學

請依照以下順序在您的終端機 (Terminal) 執行指令：

### 1. 啟動伺服器 (視窗一)
先進入專案目錄，啟動虛擬環境並執行 Uvicorn：
```powershell
# 1. 啟動虛擬環境
.\venv\Scripts\Activate.ps1

# 2. 啟動伺服器
uvicorn main:app --reload
```

### 2. 開啟對外通路 (視窗二)
開啟另一個新的終端機視窗，執行 SSH 隧道（不需要安裝任何軟體）：
```powershell
ssh -R 80:127.0.0.1:8000 nokey@localhost.run
```
執行後，請找到畫面中的 `https://...lhr.life` 網址並複製起來。

---

## ⚙️ LINE 後台設定

1.  登入 [LINE Developers Console](https://developers.line.biz/console/)。
2.  進入您的 Channel -> **Messaging API**。
3.  找到 **Webhook URL**，將剛才複製的網址貼上，並在結尾加上 `/callback`。
    *   例如：`https://eb4eb56d1ca519.lhr.life/callback`
4.  點擊 **Update**，並確保 **Use webhook** 的開關已開啟。

---

## 🤖 機器人主要功能

*   **綁定身分**：輸入 `綁定 [姓名]` (初次使用必做)。
*   **查詢班表**：輸入 `查詢` 或 `查詢 2026-04-13`。
*   **找代班**：輸入 `找代班` 叫出表單填寫。
*   **接手代班**：點擊卡片上的「我願意代班」，系統會跳出彈窗確認，避免誤觸。
*   **管理後台**：瀏覽器開啟 `http://localhost:8000/admin` 可下載範本並上傳 CSV 班表。
