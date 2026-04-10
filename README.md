# LINE 辦公室代班管理助手 (Shift Manager Bot)

這是一個專為辦公室與系辦設計的專業級代班管理系統。系統採用 **Google Sheets** 作為雲端後台，結合 **LINE Messaging API** 與 **LIFF (LINE Front-end Framework)**，提供直觀、高效且美觀的自動化代班媒合體驗。

---

## 核心特色

- **Google Sheets 雲端驅動**：所有資料即時儲存於雲端試算表，管理員可隨時透過瀏覽器編輯。
- **多人共事支援**：支援同一個時段有多位工讀生排班，系統會自動辨識並追蹤各自的代班狀態。
- **多選代班申請**：升級後的 LIFF 表單支援「一次勾選多個時段」，大幅縮短請假流程。
- **莫蘭迪美學 Flex Message**：所有的班表查詢結果皆採用精心設計的板岩灰色調 (Slate) 圖卡。
- **效能快取優化**：內建 API 讀取快取機制 (TTL Cache)，有效防止 Google API 請求過頻 (429 Error) 的問題。
- **安全推播保護**：已設定完善的 `.gitignore` 機制並支援 GitHub Secret Scanning 防護。

---

## 開發與環境設定

### 1. 核心環境變數 (.env)
請確保您的專案目錄下有 `.env` 檔案並包含以下資訊：
```env
LINE_CHANNEL_ACCESS_TOKEN=你的Token
LINE_CHANNEL_SECRET=你的Secret
LIFF_ID=你的LIFF編號
```

### 2. Google Sheets 憑證
系統運作需要以下兩份由 Google Cloud Console 產生的金鑰檔案（請放置於專案根目錄）：
- `shift-helper-sheet.json` (Google Sheets API 存取)
- `shift-helper-drive.json` (Google Drive API 存取)

### 3. 啟動指令
```powershell
# 安裝依賴
pip install -r requirements.txt

# 啟動伺服器
uvicorn main:app --reload
```

---

## 機器人指令說明

| 指令內容 | 說明 |
| :--- | :--- |
| **`綁定 [真實姓名]`** | 初始使用必做，將 LINE ID 與班表人名連結 |
| **`查詢 今天/明天`** | 快速查看目前的排班與代班狀態 (支援多人並列) |
| **`查詢 星期X`** | 查看該星期的通用常規班表 |
| **`找代班`** | 喚起 LIFF 申請表單，進行多選時段請假 |
| **`說明書`** | 隨時查看以上指令的圖文教學 |

---

## 管理員後台 (Admin Panel)

瀏覽器開啟 `http://localhost:8000/admin`，您可以：
- 下載班表範本。
- 上傳最新的學期班表。
- 獲取四張核心 Google Sheets 的直接存取連結：
    - Users (使用者綁定)
    - Sub Requests (代班紀錄)
    - Schedule Slots (時段配置)
    - Schedule Plans (班表計畫)

---

## 安全提示
**請勿**將 `shift-helper-*.json` 或 `.env` 檔案推送至公共 Git 存儲庫。本專案已自動設定 `.gitignore` 來排除這些敏感檔案。
