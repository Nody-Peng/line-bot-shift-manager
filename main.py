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
from dotenv import load_dotenv
from pydantic import BaseModel

# Google Sheets 整合
import sheets

load_dotenv()

class SubSubmitData(BaseModel):
    date: str
    time_slots: list[str]
    reason: str
    userId: str
    groupId: Optional[str] = None

app = FastAPI()
templates = Jinja2Templates(directory="templates")

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

LIFF_ID = os.getenv('LINE_LIFF_ID', 'YOUR_LIFF_ID')

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

@app.post("/api/submit-sub")
async def api_submit_sub(data: SubSubmitData):
    # 1. 時間檢核：不能申請過去的班
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    if data.date < today_str:
        print(f"DEBUG: Date check failed. Request date: {data.date}, Today: {today_str}")
        raise HTTPException(status_code=400, detail="無法申請過去的代班。")

    # 2. 確認使用者已綁定
    user = sheets.get_user(data.userId)
    if not user:
        print(f"DEBUG: User search failed for ID: {data.userId}")
        raise HTTPException(status_code=400, detail="請先在對話框輸入「綁定 您的姓名」。")

    user_name = str(user["name"]).strip()
    success_requests = []
    errors = []

    # 3. 處理每一個申請的時段
    for slot in data.time_slots:
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

    # 5. 推播到群組或個人
    target_id = data.groupId or data.userId
    try:
        # 批次發送 (每個時段一張卡)
        for req in success_requests:
            flex = create_sos_card(req, user_name)
            line_bot_api.push_message(target_id, flex)
    except Exception as e:
        print(f"Push error: {e}")

    return {"status": "success", "count": len(success_requests)}


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
    flex = create_help_flex()
    line_bot_api.reply_message(event.reply_token, flex)

@handler.add(JoinEvent)
def handle_join(event):
    flex = create_help_flex()
    line_bot_api.reply_message(event.reply_token, flex)


# ─────────────────────────────────────────────
# 事件：文字訊息
# ─────────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    msg = event.message.text.strip()
    user_id = event.source.user_id

    try:
        # ── 1. 綁定身分 ──────────────────────────────
        if msg.startswith("綁定 "):
            name = msg.split(" ", 1)[1].strip()
            action = sheets.upsert_user(user_id, name)
            reply = f"系統提示：{'更新' if action == 'updated' else '新建'}綁定成功 ({name})"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

        # ── 1b. 說明書 / 幫助 ──────────────────────────
        elif msg in ["說明書", "幫助", "指令", "使用說明", "help", "Help", "?"]:
            flex = create_help_flex()
            line_bot_api.reply_message(event.reply_token, flex)

        # ── 2. 找代班 ────────────────────────────────
        elif msg == "找代班":
            user = sheets.get_user(user_id)
            if not user:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="系統提示：請先綁定身分。"))
                return
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
        elif msg.startswith("查詢"):
            parts = msg.split(" ", 1)
            query_target = parts[1].strip() if len(parts) > 1 else "今天"

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

        # ── 4. 預設幫助訊息 ──────────────────────────
        else:
            flex = create_help_flex()
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
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "paddingAll": "20px",
                "contents": [
                    {"type": "text", "text": "代班小幫手", "color": "#94A3B8", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": "使用說明書", "color": "#334155", "size": "xl", "weight": "bold", "margin": "sm"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "20px", "spacing": "md",
                "contents": [
                    {
                        "type": "box", "layout": "vertical",
                        "contents": [
                            {"type": "text", "text": "1. 帳號綁定", "weight": "bold", "size": "md", "color": "#475569"},
                            {"type": "text", "text": "請在私訊中輸入「綁定 您的姓名」\n範例：綁定 王彥心", "size": "sm", "color": "#64748B", "wrap": True, "margin": "sm"}
                        ]
                    },
                    {"type": "separator", "margin": "lg"},
                    {
                        "type": "box", "layout": "vertical", "margin": "lg",
                        "contents": [
                            {"type": "text", "text": "2. 查詢班表", "weight": "bold", "size": "md", "color": "#475569"},
                            {"type": "text", "text": "輸入「查詢」關鍵字：\n• 查詢 今天 / 明天\n• 查詢 星期三\n• 查詢 2026-04-10", "size": "sm", "color": "#64748B", "wrap": True, "margin": "sm"}
                        ]
                    },
                    {"type": "separator", "margin": "lg"},
                    {
                        "type": "box", "layout": "vertical", "margin": "lg",
                        "contents": [
                            {"type": "text", "text": "3. 申請代班", "weight": "bold", "size": "md", "color": "#475569"},
                            {"type": "text", "text": "輸入「找代班」即可開啟申請表單。\n表單內可一次勾選當天多個時段。", "size": "sm", "color": "#64748B", "wrap": True, "margin": "sm"}
                        ]
                    }
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "10px",
                "contents": [
                    {"type": "text", "text": "輸入「幫助」可隨時喚出此選單", "size": "xs", "color": "#CBD5E1", "align": "center"}
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

        # ── 接手代班（第一步：確認彈窗）──
        if action == "accept_sub":
            req_id = int(data_dict.get("request_id"))
            req = sheets.get_sub_request_by_id(req_id)

            if not req or str(req.get("status", "")).strip() == "已結案":
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="狀態通知：此代班請求已結束或不存在。"))
                return

            confirm_template = ConfirmTemplate(
                text=(f"請確認是否接手此代班作業：\n\n"
                      f"日期：{req['date']}\n"
                      f"時段：{req['time_slot']}\n"
                      f"申請人：{req['requester_name']}"),
                actions=[
                    PostbackAction(label="確認接手", data=f"action=confirm_accept_sub&request_id={req_id}"),
                    PostbackAction(label="取消操作", data="action=cancel_sub")
                ]
            )
            line_bot_api.reply_message(event.reply_token,
                TemplateSendMessage(alt_text="確認代班作業", template=confirm_template))

        # ── 接手代班（第二步：正式更新）──
        elif action == "confirm_accept_sub":
            req_id = int(data_dict.get("request_id"))

            user = sheets.get_user(user_id)
            if not user:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="系統提示：請先輸入「綁定 [姓名]」才能執行此操作。"))
                return

            user_name = str(user["name"]).strip()
            req = sheets.get_sub_request_by_id(req_id)

            if not req or str(req.get("status", "")).strip() == "已結案":
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="處理失敗：任務可能已由他人接手或失效。"))
                return

            # 不能接手自己的
            if str(req.get("requester_name", "")).strip() == user_name:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="系統提示：您無法接手自己的代班請求。"))
                return

            # ★ 防止同時段已有排班的人來接手（新增驗證）
            plan = sheets.find_active_plan(str(req["date"]))
            if plan:
                dt_obj = datetime.datetime.strptime(str(req["date"]), "%Y-%m-%d")
                weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
                weekday_str = weekday_names[dt_obj.weekday()]
                slots = sheets.get_slots(int(plan["id"]), weekday_str)
                scheduled_names = [str(s["user_name"]).strip() for s in slots
                                   if str(s.get("time_slot", "")).strip() == str(req["time_slot"]).strip()]
                if user_name in scheduled_names:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"系統提示：您在 {req['date']} {req['time_slot']} 本來就有排班，無法接手同時段的代班。"))
                    return

            # 結案
            success = sheets.close_sub_request(req_id, user_name)
            if not success:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="處理失敗：請稍後再試。"))
                return

            success_flex = FlexSendMessage(
                alt_text="代班媒合成功",
                contents={
                    "type": "bubble", "size": "mega",
                    "header": {
                        "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "paddingAll": "15px",
                        "contents": [
                            {"type": "text", "text": "STATUS", "color": "#94A3B8", "size": "xs", "weight": "bold"},
                            {"type": "text", "text": "媒合成功", "color": "#334155", "size": "md", "weight": "bold"}
                        ]
                    },
                    "body": {
                        "type": "box", "layout": "vertical", "paddingAll": "15px", "spacing": "sm",
                        "contents": [
                            {"type": "text", "text": f"日期：{req['date']} ({req['time_slot']})", "size": "sm", "color": "#64748B"},
                            {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                                {"type": "text", "text": str(req["requester_name"]), "color": "#94A3B8", "weight": "bold", "align": "center", "size": "sm"},
                                {"type": "text", "text": "➔", "align": "center", "color": "#CBD5E1", "size": "sm"},
                                {"type": "text", "text": user_name, "color": "#475569", "weight": "bold", "align": "center", "size": "sm"}
                            ]},
                            {"type": "text", "text": "系統已自動登錄此變動", "margin": "lg", "size": "xs", "color": "#94A3B8", "align": "center"}
                        ]
                    }
                }
            )
            line_bot_api.reply_message(event.reply_token, success_flex)

        elif action == "cancel_sub":
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="已取消操作。"))

    except Exception as e:
        print(f"handle_postback Error: {e}")


# ─────────────────────────────────────────────
# Flex Message 模板
# ─────────────────────────────────────────────

def create_sos_card(req: dict, name: str) -> FlexSendMessage:
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
                "contents": [
                    {"type": "button", "style": "primary", "color": "#64748B", "height": "sm",
                     "action": {"type": "postback", "label": "確認接手", "data": f"action=accept_sub&request_id={req['id']}"}}
                ]
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
                status_text = f"{o['current']} (待校辦)"
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
