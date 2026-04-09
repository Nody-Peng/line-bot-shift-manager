import os, json
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Response
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, FlexSendMessage,
    CarouselContainer, BubbleContainer, BoxComponent, TextComponent,
    ButtonComponent, PostbackAction, URIAction,
    TemplateSendMessage, ButtonsTemplate, ConfirmTemplate, DatetimePickerTemplateAction,
    QuickReply, QuickReplyButton
)
import datetime
import csv
from io import StringIO
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

# 資料庫相關匯入
from database import SessionLocal, engine, Base
from models import User, SubRequest, WeeklySchedule
from pydantic import BaseModel

load_dotenv()

# 數據模型定義
class SubSubmitData(BaseModel):
    date: str
    time_slot: str
    reason: str
    userId: str # LINE User ID
    groupId: Optional[str] = None # 如果在群組或聊天室內

# 確保資料表已建立
Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# 從環境變數讀取 LIFF ID
LIFF_ID = os.getenv('LINE_LIFF_ID', 'YOUR_LIFF_ID')
BASE_URL = os.getenv('BASE_URL', '')

# 獲取資料庫連線的依賴函式
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 用於追蹤使用者目前對話進度的字典
user_states = {}

@app.get("/")
def read_root():
    return {"message": "LINE Bot is running!", "admin_url": "/admin", "liff_url": "/liff"}

# --- 網頁路由 ---

@app.get("/liff")
async def get_liff_form(request: Request):
    return templates.TemplateResponse(
        request=request, name="liff_form.html", context={"liff_id": LIFF_ID}
    )

@app.get("/admin")
async def admin_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="admin_upload.html"
    )

@app.get("/template.csv")
async def download_template():
    with open("template.csv", "r", encoding="utf-8") as f:
        content = f.read()
    return Response(content=content, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=template.csv"})

@app.post("/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    if not file.filename.endswith('.csv'):
        return {"error": "請上傳 CSV 檔案"}
    
    content = await file.read()
    decoded = content.decode('utf-8')
    csv_reader = csv.DictReader(StringIO(decoded))
    
    db = SessionLocal()
    try:
        # 清空舊班表
        db.query(WeeklySchedule).delete()
        
        # 匯入新班表
        for row in csv_reader:
            new_slot = WeeklySchedule(
                day_of_week=row['星期'].strip(),
                time_slot=row['時段'].strip(),
                user_name=row['姓名'].strip()
            )
            db.add(new_slot)
        db.commit()
    except Exception as e:
        db.rollback()
        return {"error": f"解析失敗: {str(e)}"}
    finally:
        db.close()
        
    return {"message": "班表匯入成功！"}

@app.post("/api/submit-sub")
async def api_submit_sub(data: SubSubmitData, request: Request):
    # 偵錯用：印出收到的原始資料
    print(f"DEBUG: Received API Sub Request: {data.json()}")
    db = SessionLocal()
    try:
        current_user = db.query(User).filter(User.line_user_id == data.userId).first()
        user_name = current_user.name if current_user else "匿名使用者"
        
        # 建立請求
        new_request = SubRequest(
            date=data.date, 
            time_slot=data.time_slot,
            original_user_name=user_name,
            reason=data.reason, 
            status="尋找中"
        )
        db.add(new_request)
        db.commit()
        db.refresh(new_request)
        
        # 決定要發送到哪裡
        target_id = data.groupId or data.userId
        
        try:
            flex_message = create_sos_card(new_request, user_name)
            line_bot_api.push_message(target_id, flex_message)
        except Exception as line_err:
            print(f"LINE Push Error: {line_err}")
            # 如果 push 失敗，提示使用者可能沒加好友或權限問題
            raise HTTPException(status_code=400, detail=f"LINE 傳送失敗，請確認您已將機器人加入好友，或機器人已加入此群組。內容：{str(line_err)}")
        
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"API Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()

# --- LINE Callback ---

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get('X-Line-Signature')
    body = await request.body()
    try:
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return 'OK'

# ==========================================
# 處理「文字訊息」
# ==========================================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id
    db = next(get_db())
    
    try:
        # 0. 處理來自 LIFF 的極速指令 (#NEW_SUB#)
        if user_message.startswith("#NEW_SUB#"):
            parts = user_message.split(" ")
            if len(parts) >= 4:
                date_str = parts[1]
                time_slot = parts[2]
                reason = " ".join(parts[3:])
                
                current_user = db.query(User).filter(User.line_user_id == user_id).first()
                # 存入資料庫
                new_request = SubRequest(
                    date=date_str, time_slot=time_slot,
                    original_user_name=current_user.name if current_user else "未知", 
                    reason=reason, status="尋找中"
                )
                db.add(new_request)
                db.commit()
                db.refresh(new_request)

                # 發布 SOS 卡片
                flex_message = create_sos_card(new_request, current_user.name if current_user else "未知")
                line_bot_api.reply_message(event.reply_token, flex_message)
                return

        # 1. 綁定身分
        if user_message.startswith("綁定 "):
            name_to_bind = user_message.split(" ")[1]
            existing_user = db.query(User).filter(User.line_user_id == user_id).first()
            
            if existing_user:
                existing_user.name = name_to_bind
                reply_text = f"已為您更新綁定姓名為：{name_to_bind}"
            else:
                new_user = User(line_user_id=user_id, name=name_to_bind)
                db.add(new_user)
                reply_text = f"✅ 綁定成功！歡迎你，{name_to_bind}！"
                
            db.commit()
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

        # 2. 找代班 (頂規版：直接給 LIFF 連結)
        elif user_message == "找代班":
            current_user = db.query(User).filter(User.line_user_id == user_id).first()
            if not current_user:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 請先綁定身分喔！"))
                return

            liff_url = f"https://liff.line.me/{LIFF_ID}"
            flex_message = FlexSendMessage(
                alt_text="開啟找代班表單",
                contents={
                    "type": "bubble",
                    "hero": {
                        "type": "image",
                        "url": "https://images.unsplash.com/photo-1506784919141-93ad54a106f2?auto=format&fit=crop&q=80&w=1000",
                        "size": "full",
                        "aspectRatio": "20:13",
                        "aspectMode": "cover"
                    },
                    "body": {
                        "type": "box",
                        "layout": "vertical",
                        "contents": [
                            {"type": "text", "text": "快速申請找代班", "weight": "bold", "size": "xl"},
                            {"type": "text", "text": "點擊下方按鈕開啟表單，一鍵完成申請", "size": "sm", "color": "#888888", "margin": "md"}
                        ]
                    },
                    "footer": {
                        "type": "box",
                        "layout": "vertical",
                        "contents": [
                            {
                                "type": "button",
                                "style": "primary",
                                "color": "#4A90E2",
                                "action": {
                                    "type": "uri",
                                    "label": "✍️ 填寫表單",
                                    "uri": liff_url
                                }
                            }
                        ]
                    }
                }
            )
            line_bot_api.reply_message(event.reply_token, flex_message)

        # 3. 查詢代班狀況 (高級版：整合週班表)
        elif user_message.startswith("查詢"):
            query_date = ""
            if len(user_message.split(" ")) > 1:
                query_date = user_message.split(" ")[1]
            else:
                query_date = datetime.datetime.now().strftime("%Y-%m-%d")

            try:
                date_obj = datetime.datetime.strptime(query_date, "%Y-%m-%d")
                weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
                weekday_str = weekdays[date_obj.weekday()]
                
                # 1. 撈出該天的原始班表
                base_schedule = db.query(WeeklySchedule).filter(WeeklySchedule.day_of_week == weekday_str).all()
                # 2. 撈出該天的代班請求
                sub_requests = db.query(SubRequest).filter(SubRequest.date == query_date).all()
                
                if not base_schedule and not sub_requests:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📅 {query_date} 目前沒有排班資料。"))
                    return

                # 製作超級質感的週視圖卡片
                reply_flex = create_query_flex(query_date, weekday_str, base_schedule, sub_requests)
                line_bot_api.reply_message(event.reply_token, reply_flex)
                
            except Exception as e:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 日期格式錯誤，請輸入 2026-04-13"))

        # 4. 預設幫助訊息
        else:
            help_text = (
                "🤖 系辦代班小精靈為您服務！\n"
                "可用指令：\n"
                "1. 綁定 [姓名]\n"
                "2. 找代班 [日期] [時段] [理由]\n"
                "3. 查詢 [日期]"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))

    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
    finally:
        db.close()


# ==========================================
# 處理「按鈕點擊事件 (Postback)」
# ==========================================
@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    postback_data = event.postback.data
    db = next(get_db())
    
    try:
        # 解析按鈕藏的資料 (例如 "action=accept_sub&request_id=1")
        data_dict = dict(item.split("=") for item in postback_data.split("&"))
        action = data_dict.get("action")
        
        # 處理互動流 - 選擇日期
        if action == "select_date":
            selected_date = event.postback.params['date']
            
            # 更新狀態
            user_states[user_id] = {
                "state": "waiting_time",
                "date": selected_date
            }
            
            # 建立快速回覆按鈕 (選時段 - 套用新的4個時段)
            quick_reply = QuickReply(items=[
                QuickReplyButton(action=PostbackAction(label="9~12", data="action=select_time&time=9~12", display_text="我想請 9~12 的班")),
                QuickReplyButton(action=PostbackAction(label="12~14", data="action=select_time&time=12~14", display_text="我想請 12~14 的班")),
                QuickReplyButton(action=PostbackAction(label="14~16", data="action=select_time&time=14~16", display_text="我想請 14~16 的班")),
                QuickReplyButton(action=PostbackAction(label="16~18", data="action=select_time&time=16~18", display_text="我想請 16~18 的班"))
            ])
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請選擇時段：", quick_reply=quick_reply))
            
        elif action == "select_time":
            time_slot = data_dict.get("time")
            user_state = user_states.get(user_id)
            
            if user_state and user_state.get("state") == "waiting_time":
                date = user_state.get("date")
                # 更新狀態
                user_states[user_id] = {
                    "state": "waiting_reason",
                    "date": date,
                    "time": time_slot
                }
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入請假理由："))

        # 處理 - 我願意代班 (第一步：詢問確認)
        elif action == "accept_sub":
            req_id = int(data_dict.get("request_id"))
            sub_request = db.query(SubRequest).filter(SubRequest.id == req_id).first()
            
            if not sub_request or sub_request.status == "已結案":
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 此代班任務已結案或不存在囉！"))
                return

            # 使用 LINE 原生確認彈窗 (ConfirmTemplate) - 更難誤觸且會「跳出來」
            confirm_template = ConfirmTemplate(
                text=f"�� 您確定要幫忙代這個班嗎？\n\n日期：{sub_request.date}\n時段：{sub_request.time_slot}\n申請人：{sub_request.original_user_name}",
                actions=[
                    PostbackAction(label="是的，我確定", data=f"action=confirm_accept_sub&request_id={req_id}"),
                    PostbackAction(label="不小心按到", data="action=cancel_sub")
                ]
            )
            line_bot_api.reply_message(
                event.reply_token, 
                TemplateSendMessage(alt_text="❓ 確認代班意願", template=confirm_template)
            )

        # 處理 - 我願意代班 (第二步：正式更新)
        elif action == "confirm_accept_sub":
            req_id = int(data_dict.get("request_id"))
            
            # 檢查點擊的人有沒有綁定姓名
            current_user = db.query(User).filter(User.line_user_id == user_id).first()
            if not current_user:
                line_bot_api.reply_message(
                    event.reply_token, TextSendMessage(text="⚠️ 請先輸入「綁定 你的姓名」才能接代班喔！")
                )
                return
                
            # 從資料庫找這筆代班請求
            sub_request = db.query(SubRequest).filter(SubRequest.id == req_id).first()
            
            if not sub_request or sub_request.status == "已結案":
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 動作失敗：此班表可能剛剛被其他人搶先一步接走，或已失效。"))
                return
                
            # 更新資料庫
            sub_request.sub_user_name = current_user.name
            sub_request.status = "已結案"
            db.commit()
            
            # 更新成功公告 (緊湊版)
            success_flex = FlexSendMessage(
                alt_text="🎉 代班媒合成功",
                contents={
                    "type": "bubble", "size": "mega",
                    "header": {
                        "type": "box", "layout": "vertical", "backgroundColor": "#4CAF50", "paddingAll": "15px",
                        "contents": [
                            {"type": "text", "text": "SUCCESS!", "color": "#ffffff99", "size": "xs", "weight": "bold"},
                            {"type": "text", "text": "代班媒合成功 🎉", "color": "#ffffff", "size": "md", "weight": "bold"}
                        ]
                    },
                    "body": {
                        "type": "box", "layout": "vertical", "paddingAll": "15px", "spacing": "sm",
                        "contents": [
                            {"type": "text", "text": f"📅 日期：{sub_request.date} ({sub_request.time_slot})", "size": "sm", "color": "#666666"},
                            {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
                                {"type": "text", "text": sub_request.original_user_name, "color": "#FF6B6B", "weight": "bold", "align": "center", "size": "sm"},
                                {"type": "text", "text": "➔", "align": "center", "color": "#aaaaaa", "size": "sm"},
                                {"type": "text", "text": current_user.name, "color": "#4A90E2", "weight": "bold", "align": "center", "size": "sm"}
                            ]},
                            {"type": "text", "text": "感謝救援，系統已自動登錄！", "margin": "lg", "size": "xs", "color": "#aaaaaa", "align": "center"}
                        ]
                    }
                }
            )
            line_bot_api.reply_message(event.reply_token, success_flex)
            return

        elif action == "cancel_sub":
             line_bot_api.reply_message(event.reply_token, TextSendMessage(text="好的，已取消動作。沒事沒事～"))
             return

    except Exception as e:
        print(f"Postback Error: {e}")
        db.rollback()
    finally:
        db.close()

# --- 輔助函式 (Flex Message 模板) ---

def create_sos_card(req, name):
    return FlexSendMessage(
        alt_text="🆘 緊急代班請求",
        contents={
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#FF6B6B",
                "contents": [
                    {"type": "text", "text": "SHIFT COVER", "color": "#ffffff99", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": "緊急徵求代班", "color": "#ffffff", "size": "lg", "weight": "bold"}
                ],
                "paddingAll": "15px"
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "15px", "spacing": "sm",
                "contents": [
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "📅 日期", "color": "#aaaaaa", "flex": 2, "size": "sm"},
                        {"type": "text", "text": req.date, "color": "#444444", "flex": 5, "size": "sm", "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "⏰ 時段", "color": "#aaaaaa", "flex": 2, "size": "sm"},
                        {"type": "text", "text": req.time_slot, "color": "#444444", "flex": 5, "size": "sm", "weight": "bold"}
                    ]},
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": "👤 原排班", "color": "#aaaaaa", "flex": 2, "size": "sm"},
                        {"type": "text", "text": name, "color": "#444444", "flex": 5, "size": "sm", "weight": "bold"}
                    ]},
                    {"type": "separator", "margin": "md"},
                    {"type": "box", "layout": "vertical", "margin": "md", "contents": [
                        {"type": "text", "text": "💡 請假理由", "color": "#aaaaaa", "size": "xs", "weight": "bold", "margin": "none"},
                        {"type": "text", "text": req.reason, "wrap": True, "color": "#666666", "size": "sm", "margin": "sm"}
                    ]}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "10px",
                "contents": [
                    {"type": "button", "style": "primary", "color": "#FF6B6B", "height": "sm", 
                     "action": {"type": "postback", "label": "💪 我願意代班", "data": f"action=accept_sub&request_id={req.id}"}}
                ]
            }
        }
    )

def create_query_flex(date_str, weekday_str, base, subs):
    rows = []
    time_slots = ["9~12", "12~14", "14~16", "16~18"]
    
    for slot in time_slots:
        # 找原始排班
        original = next((b.user_name for b in base if b.time_slot == slot), "無")
        # 找代班狀況
        sub_req = next((s for s in subs if s.time_slot == slot), None)
        
        status_text = original
        status_color = "#444444"
        
        if sub_req:
            if sub_req.status == "已結案":
                status_text = f"{original} ➔ {sub_req.sub_user_name}"
                status_color = "#4CAF50" # 成功色
            else:
                status_text = f"{original} (徵求中...)"
                status_color = "#FF6B6B" # 警告色

        rows.append({
            "type": "box", "layout": "horizontal", "paddingAll": "8px",
            "contents": [
                {"type": "text", "text": slot, "size": "sm", "color": "#aaaaaa", "flex": 2},
                {"type": "text", "text": status_text, "size": "sm", "color": status_color, "flex": 5, "weight": "bold"}
            ]
        })

    return FlexSendMessage(
        alt_text="📅 日排班狀況",
        contents={
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#1e293b", "paddingAll": "15px",
                "contents": [
                    {"type": "text", "text": "DAILY SCHEDULE", "color": "#ffffff99", "size": "xs", "weight": "bold"},
                    {"type": "text", "text": f"{date_str} ({weekday_str})", "color": "#ffffff", "size": "md", "weight": "bold"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "10px", "contents": rows
            }
        }
    )
