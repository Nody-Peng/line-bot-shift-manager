import os, hmac, hashlib, base64
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Response, BackgroundTasks
from linebot import LineBotApi, WebhookHandler
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    FollowEvent, JoinEvent,
    PostbackEvent, FlexSendMessage,
    TemplateSendMessage, ConfirmTemplate, PostbackAction
)
import datetime
import csv
from io import StringIO
from fastapi.templating import Jinja2Templates
from fastapi.responses import FileResponse
from dotenv import load_dotenv
from pydantic import BaseModel
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# Google Sheets 整合
import sheets

load_dotenv()

class SubSubmitData(BaseModel):
    date: str
    time_slots: list[str]
    reason: str
    userId: str
    groupId: Optional[str] = None
    notify_targets: list[str] = []  # 申請人指定要推播通知的對象 ID 清單

app = FastAPI()
templates = Jinja2Templates(directory="templates")

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

LIFF_ID = os.getenv('LINE_LIFF_ID', 'YOUR_LIFF_ID')
DEFAULT_GROUP_ID = os.getenv('LINE_GROUP_ID')

timezone = pytz.timezone('Asia/Taipei')
scheduler = BackgroundScheduler(timezone=timezone)

# ─────────────────────────────────────────────
# Web Routes
# ─────────────────────────────────────────────

@app.get("/")
def read_root():
    return {"message": "LINE Bot is running!", "admin_url": "/admin"}

@app.get("/liff")
async def get_liff_form(request: Request):
    return templates.TemplateResponse(
        request=request, name="liff_form.html", context={"liff_id": LIFF_ID}
    )

@app.get("/admin")
async def admin_page(request: Request):
    return templates.TemplateResponse(request=request, name="admin_upload.html")

@app.get("/download-template")
async def download_template():
    file_path = os.path.join(os.getcwd(), "template.csv")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Template file not found")
    return FileResponse(path=file_path, filename="template.csv", media_type="text/csv")

@app.post("/upload-csv")
async def upload_csv(
    plan_name: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    file: UploadFile = File(...)
):
    if not file.filename.endswith('.csv'):
        return {"error": "請上傳 CSV 檔案"}

    content = await file.read()
    decoded = content.decode('utf-8')
    csv_reader = csv.DictReader(StringIO(decoded))

    try:
        # 建立班表計畫
        plan_id = sheets.add_plan(plan_name, start_date, end_date)

        # 批次寫入時段
        rows = []
        for row in csv_reader:
            rows.append((
                row['星期'].strip(),
                row['時段'].strip(),
                row['姓名'].strip()
            ))
        if rows:
            sheets.add_slots_batch(plan_id, rows)

    except Exception as e:
        return {"error": f"匯入失敗: {str(e)}"}

    return {"message": f"班表「{plan_name}」匯入成功！(計畫 ID: {plan_id})"}


# ─────────────────────────────────────────────
# LIFF API：代班申請
# ─────────────────────────────────────────────

@app.get("/api/get-notify-targets")
async def api_get_notify_targets():
    """回傳預設通知群組資訊。"""
    targets = []
    if DEFAULT_GROUP_ID and str(DEFAULT_GROUP_ID).strip():
        targets.append({"type": "group", "id": DEFAULT_GROUP_ID, "name": "辦公室群組"})
    # 移除個人成員回傳，節省 quota 並簡化使用者選擇
    return {"targets": targets}



@app.post("/api/submit-sub")
async def api_submit_sub(data: SubSubmitData):
    # 1. 時間檢核：確認是否為台北時區的今天
    today_dt = datetime.datetime.now(timezone)
    today_str = today_dt.strftime("%Y-%m-%d")
    
    if data.date < today_str:
        raise HTTPException(status_code=400, detail="系統提示：無法申請過去日期的代班。")

    # 2. 確認使用者已綁定
    user = sheets.get_user(data.userId)
    if not user:
        raise HTTPException(status_code=400, detail="請先在對話框輸入「綁定 您的姓名」。")

    user_name = str(user["name"]).strip()
    success_requests = []
    errors = []

    # 3. 處理每一個申請的時段
    for slot in data.time_slots:
        # 如果日期是今天，檢查時段是否已開始 (簡單解析第一個時間點)
        # 例如 "09:00 - 12:00" 取 "09:00"
        if data.date == today_str:
            try:
                start_time_str = slot.split("-")[0].strip()
                # 結合日期與起始時間，並設為台北時區
                start_time_naive = datetime.datetime.strptime(f"{data.date} {start_time_str}", "%Y-%m-%d %H:%M")
                start_time = timezone.localize(start_time_naive)
                
                if today_dt > start_time:
                    errors.append(f"{slot}: 此時段已開始或已結束，無法申請。")
                    continue
            except:
                pass

        # 身分檢核 (使用新的多人支援邏輯)
        if not sheets.is_user_responsible_for_slot(data.date, slot, user_name):
            print(f"DEBUG: User {user_name} not responsible for {data.date} {slot}")
            errors.append(f"{slot}: 您不是該時段的負責人或已申請代班。")
            continue

        # 建立代班請求
        req = sheets.add_sub_request(data.date, slot, user_name, data.reason)
        success_requests.append(req)

    # 4. 如果全失敗
    if not success_requests:
        raise HTTPException(status_code=400, detail="\n".join(errors) or "申請處理失敗")

    # 正式改為零額度流程後，此 API 不再主動推播。
    # 僅回報處理成功的筆數。
    return {"status": "success", "count": len(success_requests)}
    

@app.get("/api/get-sub-flex/{req_id}")
async def api_get_sub_flex(req_id: int):
    req = sheets.get_sub_request_by_id(req_id)
    if not req:
        raise HTTPException(status_code=404, detail="找不到該代班請求")
    
    # 這裡只回傳不含取消按鈕的卡片，供分享使用
    flex = create_sos_card(req, str(req["requester_name"]), show_cancel=False)
    return {"flex_contents": [flex.contents]}

@app.get("/api/get-my-shifts")
async def api_get_my_shifts(date: str, userId: str):
    user = sheets.get_user(userId)
    if not user:
        return {"shifts": []}
    
    user_name = str(user["name"]).strip()
    
    # 獲取該日期所有時段的負責人資訊
    # 先抓取當天所有代班紀錄以利優化
    all_day_subs = sheets.get_all_sub_requests_for_date(date)
    
    plan = sheets.find_active_plan(date)
    if not plan:
        return {"shifts": []}
    
    dt_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = weekday_names[dt_obj.weekday()]
    slots = sheets.get_slots(int(plan["id"]), weekday_str)
    
    my_slots = []
    unique_slots = sorted(list(set(str(s["time_slot"]).strip() for s in slots)))
    
    for s_name in unique_slots:
        owners = sheets.get_slot_owners_info(date, s_name, cached_subs=all_day_subs)
        # 檢查該人員是否為目前的負責人且「不在徵代班中」
        for o in owners:
            if o["current"] == user_name and not o["seeking_sub"]:
                my_slots.append(s_name)
    
    return {"shifts": my_slots}


# ─────────────────────────────────────────────
# 自動化排程：前一晚提醒
# ─────────────────────────────────────────────

def nightly_broadcast_task():
    """每天 20:00 執行，通知隔天的代班異動。"""
    # ── 1. 資料清理：先將過去的資料封存到 History 分頁 ──
    try:
        archive_count = sheets.archive_old_sub_requests()
        if archive_count > 0:
            print(f"[Scheduler] 已封存 {archive_count} 筆過期資料。")
    except Exception as e:
        print(f"[Scheduler] 資料封存失敗: {e}")

    # ── 2. 發送提醒 ──
    tomorrow_dt = datetime.datetime.now(timezone) + datetime.timedelta(days=1)
    tomorrow = tomorrow_dt.strftime("%Y-%m-%d")
    
    # 取得隔天的所有代班請求
    all_subs = sheets.get_all_sub_requests_for_date(tomorrow)
    if not all_subs:
        return

    # 分類：已結案 (有人接) 與 尋找中 (沒人接)
    matched = [s for s in all_subs if str(s.get("status")).strip() == "已結案"]
    seeking = [s for s in all_subs if str(s.get("status")).strip() == "尋找中"]

    if not matched and not seeking:
        return

    # 生成提醒圖卡
    flex = create_reminder_flex(tomorrow, matched, seeking)
    
    # 找出發送目標 (優先使用最新的 groupId)
    target_id = DEFAULT_GROUP_ID
    if not target_id and all_subs:
        # 這裡我們無法直接從試算表得知 groupId (因為試算表沒存)，
        # 通常建議在 .env 設定固定群組 ID，或在申請時寫入一個專門存 groupId 的地方。
        # 暫時假設管理員已設定 DEFAULT_GROUP_ID。
        pass

    if target_id:
        try:
            line_bot_api.push_message(target_id, flex)
        except Exception as e:
            print(f"Broadcast error: {e}")

@app.on_event("startup")
def start_scheduler():
    scheduler.add_job(nightly_broadcast_task, 'cron', hour=20, minute=0)
    scheduler.start()
    print("Scheduler started.")


# ─────────────────────────────────────────────
# LINE Webhook
# ─────────────────────────────────────────────

@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get('X-Line-Signature', '')
    body = await request.body()
    body_str = body.decode('utf-8')

    channel_secret = os.getenv('LINE_CHANNEL_SECRET', '').encode('utf-8')
    hash_digest = hmac.new(channel_secret, body, hashlib.sha256).digest()
    expected_signature = base64.b64encode(hash_digest).decode('utf-8')
    if not hmac.compare_digest(expected_signature, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    background_tasks.add_task(handler.handle, body_str, signature)
    return Response(content='OK', status_code=200, media_type='text/plain')


# ─────────────────────────────────────────────
# 事件：加好友 / 進群組
# ─────────────────────────────────────────────

@handler.add(FollowEvent)
def handle_follow(event):
    try:
        flex = create_help_flex()
        line_bot_api.reply_message(event.reply_token, flex)
    except Exception as e:
        if "400" not in str(e):
            print(f"handle_follow error: {e}")

@handler.add(JoinEvent)
def handle_join(event):
    try:
        flex = create_help_flex()
        line_bot_api.reply_message(event.reply_token, flex)
    except Exception as e:
        if "400" not in str(e):
            print(f"handle_join error: {e}")


# ─────────────────────────────────────────────
# 事件：文字訊息
# ─────────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    user_id = event.source.user_id
    user = sheets.get_user(user_id)

    try:
        # ── 1. 綁定身分 (不受限制) ──────────────────────
        if msg.startswith("綁定 "):
            name = msg.split(" ", 1)[1].strip()
            res = sheets.bind_whitelist_user(user_id, name)
            
            if res == "success":
                reply = f"系統提示：綁定成功！您現在是「{name}」。"
            elif res == "already_bound_to_you":
                reply = f"系統提示：您已經綁定為「{name}」了。"
            elif res == "bound_to_other":
                reply = f"系統提示：此姓名已被其他人綁定，請聯繫管理員。"
            elif res == "not_in_whitelist":
                reply = "系統提示：非管理名單內的人員，請洽負責人。"
            else:
                reply = "系統提示：綁定失敗，請稍後再試。"
                
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        # ── 1.5 處理 LIFF 發出的代班申請指令 (#申請代班) ──
        elif msg.startswith("#申請代班"):
            try:
                print(f"[Command] Received #申請代班 from {user_id}")
                # 解析格式：#申請代班\n日期: 2024-05-01\n時段: 09:00 - 12:00, 14:00 - 16:00\n理由: 私事
                lines = msg.split("\n")
                req_data = {}
                for line in lines:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        req_data[k.strip()] = v.strip()
                
                date = req_data.get("日期")
                slots_str = req_data.get("時段")
                reason = req_data.get("理由", "無")
                
                if not date or not slots_str:
                    print(f"[Command] Invalid data: date={date}, slots={slots_str}")
                    return

                if not user:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統提示：請先完成身分綁定再申請代班。"))
                    return
                
                user_name = str(user["name"]).strip()
                time_slots = [s.strip() for s in slots_str.split(",")]
                print(f"[Command] Processing {len(time_slots)} slots for {user_name}")
                
                success_reqs = []
                for slot in time_slots:
                    new_req = sheets.add_sub_request(date, slot, user_name, reason)
                    success_reqs.append(new_req)
                
                # 回覆 SOS 卡片 (使用 Carousel 支援多時段)
                if success_reqs:
                    bubbles = []
                    for req in success_reqs:
                        flex_obj = create_sos_card(req, user_name)
                        bubbles.append(flex_obj.contents)
                    
                    if len(bubbles) == 1:
                        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="代班請求", contents=bubbles[0]))
                    else:
                        line_bot_api.reply_message(event.reply_token, FlexSendMessage(alt_text="代班請求組合", contents={"type": "carousel", "contents": bubbles[:12]}))
                    print(f"[Command] Success: Sent {len(bubbles)} cards via Reply")
                return
            except Exception as e:
                print(f"Handle #申請代班 Error: {e}")
                import traceback
                traceback.print_exc()
                return

        # ── 2. 指令面板/幫助訊息 (不受限制) ──────────
        elif msg in ["主選單", "菜單", "menu", "Menu"]:
            flex = create_main_menu_flex()
            line_bot_api.reply_message(event.reply_token, flex)
            return

        elif msg in ["說明書", "幫助", "指令", "使用說明", "help", "Help", "?", "？"]:
            flex = create_help_flex()
            line_bot_api.reply_message(event.reply_token, flex)
            return

        # ── 3. 權限檢查 (其餘指令需先綁定) ────────────────
        # 定義需要權限的指令
        authorized_commands = ["找代班", "查班表", "查詢", "查代班", "接代班", "市場", "我是誰"]
        is_cmd = any(msg.startswith(c) for c in authorized_commands)

        if is_cmd and not user:
            reply = "系統提示：您尚未完成身分綁定，目前無法使用此系統。\n\n請輸入「綁定 您的姓名」來開始使用。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        user_name = str(user["name"]).strip()

        # ── 4. 我是誰 ──────────────────────────────
        if msg == "我是誰":
            reply = f"【身分資訊】\n綁定姓名：{user_name}\nLINE ID：{user_id}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

        # ── 5. 找代班 ────────────────────────────────
        elif msg == "找代班":
            liff_url = f"https://liff.line.me/{LIFF_ID}"
            flex = FlexSendMessage(
                alt_text="開啟代班申請",
                contents={
                    "type": "bubble",
                    "body": {
                        "type": "box", "layout": "vertical", "paddingAll": "20px",
                        "contents": [
                            {"type": "text", "text": "代班申請", "weight": "bold", "size": "xl", "color": "#334155"},
                            {"type": "text", "text": "填寫表單以發布需求", "size": "sm", "color": "#64748B", "margin": "md"}
                        ]
                    },
                    "footer": {
                        "type": "box", "layout": "vertical", "paddingAll": "10px",
                        "contents": [
                            {"type": "button", "style": "primary", "color": "#64748B",
                             "action": {"type": "uri", "label": "填寫申請", "uri": liff_url}}
                        ]
                    }
                }
            )
            line_bot_api.reply_message(event.reply_token, flex)

        # ── 3. 查詢 ──────────────────────────────────
        elif msg.startswith("查詢") or msg == "查班表":
            parts = msg.split(" ", 1)
            query_target = parts[1].strip() if len(parts) > 1 else None

            # 如果只有「查詢」二字，彈出日期選擇器
            if not query_target:
                flex = create_query_picker_flex()
                line_bot_api.reply_message(event.reply_token, flex)
                return

            today = datetime.datetime.now()
            today_str = today.strftime("%Y-%m-%d")
            weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

            # 解析查詢目標
            target_date_str = None
            specific_weekday = None

            if query_target == "今天":
                target_date_str = today_str
            elif query_target == "明天":
                target_date_str = (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            elif query_target == "昨天":
                target_date_str = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            elif query_target in weekday_names:
                specific_weekday = query_target
            else:
                try:
                    dt = datetime.datetime.strptime(query_target, "%Y-%m-%d")
                    target_date_str = dt.strftime("%Y-%m-%d")
                except ValueError:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text="格式錯誤，請輸入：今天 / 明天 / 昨天 / 星期X / YYYY-MM-DD"))
                    return

            # ── 3a. 查詢某星期幾的通用班表 ──
            if specific_weekday:
                plan = sheets.find_active_plan(today_str)
                if not plan:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text="系統提示：目前日期沒有啟用的班表計畫。"))
                    return
                slots = sheets.get_slots(int(plan["id"]), specific_weekday)
                if not slots:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"【{specific_weekday}】在目前計畫中沒有排班。"))
                    return
                flex = create_weekday_flex(str(plan["name"]), specific_weekday, slots)
                line_bot_api.reply_message(event.reply_token, flex)

            # ── 3b. 查詢某特定日期（含代班異動）──
            elif target_date_str:
                plan = sheets.find_active_plan(target_date_str)
                if not plan:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"[{target_date_str}] 查無適用的班表計畫。"))
                    return

                dt_obj = datetime.datetime.strptime(target_date_str, "%Y-%m-%d")
                weekday_str = weekday_names[dt_obj.weekday()]
                slots = sheets.get_slots(int(plan["id"]), weekday_str)

                if not slots:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"[{target_date_str}] {weekday_str} 無排班。"))
                    return

                # 取得每個時段的最終負責人資訊 (改為分組邏輯 + 效能優化)
                from collections import defaultdict
                grouped_results = defaultdict(list)
                
                # [優化] 一次抓完該日所有代班紀錄
                all_day_subs = sheets.get_all_sub_requests_for_date(target_date_str)
                
                unique_slots_names = sorted(list(set(str(s["time_slot"]).strip() for s in slots)))
                for s_name in unique_slots_names:
                    # 傳入 all_day_subs 避免迴圈內重複打 API
                    owners = sheets.get_slot_owners_info(target_date_str, s_name, cached_subs=all_day_subs)
                    grouped_results[s_name].extend(owners)

                flex = create_daily_flex(target_date_str, weekday_str, grouped_results)
                line_bot_api.reply_message(event.reply_token, flex)

        # ── 4. 代班大廳 (接代班) ───────────────────────
        elif msg in ["查代班", "接代班", "市場"]:
            active_reqs = sheets.get_all_active_sub_requests()
            if not active_reqs:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統提示：目前沒有待接手的代班需求。"))
                return
            
            # 依日期排序
            active_reqs.sort(key=lambda x: x['date'])
            flex = create_market_carousel(active_reqs)
            line_bot_api.reply_message(event.reply_token, flex)



        # ── 6. 查詢群組 ID (方便設定通知) ──────────────
        elif msg == "群組ID":
            gid = getattr(event.source, "group_id", None) or getattr(event.source, "room_id", None)
            if gid:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"此群組的 ID 為：\n{gid}\n\n請將此 ID 複製並填入 .env 的 LINE_GROUP_ID 中。"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="這是一對一聊天，無法獲取群組 ID。請在群組中輸入此指令。"))

        # ── 7. 預設回覆 ──────────────────────────────
        else:
            # 私訊時自動跳出主選單；群組中則保持安靜以免干擾。
            if event.source.type == "user":
                flex = create_main_menu_flex()
                line_bot_api.reply_message(event.reply_token, flex)

    except Exception as e:
        print(f"handle_message Error: {e}")
        # Quota exceeded 等錯誤處理
        if "Quota exceeded" in str(e):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 系統忙碌中（API 配額達上限），請稍候 30 秒再試。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統忙碌中，請稍後再試。"))


# ─────────────────────────────────────────────
# Flex Message 模板生成器
# ─────────────────────────────────────────────

def create_help_flex() -> FlexSendMessage:
    """生成系統說明書 Flex Message。"""
    return FlexSendMessage(
        alt_text="使用說明書",
        contents={
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "paddingAll": "20px",
                "contents": [
                    {"type": "text", "text": "代班小幫手", "color": "#94A3B8", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": "使用說明書", "color": "#334155", "size": "xl", "weight": "bold", "margin": "sm"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "20px", "spacing": "sm",
                "contents": [
                    {
                        "type": "box", "layout": "vertical",
                        "contents": [
                            {"type": "text", "text": "1. 帳號綁定", "weight": "bold", "size": "md", "color": "#475569"},
                            {"type": "text", "text": "關鍵字：綁定+空格+姓名、我是誰\n• 範例：綁定 王小明\n• 功能：初次使用必做，連結後才能執行代班操作。", "size": "sm", "color": "#64748B", "wrap": True, "margin": "sm"}
                        ]
                    },
                    {"type": "separator", "margin": "md"},
                    {
                        "type": "box", "layout": "vertical", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "2. 查班表與排班", "weight": "bold", "size": "md", "color": "#475569"},
                            {"type": "text", "text": "關鍵字：查詢、查班表\n• 進階用法：查詢 [今天/明天/星期幾/日期]\n• 範例：查詢 星期五、查詢 2024-05-01", "size": "sm", "color": "#64748B", "wrap": True, "margin": "sm"}
                        ]
                    },
                    {"type": "separator", "margin": "md"},
                    {
                        "type": "box", "layout": "vertical", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "3. 找代班 (送出請求)", "weight": "bold", "size": "md", "color": "#475569"},
                            {"type": "text", "text": "關鍵字：找代班\n• 功能：填寫請假日期與時段，送出後系統會推播通知至指定的成員與群組。", "size": "sm", "color": "#64748B", "wrap": True, "margin": "sm"}
                        ]
                    },
                    {"type": "separator", "margin": "md"},
                    {
                        "type": "box", "layout": "vertical", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "4. 接代班與市場", "weight": "bold", "size": "md", "color": "#475569"},
                            {"type": "text", "text": "關鍵字：接代班、查代班、市場\n• 功能：查看目前所有開放中的代班需求，點擊卡片「確認接手」即可完成媒合。", "size": "sm", "color": "#64748B", "wrap": True, "margin": "sm"}
                        ]
                    },
                    {"type": "separator", "margin": "md"},
                    {
                        "type": "box", "layout": "vertical", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "5. 功能選單與說明", "weight": "bold", "size": "md", "color": "#475569"},
                            {"type": "text", "text": "關鍵字：主選單、說明書、幫助、?\n• 功能：呼叫快速按鈕面板或此份使用指南。", "size": "sm", "color": "#64748B", "wrap": True, "margin": "sm"}
                        ]
                    }
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "10px",
                "contents": [
                    {"type": "text", "text": "輸入「說明書」或「幫助」可隨時喚出此頁面", "size": "xs", "color": "#CBD5E1", "align": "center"}
                ]
            }
        }
    )


# ─────────────────────────────────────────────
# 事件：Postback (按鈕)
# ─────────────────────────────────────────────

@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    postback_data = event.postback.data

    try:
        data_dict = dict(item.split("=") for item in postback_data.split("&"))
        action = data_dict.get("action")

        # ── 查詢日期（來自 datetimepicker）──
        if action == "query_by_date":
            selected_date = event.postback.params.get("date")
            if selected_date:
                # 建立一個 Mock Event 來觸發查詢邏輯
                class MockMessage: text = f"查詢 {selected_date}"
                class MockEvent:
                    message = MockMessage()
                    reply_token = event.reply_token
                    source = event.source
                return handle_message(MockEvent())

        # ── 接手代班（第一步：嘗試鎖定 + 確認彈窗）──
        if action == "accept_sub":
            req_id = int(data_dict.get("request_id"))
            user = sheets.get_user(user_id)
            if not user:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="系統提示：請先輸入「綁定 [姓名]」才能執行此操作。"))
                return
            user_name = str(user["name"]).strip()
            req = sheets.get_sub_request_by_id(req_id)

            if not req or str(req.get("status", "")).strip() in ["已結案", "已撤回"]:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="狀態通知：此代班請求已結束或已撤回。"))
                return

            # 不能接手自己的
            if str(req.get("requester_name", "")).strip() == user_name:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="系統提示：您無法接手由自己發出的代班請求。"))
                return

            # 嘗試取得鎖定（防止同時接手的競爭）
            lock_result = sheets.try_lock_sub_request(req_id, user_name)
            if lock_result == "locked_by_other":
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="系統提示：已有人正在確認接手此班次，請稍候片刻再試。"))
                return
            # lock_result == "ok" or "already_locked_by_you"

            confirm_template = ConfirmTemplate(
                text=(f"請確認是否接手此代班作業：\n\n"
                      f"日期：{req['date']}\n"
                      f"時段：{req['time_slot']}\n"
                      f"申請人：{req['requester_name']}"),
                actions=[
                    PostbackAction(label="確認接手", data=f"action=confirm_accept_sub&request_id={req_id}"),
                    PostbackAction(label="取消操作", data=f"action=cancel_lock&request_id={req_id}")
                ]
            )
            line_bot_api.reply_message(event.reply_token,
                TemplateSendMessage(alt_text="確認代班作業", template=confirm_template))

        # ── 接手代班（第二步：驗證鎖定 + 正式更新）──
        elif action == "confirm_accept_sub":
            req_id = int(data_dict.get("request_id"))

            user = sheets.get_user(user_id)
            if not user:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="系統提示：請先輸入「綁定 [姓名]」才能執行此操作。"))
                return

            user_name = str(user["name"]).strip()
            req = sheets.get_sub_request_by_id(req_id)

            if not req or str(req.get("status", "")).strip() in ["已結案", "已撤回"]:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="處理失敗：此代班已由他人接手或已撤回。"))
                return

            # 驗證鎖定是否仍屬於此用戶（防止搶鎖）
            pending = str(req.get("pending_taker", "")).strip()
            if not pending or not pending.startswith(user_name + "|"):
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="處理失敗：鎖定過期或已被取消，請重新點擊「接手」。"))
                return

            # 不能接手自己的
            if str(req.get("requester_name", "")).strip() == user_name:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="系統提示：您無法接手自己的代班請求。"))
                return

            # ★ 防止同時段已有排班的人來接手
            # （例外：若此人已將該班委託出去，即自己也是某個已結案代班的申請人，則允許接回）
            plan = sheets.find_active_plan(str(req["date"]))
            if plan:
                dt_obj = datetime.datetime.strptime(str(req["date"]), "%Y-%m-%d")
                weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
                weekday_str = weekday_names[dt_obj.weekday()]
                slots = sheets.get_slots(int(plan["id"]), weekday_str)
                scheduled_names = [str(s["user_name"]).strip() for s in slots
                                   if str(s.get("time_slot", "")).strip() == str(req["time_slot"]).strip()]
                if user_name in scheduled_names:
                    # 檢查此人是否已將此班委託出去（允許接回自己的班）
                    related_subs = sheets.get_sub_requests(str(req["date"]), str(req["time_slot"]))
                    already_delegated = any(
                        str(r.get("requester_name", "")).strip() == user_name
                        and str(r.get("status", "")).strip() == "已結案"
                        for r in related_subs
                    )
                    if not already_delegated:
                        sheets.release_lock(req_id)  # 釋放鎖定
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(
                            text=f"系統提示：您在 {req['date']} {req['time_slot']} 本來就有排班，無法接手同時段的代班。"))
                        return

            # 結案（同時清除 pending_taker）
            success = sheets.close_sub_request(req_id, user_name)
            if not success:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="處理失敗：請稍後再試。"))
                return

            # 1. 處理回覆與推播邏輯 (避免在群組操作時重複發送)
            success_flex = create_matching_success_flex(req, user_name, role="taker")
            group_flex = create_match_group_notification_flex(req, user_name)
            
            if event.source.type == "group":
                # 在群組操作，直接回覆成功訊息 (免費)
                line_bot_api.reply_message(event.reply_token, group_flex)
            else:
                # 在私訊操作，回覆接手人成功訊息 (免費)
                line_bot_api.reply_message(event.reply_token, success_flex)
                # 同步通知群組 (Push 1 則)
                if DEFAULT_GROUP_ID and str(DEFAULT_GROUP_ID).strip():
                    try:
                        line_bot_api.push_message(DEFAULT_GROUP_ID, group_flex)
                    except Exception as e:
                        print(f"Group notify error: {e}")

            # 移除對原申請人的主動 Push 通知以節省額度 (申請人請看群組或自行查詢)

        # ── 申請人撤回 (媒合前) ──
        elif action == "cancel_sub_req_start":
            req_id = int(data_dict.get("request_id"))
            user = sheets.get_user(user_id)
            if not user:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請先完成身分綁定。"))
                return
            
            user_name = str(user["name"]).strip()
            req = sheets.get_sub_request_by_id(req_id)
            if not req:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="找不到該請求。"))
                return

            # 安全檢查：限本人撤回
            if str(req.get("requester_name", "")).strip() != user_name:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="權限提示：只有原申請人可以撤回此代班計畫。"))
                return

            if str(req.get("status", "")).strip() == "已結案":
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="撤回失敗：此請求已被接手，申請人無法再撤回。\n若希望取消代班，請通知接手人在 5 分鐘內自行取消接手。"))
                return
            if str(req.get("pending_taker", "")).strip():
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="撤回失敗：目前正有人在確認接手此班次，請稍後再試。\n若對方接手成功，則無法撤回。"))
                return
            confirm_flex = create_cancellation_confirm_flex(req_id)
            line_bot_api.reply_message(event.reply_token, confirm_flex)

        # ── 申請人撤回確認 ──
        elif action == "confirm_cancel_sub_req":
            req_id = int(data_dict.get("request_id"))
            user = sheets.get_user(user_id)
            if not user: return
            user_name = str(user["name"]).strip()
            req = sheets.get_sub_request_by_id(req_id)
            res = sheets.cancel_sub_request_by_id(req_id, user_name)

            if res == "withdrawn_by_requester":
                group_msg = f"代班資訊更新：\n\n申請人 {user_name} 已撤回 {req['date']} ({req['time_slot']}) 的代班請求。\n該時段已不再需要代班。"
                if event.source.type == "group":
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=group_msg))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已成功撤回代班請求，該時段已移出代班市場。"))
                    # 移除主動 Push 通知以節省額度
            elif res == "already_matched":
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="撤回失敗：此請求已被他人接手。\n若希望取消代班，請通知接手人在 5 分鐘內自行取消接手。"))
            elif res == "currently_locked":
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="撤回失敗：目前正有人在確認接手此班次，請稍後再試。\n若對方接手成功，則無法撤回。"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="撤回失敗，請稍後再試。"))

        # ── 接手人 5分鐘內取消接手 ──
        elif action == "cancel_by_taker":
            req_id = int(data_dict.get("request_id"))
            user = sheets.get_user(user_id)
            if not user: return
            user_name = str(user["name"]).strip()
            req = sheets.get_sub_request_by_id(req_id)
            if not req:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="找不到該請求。"))
                return
            if str(req.get("sub_user_name", "")).strip() != user_name:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統提示：您不是此次代班的接手人。"))
                return
            matched_at_str = str(req.get("matched_at", "")).strip()
            if matched_at_str:
                try:
                    tz = pytz.timezone("Asia/Taipei")
                    matched_at = datetime.datetime.fromisoformat(matched_at_str)
                    if matched_at.tzinfo is None:
                        matched_at = tz.localize(matched_at)
                    elapsed = (datetime.datetime.now(tz) - matched_at).total_seconds()
                    if elapsed > 300:
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(
                            text=f"取消期限已過（5分鐘）。\n若您確實無法代班 {req['date']} {req['time_slot']}，請您自行利用「找代班」功能重新發出代班請求。"))
                        return
                except Exception as e:
                    print(f"matched_at parse error: {e}")
            res = sheets.cancel_sub_request_by_id(req_id, user_name)
            if res == "released_by_taker":
                group_msg = f"代班資訊更新：\n\n{user_name} 已取消接手 {req['date']} ({req['time_slot']}) 原負責人 {req['requester_name']} 的代班。\n該時段已重新開放，有意願者可點擊原卡片重新接手。"
                
                # 處理回覆與推播邏輯
                if event.source.type == "group":
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=group_msg))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已成功取消接手，該時段已重新放回代班市場。"))
                    # 移除對群組與原申請人的主動 Push 通知以節省額度
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="取消失敗，請稍後再試。"))

        elif action == "cancel_lock":
            # 使用者在第一步確認彈窗取消 → 釋放鎖定
            req_id = int(data_dict.get("request_id", 0))
            user = sheets.get_user(user_id)
            if not user: return
            user_name = str(user["name"]).strip()
            req = sheets.get_sub_request_by_id(req_id)
            if req:
                pending = str(req.get("pending_taker", "")).strip()
                if pending.startswith(user_name + "|"):
                    if req_id:
                        sheets.release_lock(req_id)
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已取消接手確認，班次重新開放。"))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="操作無效：此班次目前並非由您鎖定。"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="找不到該請求。"))

        elif action == "cancel_sub_op":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已取消操作。"))

    except Exception as e:
        print(f"handle_postback Error: {e}")


# ─────────────────────────────────────────────
# Flex Message 模板
# ─────────────────────────────────────────────

def create_sos_card(req: dict, name: str, show_cancel: bool = False) -> FlexSendMessage:
    """整合版卡片：同時包含[接手]與[撤回]按鈕。"""
    footer_contents = [
        {
            "type": "button", "style": "primary", "color": "#64748B", "height": "sm",
            "action": {"type": "postback", "label": "確認接手", "data": f"action=accept_sub&request_id={req['id']}"}
        },
        {
            "type": "button", "style": "secondary", "color": "#F1F5F9", "height": "sm", "margin": "md",
            "action": {"type": "postback", "label": "撤回此申請", "data": f"action=cancel_sub_req_start&request_id={req['id']}"}
        }
    ]

    return FlexSendMessage(
        alt_text="代班請求",
        contents={
            "type": "bubble", "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#64748B", "paddingAll": "15px",
                "contents": [
                    {"type": "text", "text": "REQUIREMENT", "color": "#F1F5F9", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": "代班請求", "color": "#FFFFFF", "size": "lg", "weight": "bold"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "15px", "spacing": "sm",
                "contents": [
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "日期", "color": "#94A3B8", "flex": 2, "size": "sm"},
                        {"type": "text", "text": str(req["date"]), "color": "#334155", "flex": 5, "size": "sm", "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "時段", "color": "#94A3B8", "flex": 2, "size": "sm"},
                        {"type": "text", "text": str(req["time_slot"]), "color": "#334155", "flex": 5, "size": "sm", "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "申請人", "color": "#94A3B8", "flex": 2, "size": "sm"},
                        {"type": "text", "text": name, "color": "#334155", "flex": 5, "size": "sm", "weight": "bold"}
                    ]},
                    {"type": "separator", "margin": "md", "color": "#E2E8F0"},
                    {"type": "box", "layout": "vertical", "margin": "md", "contents": [
                        {"type": "text", "text": "事由", "color": "#94A3B8", "size": "xs", "weight": "bold"},
                        {"type": "text", "text": str(req["reason"]), "wrap": True, "color": "#475569", "size": "sm", "margin": "sm"}
                    ]}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "10px",
                "contents": footer_contents
            }
        }
    )

def create_daily_flex(date_str: str, weekday_str: str, grouped_results: dict) -> FlexSendMessage:
    rows = []
    
    for time_slot, owners in grouped_results.items():
        people_contents = []
        for o in owners:
            status_text = o["current"]
            status_color = "#334155"
            
            if o.get("seeking_sub"):
                status_text = f"{o['current']} (徵代班)"
                status_color = "#94A3B8"
            elif o["current"] != o["original"]:
                status_text = f"{o['original']}➔{o['current']}"
                status_color = "#64748B"

            people_contents.append({
                "type": "text", "text": status_text, "size": "sm", "color": status_color, "wrap": True
            })

        rows.append({
            "type": "box", "layout": "horizontal", "margin": "lg", "spacing": "md",
            "contents": [
                {"type": "text", "text": time_slot, "size": "sm", "weight": "bold", "color": "#64748B", "flex": 5},
                {
                    "type": "box", "layout": "vertical", "flex": 7,
                    "contents": people_contents
                }
            ]
        })

    return FlexSendMessage(
        alt_text="排班查詢",
        contents={
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "paddingAll": "20px",
                "contents": [
                    {"type": "text", "text": "DAILY SCHEDULE", "color": "#94A3B8", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": f"{date_str} ({weekday_str})", "color": "#334155", "size": "md", "weight": "bold"}
                ]
            },
            "body": {"type": "box", "layout": "vertical", "paddingAll": "15px", "contents": rows}
        }
    )

def create_weekday_flex(plan_name: str, weekday_str: str, slots: list) -> FlexSendMessage:
    # 依時段分組
    from collections import defaultdict
    grouped = defaultdict(list)
    for s in slots:
        grouped[str(s["time_slot"]).strip()].append(str(s["user_name"]).strip())

    rows = []
    for time_slot, names in grouped.items():
        rows.append({
            "type": "box", "layout": "horizontal", "margin": "lg", "spacing": "md",
            "contents": [
                {"type": "text", "text": time_slot, "size": "sm", "weight": "bold", "color": "#64748B", "flex": 5},
                {"type": "text", "text": "、".join(names), "size": "sm", "color": "#334155", "flex": 7, "wrap": True}
            ]
        })

    return FlexSendMessage(
        alt_text="通用班表查詢",
        contents={
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "paddingAll": "20px",
                "contents": [
                    {"type": "text", "text": plan_name, "color": "#94A3B8", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": f"排班 - {weekday_str}", "color": "#334155", "size": "md", "weight": "bold"}
                ]
            },
            "body": {"type": "box", "layout": "vertical", "paddingAll": "15px", "contents": rows}
        }
    )

def create_main_menu_flex() -> FlexSendMessage:
    return FlexSendMessage(
        alt_text="主選單",
        contents={
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "paddingAll": "20px",
                "contents": [
                    {"type": "text", "text": "MAIN MENU", "color": "#94A3B8", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": "代班小幫手", "color": "#334155", "size": "xl", "weight": "bold", "margin": "sm"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "20px", "spacing": "md",
                "contents": [
                    {"type": "button", "style": "primary", "color": "#64748B", "height": "sm",
                     "action": {"type": "message", "label": "查班表", "text": "查詢"}},
                    {"type": "button", "style": "primary", "color": "#64748B", "height": "sm", "margin": "md",
                     "action": {"type": "message", "label": "找代班", "text": "找代班"}},
                    {"type": "button", "style": "primary", "color": "#64748B", "height": "sm", "margin": "md",
                     "action": {"type": "message", "label": "接代班", "text": "接代班"}},
                    {"type": "button", "style": "secondary", "color": "#CBD5E1", "height": "sm", "margin": "md",
                     "action": {"type": "message", "label": "說明書", "text": "說明書"}}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "10px",
                "contents": [
                    {"type": "text", "text": "點擊上方按鈕執行功能", "size": "xs", "color": "#94A3B8", "align": "center"}
                ]
            }
        }
    )

def create_market_carousel(reqs: list) -> FlexSendMessage:
    bubbles = []
    # 排序：日期、時段
    reqs.sort(key=lambda x: (str(x.get("date", "")), str(x.get("time_slot", ""))))
    
    # 最多前 12 筆 (避免過長)
    for req in reqs[:12]:
        status = str(req.get("status", "")).strip()
        is_matched = (status == "已結案")
        
        header_color = "#64748B" if not is_matched else "#94A3B8"
        status_text = "徵代班" if not is_matched else f"已由 {req.get('sub_user_name')} 接手"
        status_color = "#FFFFFF" if not is_matched else "#F1F5F9"

        bubbles.append({
            "type": "bubble", "size": "micro",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": header_color, "paddingAll": "10px",
                "contents": [
                    {"type": "text", "text": str(req["date"]), "color": "#FFFFFF", "size": "sm", "weight": "bold"},
                    {"type": "text", "text": status_text, "color": status_color, "size": "xxs", "margin": "xs"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "10px", "spacing": "xs",
                "contents": [
                    {"type": "text", "text": str(req["time_slot"]), "size": "xs", "weight": "bold", "color": "#334155"},
                    {"type": "text", "text": f"申請人: {req['requester_name']}", "size": "xs", "color": "#64748B"},
                    {"type": "text", "text": f"原因: {req.get('reason', '無')}", "size": "xs", "color": "#94A3B8", "wrap": True}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "5px",
                "contents": [
                    {"type": "button", "style": "primary", "color": "#64748B", "height": "sm",
                     "disabled": is_matched,
                     "action": {"type": "postback", "label": "接手" if not is_matched else "已結案", 
                                "data": f"action=accept_sub&request_id={req['id']}"}}
                ]
            }
        })
    
    return FlexSendMessage(alt_text="代班大廳", contents={"type": "carousel", "contents": bubbles})

def create_reminder_flex(date_str: str, matched: list, seeking: list) -> FlexSendMessage:
    contents = [
        {"type": "text", "text": "SCHEDULE REMINDER", "color": "#94A3B8", "size": "xs", "weight": "bold"},
        {"type": "text", "text": f"{date_str} 異動通知", "color": "#334155", "size": "md", "weight": "bold", "margin": "sm"},
        {"type": "separator", "margin": "lg"}
    ]

    if matched:
        contents.append({"type": "text", "text": "[已媒合項目]", "weight": "bold", "size": "sm", "margin": "lg", "color": "#475569"})
        for m in matched:
            contents.append({
                "type": "text", "text": f"- {m['time_slot']}: {m['requester_name']} -> {m['sub_user_name']}",
                "size": "xs", "color": "#64748B", "margin": "xs"
            })

    if seeking:
        contents.append({"type": "text", "text": "[尚無人代班]", "weight": "bold", "size": "sm", "margin": "lg", "color": "#991B1B"})
        for s in seeking:
            contents.append({
                "type": "text", "text": f"- {s['time_slot']}: {s['requester_name']} (徵求中)",
                "size": "xs", "color": "#EF4444", "margin": "xs"
            })

    return FlexSendMessage(
        alt_text="明日代班異動提醒",
        contents={
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": contents}
        }
    )

def create_query_picker_flex() -> FlexSendMessage:
    return FlexSendMessage(
        alt_text="選擇查詢日期",
        contents={
            "type": "bubble", "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "paddingAll": "15px",
                "contents": [
                    {"type": "text", "text": "SCHEDULE QUERY", "color": "#94A3B8", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": "請選擇查詢日期", "color": "#334155", "size": "md", "weight": "bold"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "15px", "spacing": "sm",
                "contents": [
                    {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [
                        {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "message", "label": "今天", "text": "查詢 今天"}},
                        {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "message", "label": "明天", "text": "查詢 明天"}}
                    ]},
                    {"type": "separator", "margin": "md"},
                    {"type": "text", "text": "快速選擇星期", "size": "xs", "color": "#94A3B8", "margin": "md"},
                    {"type": "box", "layout": "horizontal", "spacing": "xs", "contents": [
                        {"type": "button", "action": {"type": "message", "label": "一", "text": "查詢 星期一"}},
                        {"type": "button", "action": {"type": "message", "label": "二", "text": "查詢 星期二"}},
                        {"type": "button", "action": {"type": "message", "label": "三", "text": "查詢 星期三"}},
                        {"type": "button", "action": {"type": "message", "label": "四", "text": "查詢 星期四"}}
                    ]},
                    {"type": "box", "layout": "horizontal", "spacing": "xs", "contents": [
                        {"type": "button", "action": {"type": "message", "label": "五", "text": "查詢 星期五"}},
                        {"type": "button", "action": {"type": "message", "label": "六", "text": "查詢 星期六"}},
                        {"type": "button", "action": {"type": "message", "label": "日", "text": "查詢 星期日"}}
                    ]},
                    {"type": "separator", "margin": "lg"},
                    {"type": "text", "text": "選擇特定日期", "size": "xs", "color": "#94A3B8", "margin": "md", "align": "center"},
                    {"type": "button", "style": "primary", "color": "#64748B", "height": "sm", 
                     "action": {
                         "type": "datetimepicker",
                         "label": "點我開啟日曆",
                         "data": "action=query_by_date",
                         "mode": "date"
                     }}
                ]
            }
        }
    )

def create_matching_success_flex(req: dict, taker_name: str, role: str = "requester") -> FlexSendMessage:
    """
    role:
      "taker"     → 接手人收到，含 5 分鐘取消按鈕
      "requester" → 申請人收到，純通知無按鈕
      "group"     → 群組通知（同 requester）
    """
    title = "代班媒合成功"
    bg_color = "#F8FAFC"

    body_contents = [
        {"type": "text", "text": f"日期：{req['date']}", "size": "sm", "color": "#64748B"},
        {"type": "text", "text": f"時段：{req['time_slot']}", "size": "sm", "color": "#64748B", "margin": "sm"},
        {"type": "box", "layout": "horizontal", "margin": "lg", "contents": [
            {"type": "text", "text": str(req["requester_name"]), "color": "#94A3B8", "weight": "bold",
             "align": "center", "size": "sm", "flex": 4},
            {"type": "text", "text": "→", "align": "center", "color": "#CBD5E1", "size": "md", "flex": 2},
            {"type": "text", "text": taker_name, "color": "#475569", "weight": "bold",
             "align": "center", "size": "sm", "flex": 4}
        ]},
        {"type": "text", "text": f"事由：{req.get('reason', '無')}", "size": "xs", "color": "#94A3B8",
         "margin": "md", "align": "center", "wrap": True}
    ]

    footer_contents = []
    if role == "taker":
        footer_contents = [
            {"type": "text", "text": "※ 接手後 5 分鐘內可取消接手", "size": "xs",
             "color": "#94A3B8", "align": "center", "margin": "sm"},
            {"type": "button", "style": "secondary", "color": "#F1F5F9", "height": "sm", "margin": "md",
             "action": {"type": "postback", "label": "取消接手",
                        "data": f"action=cancel_by_taker&request_id={req['id']}"}}
        ]

    return FlexSendMessage(
        alt_text=title,
        contents={
            "type": "bubble", "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": bg_color, "paddingAll": "15px",
                "contents": [
                    {"type": "text", "text": "MATCHED ✓", "color": "#94A3B8", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": title, "color": "#334155", "size": "md", "weight": "bold"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "15px", "spacing": "sm",
                "contents": body_contents
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "10px",
                "contents": footer_contents if footer_contents else [
                    {"type": "text", "text": "代班媒合已確認", "size": "xs", "color": "#94A3B8", "align": "center"}
                ]
            }
        }
    )


def create_match_group_notification_flex(req: dict, taker_name: str) -> FlexSendMessage:
    """發送到群組的媒合成功通知卡，含 5 分鐘內撤銷按鈕。"""
    return FlexSendMessage(
        alt_text="代班媒合成功通知",
        contents={
            "type": "bubble", "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "paddingAll": "15px",
                "contents": [
                    {"type": "text", "text": "GROUP NOTICE", "color": "#94A3B8", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": "代班媒合成功 ✓", "color": "#334155", "size": "md", "weight": "bold"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "15px", "spacing": "sm",
                "contents": [
                    {"type": "text", "text": f"日期：{req['date']}", "size": "sm", "color": "#64748B"},
                    {"type": "text", "text": f"時段：{req['time_slot']}", "size": "sm", "color": "#64748B", "margin": "sm"},
                    {"type": "box", "layout": "horizontal", "margin": "lg", "contents": [
                        {"type": "text", "text": str(req["requester_name"]), "color": "#94A3B8", "weight": "bold",
                         "align": "center", "size": "sm", "flex": 4},
                        {"type": "text", "text": "→", "align": "center", "color": "#CBD5E1", "size": "md", "flex": 2},
                        {"type": "text", "text": taker_name, "color": "#475569", "weight": "bold",
                         "align": "center", "size": "sm", "flex": 4}
                    ]},
                    {"type": "text", "text": f"事由：{req.get('reason', '無')}", "size": "xs",
                     "color": "#94A3B8", "margin": "md", "align": "center", "wrap": True}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "10px", "spacing": "sm",
                "contents": [
                    {"type": "button", "style": "secondary", "color": "#F1F5F9", "height": "sm",
                     "action": {"type": "postback", "label": "撤銷此回覆 (5分鐘內)", 
                                "data": f"action=cancel_by_taker&request_id={req['id']}"}},
                    {"type": "text", "text": "代班安排已完成，感謝配合！", "size": "xs",
                     "color": "#CBD5E1", "align": "center", "margin": "sm"}
                ]
            }
        }
    )

def create_cancellation_confirm_flex(req_id: int) -> FlexSendMessage:
    return FlexSendMessage(
        alt_text="確認取消代班",
        contents={
            "type": "bubble", "size": "mega",
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [
                    {"type": "text", "text": "確認取消", "weight": "bold", "size": "md", "color": "#334155"},
                    {"type": "text", "text": "您確定要取消這項代班安排嗎？行為將無法復原。", "size": "sm", "color": "#64748B", "margin": "md", "wrap": True},
                    {"type": "box", "layout": "horizontal", "margin": "lg", "spacing": "sm", "contents": [
                        {"type": "button", "style": "primary", "color": "#64748B", "height": "sm",
                         "action": {"type": "postback", "label": "確定取消", "data": f"action=confirm_cancel_sub_req&request_id={req_id}"}},
                        {"type": "button", "style": "secondary", "height": "sm",
                         "action": {"type": "postback", "label": "先不要", "data": "action=cancel_sub_op"}}
                    ]}
                ]
            }
        }
    )
