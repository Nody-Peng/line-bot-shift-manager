from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship
from database import Base

# 1. 使用者綁定表
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    line_user_id = Column(String, unique=True, index=True)
    name = Column(String)

# 2. 班表計畫 (區分寒暑假、開學等)
class SchedulePlan(Base):
    __tablename__ = "schedule_plans"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)         # e.g., 寒假班表
    start_date = Column(String)   # 格式: YYYY-MM-DD
    end_date = Column(String)     # 格式: YYYY-MM-DD
    
    slots = relationship("ScheduleSlot", back_populates="plan", cascade="all, delete-orphan")

# 3. 每週固定班表時段 (從 CSV 匯入)
class ScheduleSlot(Base):
    __tablename__ = "schedule_slots"
    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("schedule_plans.id"))
    day_of_week = Column(String)  # 星期一 ~ 星期日
    time_slot = Column(String)    # 09:00-12:00 等
    user_name = Column(String)    # 負責人姓名

    plan = relationship("SchedulePlan", back_populates="slots")

# 4. 代班請求與紀錄
class SubRequest(Base):
    __tablename__ = "sub_requests"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(String)             # 請假日期
    time_slot = Column(String)        # 請假時段
    requester_name = Column(String)   # 尋求代班的人 (可能是原本負責的人，也可能是接手後臨時有事的人)
    reason = Column(String)           # 請假理由
    sub_user_name = Column(String, nullable=True) # 接手代班的人
    status = Column(String, default="尋找中") # "尋找中", "已結案"