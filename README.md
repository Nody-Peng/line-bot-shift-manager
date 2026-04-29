# LINE 代班管理機器人 (Shift Manager Bot)

這是一個基於 LINE Messaging API 與 Google Sheets API 開發的代班管理系統。支援身分綁定、班表查詢、代班申請與自動推播通知。

## 🚀 本地開發啟動指南

### 1. 準備環境
- Python 3.10 以上版本。
- Google Cloud Platform 專案，並啟動 Google Sheets API 與 Drive API。
- LINE Developers 帳號，並建立一個 Messaging API Channel。

### 2. 安裝步驟
1. **複製專案：**
   ```bash
   git clone <repository_url>
   cd line-bot-shift-manager
   ```

2. **建立虛擬環境：**
   ```bash
   python -m venv venv
   .\venv\Scripts\activate  # Windows
   source venv/bin/activate  # macOS/Linux
   ```

3. **安裝套件：**
   ```bash
   pip install -r requirements.txt
   ```

### 3. 設定環境變數 (`.env`)
請在根目錄建立 `.env` 檔案，填入以下資訊：
```env
LINE_CHANNEL_ACCESS_TOKEN=你的TOKEN
LINE_CHANNEL_SECRET=你的SECRET
LIFF_ID=你的LIFF_ID
GOOGLE_SHEET_KEY=你的試算表KEY
DEFAULT_GROUP_ID=你的群組ID (選填)
```

### 4. 設定 Google 憑證
將 Google 服務帳戶的 JSON 憑證重新命名為 `credentials.json` 並放在專案根目錄。

### 5. 啟動服務
1. **啟動 API 伺服器：**
   ```bash
   uvicorn main:app --reload
   ```

2. **建立公網隧道 (Ngrok)：**
   ```bash
   ngrok http 8000
   ```
   獲取 Ngrok 的網址後，前往 LINE Developers Console 的 **Webhook URL** 設定為：`https://你的網址/callback`

## 🛠 關鍵技術
- **Backend:** FastAPI (Python)
- **Database:** Google Sheets
- **UI:** LINE Flex Message & LIFF (HTML/JS)
- **Scheduler:** APScheduler (自動更新快取與提醒)

## 🖥 後台管理與班表輸入
系統內建一個網頁版後台，方便管理者批次匯入學期班表。

### 如何開啟後台
1. 確認您的 API 伺服器已啟動（包含 Ngrok 或其他 Tunnel 服務）。
2. 在瀏覽器中輸入您的服務網址並加上 `/admin`，例如：`https://你的網址/admin`。
3. 進入後台頁面後，您可以查看各個 Google Sheets 試算表的快速連結。

### 班表輸入流程
1. **下載範本**：點擊後台頁面中的「下載 CSV 範本」按鈕，取得最新的 `template.csv` 檔案。
2. **填寫班表**：
   - 請勿更改第一列的標題（星期,時段,姓名）。
   - 「星期」請填入如：星期一、星期二。
   - 「時段」支援的格式（包含：`09:00 - 12:00`, `12:00 - 14:00`, `14:00 - 16:00`, `16:00 - 18:00`, `18:00 - 22:00`, `09:00 - 17:00` 等）。
   - 「姓名」請確認與「使用者對照表」中的白名單姓名完全一致。
3. **上傳匯入**：
   - 在後台填寫「班表計畫名稱」（如：113學年度上學期9月班表）。
   - 設定該計畫生效的「開始/結束日期」。
   - 選擇填寫完畢的 `template.csv`，並點擊「執行匯入」。
   - 資料將自動同步至雲端試算表中。

## 📄 相關文件
- [使用操作指南 (USER_GUIDE.md)](./USER_GUIDE.md): 詳細功能指令說明。
