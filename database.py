from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# 設定 SQLite 資料庫的檔案名稱與路徑
SQLALCHEMY_DATABASE_URL = "sqlite:///./shift_manager.db"

# 建立連線引擎
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

# 建立資料庫對話連線 (Session)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 建立宣告基底 (後續的資料表都要繼承它)
Base = declarative_base()