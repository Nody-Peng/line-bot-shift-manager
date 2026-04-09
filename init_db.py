from database import engine, Base
import models

# 根據 models.py 的設計，在資料庫中建立所有表格
print("開始建立資料庫與表格...")
Base.metadata.create_all(bind=engine)
print("✅ 資料庫與資料表建立完成！")