import gspread
from google.oauth2.service_account import Credentials
import os
import datetime
import time

# Scopes
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# 各個試算表的 ID
SHEET_IDS = {
    "users":           "1ydXoy5L_Ddobe4gRbtE8YM7re8-Vnao0EcPGS13KFDk",
    "sub_requests":    "1xYZ7RJZgZ5iA333aVat2chAOXiwYnts6CWY1dI5kVJ8",
    "schedule_slots":  "1sV6vv2mIdqk27uWOx65TTixajwoUFG6v_hv5wa5JrE4",
    "schedule_plans":  "11WJq7r0sit31o18xMgLsKsutoWul3T0L9u-KZd9ZCI8",
}

_gc = None
_RECORD_CACHE = {}  # {sheet_name: {"time": stamp, "data": []}}

def get_client():
    global _gc
    if _gc is None:
        creds = Credentials.from_service_account_file(
            "shift-helper-sheet.json", scopes=SCOPES
        )
        _gc = gspread.authorize(creds)
    return _gc

def _get_sheet(name: str):
    return get_client().open_by_key(SHEET_IDS[name]).sheet1

def _get_records_cached(name: str, ttl: int = 10) -> list[dict]:
    """取得試算表的所有紀錄，並加入短時間快取減少 API 呼叫。"""
    now = time.time()
    if name in _RECORD_CACHE:
        entry = _RECORD_CACHE[name]
        if now - entry["time"] < ttl:
            return entry["data"]
    
    # 過期或無資料，重新抓取
    sh = _get_sheet(name)
    data = sh.get_all_records()
    _RECORD_CACHE[name] = {"time": now, "data": data}
    return data

def invalidate_cache(name: str):
    """手動讓快取失效（例如新增資料後）。"""
    if name in _RECORD_CACHE:
        del _RECORD_CACHE[name]

def _ensure_headers(sheet, headers: list[str]):
    """確保第一列是 header，如果試算表是空的就自動加上 header。"""
    existing = sheet.row_values(1)
    if not existing or existing != headers:
        sheet.insert_row(headers, 1)

# ─────────────────────────────────────────────
# ❶  users
# ─────────────────────────────────────────────
USERS_HEADERS = ["line_user_id", "name"]

def get_user(line_id: str) -> dict | None:
    records = _get_records_cached("users", ttl=30)
    for r in records:
        if str(r.get("line_user_id", "")).strip() == line_id.strip():
            # 回傳前先把名字也去空白
            r["name"] = str(r.get("name", "")).strip()
            return r
    return None

def upsert_user(line_id: str, name: str) -> str:
    """新增或更新使用者。回傳 'created' 或 'updated'。"""
    sh = _get_sheet("users")
    _ensure_headers(sh, USERS_HEADERS)
    records = sh.get_all_records()  # Upsert 建議用最準確的資料，不使用快取
    for i, r in enumerate(records, start=2):  # row 1 is header
        if str(r.get("line_user_id", "")).strip() == line_id.strip():
            sh.update_cell(i, 2, name)
            invalidate_cache("users")
            return "updated"
    sh.append_row([line_id, name])
    invalidate_cache("users")
    return "created"

# ─────────────────────────────────────────────
# ❷  schedule_plans
# ─────────────────────────────────────────────
PLANS_HEADERS = ["id", "name", "start_date", "end_date"]

def find_active_plan(date_str: str) -> dict | None:
    """找到涵蓋指定日期的班表計畫。若有多個，取最新（ID 最大）的。"""
    records = _get_records_cached("schedule_plans", ttl=60)
    matches = [
        r for r in records
        if str(r.get("start_date", "")) <= date_str <= str(r.get("end_date", ""))
    ]
    if not matches:
        return None
    return max(matches, key=lambda r: int(r.get("id", 0)))

def add_plan(name: str, start_date: str, end_date: str) -> int:
    sh = _get_sheet("schedule_plans")
    _ensure_headers(sh, PLANS_HEADERS)
    records = sh.get_all_records()
    
    if not records:
        new_id = 1
    else:
        ids = [int(r["id"]) for r in records if str(r.get("id", "")).isdigit()]
        new_id = max(ids) + 1 if ids else 1
        
    sh.append_row([new_id, name, start_date, end_date])
    invalidate_cache("schedule_plans")
    return new_id

# ─────────────────────────────────────────────
# ❸  schedule_slots
# ─────────────────────────────────────────────
SLOTS_HEADERS = ["plan_id", "day_of_week", "time_slot", "user_name"]

def get_slots(plan_id: int, day_of_week: str) -> list[dict]:
    records = _get_records_cached("schedule_slots", ttl=60)
    return [
        r for r in records
        if str(r.get("plan_id", "")).strip() == str(plan_id) and
           str(r.get("day_of_week", "")).strip() == day_of_week
    ]

def add_slots_batch(plan_id: int, rows: list[tuple]):
    """批次插入 slots。rows = list of (day_of_week, time_slot, user_name)"""
    sh = _get_sheet("schedule_slots")
    _ensure_headers(sh, SLOTS_HEADERS)
    data = [[plan_id, d, t, u] for (d, t, u) in rows]
    sh.append_rows(data)
    invalidate_cache("schedule_slots")

# ─────────────────────────────────────────────
# ❹  sub_requests
# ─────────────────────────────────────────────
SUBS_HEADERS = ["id", "date", "time_slot", "requester_name", "reason", "sub_user_name", "status"]

def get_sub_requests(date: str, time_slot: str) -> list[dict]:
    records = _get_records_cached("sub_requests", ttl=5)
    return [
        r for r in records
        if str(r.get("date", "")).strip() == date and
           str(r.get("time_slot", "")).strip() == time_slot
    ]

def get_all_sub_requests_for_date(date: str) -> list[dict]:
    """獲取特定日期的所有代班請求（用於查詢優化）。"""
    records = _get_records_cached("sub_requests", ttl=5)
    return [r for r in records if str(r.get("date", "")).strip() == date]

def get_sub_request_by_id(req_id: int) -> dict | None:
    records = _get_records_cached("sub_requests", ttl=5)
    for r in records:
        if str(r.get("id", "")).strip() == str(req_id):
            return r
    return None

def add_sub_request(date: str, time_slot: str, requester_name: str, reason: str) -> dict:
    sh = _get_sheet("sub_requests")
    _ensure_headers(sh, SUBS_HEADERS)
    records = sh.get_all_records()
    
    ids = [int(r["id"]) for r in records if str(r.get("id", "")).isdigit()]
    new_id = max(ids) + 1 if ids else 1
    
    sh.append_row([new_id, date, time_slot, requester_name, reason, "", "尋找中"])
    invalidate_cache("sub_requests")
    return {
        "id": new_id, "date": date, "time_slot": time_slot,
        "requester_name": requester_name, "reason": reason,
        "sub_user_name": "", "status": "尋找中"
    }

def close_sub_request(req_id: int, sub_user_name: str) -> bool:
    """將指定代班請求結案，填入代班人名。回傳是否成功。"""
    sh = _get_sheet("sub_requests")
    _ensure_headers(sh, SUBS_HEADERS)
    records = sh.get_all_records()
    for i, r in enumerate(records, start=2):
        if str(r.get("id", "")).strip() == str(req_id):
            sh.update_cell(i, 6, sub_user_name)
            sh.update_cell(i, 7, "已結案")
            invalidate_cache("sub_requests")
            return True
    return False

# ─────────────────────────────────────────────
# ❺  核心業務邏輯：追蹤最終負責人 (支援複數人員)
# ─────────────────────────────────────────────

def get_slot_owners_info(date_str: str, time_slot: str, cached_subs: list = None) -> list[dict]:
    """
    追蹤指定日期+時段的「所有」負責人（因為一格可能有多人）。
    回傳: list of {'original': 原定人, 'current': 最終負責人, 'seeking_sub': bool}
    """
    plan = find_active_plan(date_str)
    if not plan: return []

    date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = weekdays[date_obj.weekday()]

    # 1. 找出所有原定排班的人
    slots = get_slots(int(plan["id"]), weekday_str)
    original_owners = [str(s["user_name"]).strip() for s in slots if str(s["time_slot"]).strip() == time_slot]

    if not original_owners: return []

    # 2. 抓取該日該時段所有代班紀錄 (如果沒有傳入預抓好的，就去撈)
    if cached_subs is not None:
        sub_requests = [r for r in cached_subs if str(r.get("time_slot", "")).strip() == time_slot]
    else:
        sub_requests = get_sub_requests(date_str, time_slot)
    
    sub_requests.sort(key=lambda r: int(r.get("id", 0)))

    results = []
    for original in original_owners:
        temp_current = original
        seeking = False
        
        for req in sub_requests:
            if str(req.get("requester_name", "")).strip() == temp_current:
                if str(req.get("status", "")).strip() == "已結案" and req.get("sub_user_name"):
                    temp_current = str(req["sub_user_name"]).strip()
                    seeking = False
                elif str(req.get("status", "")).strip() == "尋找中":
                    seeking = True
        
        results.append({
            "original": original,
            "current": temp_current,
            "seeking_sub": seeking
        })
    
    return results

def is_user_responsible_for_slot(date_str: str, time_slot: str, user_name: str) -> bool:
    """檢查特定使用者是否為該時段的負責人（且尚未請假中）。"""
    owners = get_slot_owners_info(date_str, time_slot)
    for o in owners:
        if o["current"] == user_name and not o["seeking_sub"]:
            return True
    return False
