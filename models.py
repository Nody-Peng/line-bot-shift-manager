from sqlalchemy import Column, Integer, String
from database import Base

# 1. 使用者綁定表 (紀錄 LINE ID 與姓名的對應)
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    line_user_id = Column(String, unique=True, index=True)
    name = Column(String)

# 2. 預設每週班表 (紀錄星期幾、哪個時段是誰上班)
class WeeklySchedule(Base):
    __tablename__ = "weekly_schedule"
    id = Column(Integer, primary_key=True, index=True)
    day_of_week = Column(String)  # 例如: "星期一", "星期二"
    time_slot = Column(String)    # 例如: "上午", "下午", "晚上"
    user_name = Column(String)    # 負責人姓名

# 3. 代班請求紀錄表
class SubRequest(Base):
    __tablename__ = "sub_requests"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String)           # 請假日期 (例如: 2026-05-12)
    time_slot = Column(String)      # 請假時段 (上午/下午/晚上)
    original_user_name = Column(String) # 原排班人
    reason = Column(String)         # 請假理由
    sub_user_name = Column(String, nullable=True) # 代班人 (一開始為空)
    status = Column(String, default="尋找中")       # 狀態: "尋找中" 或 "已結案"